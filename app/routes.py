from flask import (
    Blueprint, Response, render_template, redirect, url_for,
    flash, request, current_app, session, abort, jsonify, send_file
)
from app.forms import ReservationForm, LoginForm, ContactForm, ICalFeedForm, TestimonialForm
from app.models import Reservation, User, Apartment, ICalFeed, Coupon, Testimonial, ComplianceConfig, QuesturaLog
from app.services.ical_sync import sync_all_feeds
from app import db, mail, csrf, limiter
from flask_login import login_user, logout_user, login_required, current_user
from flask_mail import Message
from flask_babel import gettext as _

from datetime import datetime, date, timedelta
import json
import secrets
from sqlalchemy.exc import IntegrityError
import stripe
import requests
import threading
import os
import holidays
import csv
import io

# Import compliance services
from app.services.tourist_tax import get_tax_service
from app.services.questura import get_questura_service
from app.services.smart_lock import trigger_gate_open, trigger_door_unlock
from app.tasks.compliance import (
    submit_questura_daily, retry_failed_questura,
    generate_monthly_tourist_tax_report, send_guest_checkin_reminder
)


bp = Blueprint('routes', __name__)


# ── Helpers ───────────────────────────────────────────────────────────────────
def get_apartment():
    return Apartment.query.first()

def get_testimonials():
    testimonials = (
        Testimonial.query
        .filter_by(is_published=True)
        .order_by(Testimonial.is_featured.desc(), Testimonial.created_at.desc())
        .limit(6)
        .all()
    )
    return testimonials


def is_available(check_in, check_out):
    # Match the calendar loop exactly by checking against anything that IS NOT cancelled
    conflicts = Reservation.query.filter(
        Reservation.status != "cancelled",
        Reservation.check_in < check_out,
        Reservation.check_out > check_in
    ).count()
    return conflicts == 0

def _send_confirmation_emails(reservation):
    """Sends confirmation emails via Brevo's Web API over unblockable HTTPS Port 443."""
    try:
        cancel_url = url_for('routes.cancel_reservation', token=reservation.cancel_token, _external=True)
        apt = get_apartment()
        payment_summary = get_payment_summary(reservation)
        
        # Grab your Brevo credentials from your Railway variables
        brevo_api_key = current_app.config.get('MAIL_PASSWORD')
        sender_email = "lotto235roma@gmail.com"  # Your verified Brevo sender email

        print("📬 HTTPS API: Dispatching emails via Brevo Web API...", flush=True)

        url = "https://api.brevo.com/v3/smtp/email"
        headers = {
            "accept": "application/json",
            "content-type": "application/json",
            "api-key": brevo_api_key
        }

        # 1. Dispatch to Guest
        guest_payload = {
            "sender": {"name": "Lotto235 Garbatella", "email": sender_email},
            "to": [{"email": reservation.guest_email}],
            "subject": f"Booking confirmation — {apt.name if apt else 'My Apartment'}",
            "htmlContent": render_template(
                'email_confirmation.html',
                reservation=reservation,
                cancel_url=cancel_url,
                nights=reservation.nights,
                total=reservation.total_price,
                apartment=apt,
                payment_summary=payment_summary
            )
        }
        
        response1 = requests.post(url, headers=headers, data=json.dumps(guest_payload))
        print(f"✅ Brevo API Response (Guest): {response1.status_code}", flush=True)

        # 2. Dispatch to Admin (Rendered dynamically from the HTML template file)
        admin_recipient = current_app.config.get('ADMIN_EMAIL') or "lotto235roma@gmail.com"
        
        # Fixed blueprint routing parameter link:
        admin_cancel_url = url_for('routes.admin_cancel_via_token', token=reservation.cancel_token, _external=True)

        admin_payload = {
            "sender": {"name": "Booking Engine", "email": sender_email},
            "to": [{"email": admin_recipient}],
            "subject": f"🔔 New Booking Alert: {reservation.guest_name}",
            "htmlContent": render_template(
                'email_admin_alert.html',
                reservation=reservation,
                payment_summary=payment_summary,
                admin_cancel_url=admin_cancel_url
            )
        }        
        response2 = requests.post(url, headers=headers, data=json.dumps(admin_payload))
        print(f"✅ Brevo API Response (Admin): {response2.status_code}", flush=True)

    except Exception as exc:
        print('!!! BREVO API FAILURE !!!', flush=True)
        print(f'Error detail: {str(exc)}', flush=True)

def send_payment_verified_email(reservation):
    """Invia l'email HTML di conferma avvenuto pagamento via Brevo Web API (Porta 443)"""
    try:
        brevo_api_key = current_app.config.get('MAIL_PASSWORD')
        sender_email = "lotto235roma@gmail.com" # Sostituisci con il mittente verificato su Brevo

        url = "https://api.brevo.com/v3/smtp/email"
        headers = {
            "accept": "application/json",
            "content-type": "application/json",
            "api-key": brevo_api_key
        }

        # 1. Send to Guest
        guest_payload = {
            "sender": {"name": "Lotto235 Garbatella", "email": sender_email},
            "to": [{"email": reservation.guest_email}],
            "subject": f"✅ Pagamento Verificato e Confermato — Prenotazione #{reservation.id}",
            "htmlContent": render_template(
                'email_payment_verified.html',
                reservation=reservation
            )
        }
        
        response1 = requests.post(url, headers=headers, data=json.dumps(guest_payload))
        current_app.logger.info(f"📬 Brevo Verified Payment Email (Guest) sent. Status: {response1.status_code}")

        # 2. Send to Admin
        admin_recipient = current_app.config.get('ADMIN_EMAIL') or "lotto235roma@gmail.com"
        admin_payload = {
            "sender": {"name": "Booking Engine", "email": sender_email},
            "to": [{"email": admin_recipient}],
            "subject": f"✅ Payment Confirmed: {reservation.guest_name} — Reservation #{reservation.id}",
            "htmlContent": render_template(
                'email_admin_payment_confirmed.html',
                reservation=reservation
            )
        }
        
        response2 = requests.post(url, headers=headers, data=json.dumps(admin_payload))
        current_app.logger.info(f"📬 Brevo Verified Payment Email (Admin) sent. Status: {response2.status_code}")

        return response1.status_code in [200, 201, 202] and response2.status_code in [200, 201, 202]
    except Exception as e:
        current_app.logger.error(f"!!! BREVO API FAILURE FOR RESERVATION #{reservation.id} !!!: {str(e)}")
        return False


def send_pending_payment_email(reservation):
    """Invia l'email di conferma prenotazione ricevuta (in attesa di pagamento) via Brevo Web API"""
    try:
        brevo_api_key = current_app.config.get('MAIL_PASSWORD')
        sender_email = "lotto235roma@gmail.com"
        cancel_url = url_for('routes.cancel_reservation', token=reservation.cancel_token, _external=True)
        apt = get_apartment()
        payment_summary = get_payment_summary(reservation)
        
        # Generate check-in token if not exists
        if not reservation.checkin_token:
            reservation.checkin_token = secrets.token_urlsafe(32)
            db.session.commit()
        
        checkin_url = url_for('routes.guest_self_checkin', token=reservation.checkin_token, _external=True)
        
        url = "https://api.brevo.com/v3/smtp/email"
        headers = {
            "accept": "application/json",
            "content-type": "application/json",
            "api-key": brevo_api_key
        }

        # 1. Dispatch to Guest
        guest_payload = {
            "sender": {"name": "Lotto235 Garbatella", "email": sender_email},
            "to": [{"email": reservation.guest_email}],
            "subject": f"Booking received — {apt.name if apt else 'Lotto 235 Garbatella'}",
            "htmlContent": render_template(
                'email_pending_payment.html',
                reservation=reservation,
                cancel_url=cancel_url,
                nights=reservation.nights,
                total=reservation.total_price,
                apartment=apt,
                payment_summary=payment_summary,
                payment_method=reservation.payment_method,
                checkin_url=checkin_url
            )
        }
        
        response1 = requests.post(url, headers=headers, data=json.dumps(guest_payload))
        current_app.logger.info(f"📬 Brevo Pending Payment Email (Guest) sent. Status: {response1.status_code}")

        # 2. Dispatch to Admin
        admin_recipient = current_app.config.get('ADMIN_EMAIL') or "lotto235roma@gmail.com"
        admin_cancel_url = url_for('routes.admin_cancel_via_token', token=reservation.cancel_token, _external=True)

        admin_payload = {
            "sender": {"name": "Booking Engine", "email": sender_email},
            "to": [{"email": admin_recipient}],
            "subject": f"🔔 New Booking (Pending Payment): {reservation.guest_name}",
            "htmlContent": render_template(
                'email_admin_alert.html',
                reservation=reservation,
                payment_summary=payment_summary,
                admin_cancel_url=admin_cancel_url
            )
        }        
        response2 = requests.post(url, headers=headers, data=json.dumps(admin_payload))
        current_app.logger.info(f"📬 Brevo Pending Payment Email (Admin) sent. Status: {response2.status_code}")

        return response1.status_code in [200, 201, 202] and response2.status_code in [200, 201, 202]
    except Exception as e:
        current_app.logger.error(f"!!! BREVO PENDING PAYMENT EMAIL FAILURE FOR RESERVATION #{reservation.id} !!!: {str(e)}")
        return False


def send_cancellation_emails(reservation, refund_failed_warning=False, refund_percentage=1.0, refund_amount=None):
    """Sends cancellation emails to both guest and admin via Brevo API."""
    try:
        brevo_api_key = current_app.config.get('MAIL_PASSWORD')
        sender_email = "lotto235roma@gmail.com"
        admin_recipient = current_app.config.get('ADMIN_EMAIL') or "lotto235roma@gmail.com"

        url = "https://api.brevo.com/v3/smtp/email"
        headers = {
            "accept": "application/json",
            "content-type": "application/json",
            "api-key": brevo_api_key
        }

        # Determine refund status text
        if refund_percentage == 1.0:
            refund_text = "100% (full refund)"
        elif refund_percentage == 0.5:
            refund_text = "50% (partial refund)"
        else:
            refund_text = "0% (no refund per policy)"

        if refund_amount is not None:
            refund_text += f" — €{refund_amount:.2f}"

        refund_note = ""
        if refund_failed_warning:
            refund_note = "\n\n⚠️ Note: There was a delay processing your automatic refund. Our team has been flagged to verify it manually."

        # 1. Send to Guest
        guest_payload = {
            "sender": {"name": "Lotto235 Garbatella", "email": sender_email},
            "to": [{"email": reservation.guest_email}],
            "subject": f"Your reservation has been cancelled — Lotto 235 Garbatella",
            "htmlContent": render_template(
                'email_cancellation.html',
                reservation=reservation,
                refund_note=refund_note,
                refund_failed=refund_failed_warning,
                refund_percentage=refund_percentage,
                refund_amount=refund_amount
            )
        }
        
        response1 = requests.post(url, headers=headers, data=json.dumps(guest_payload))
        current_app.logger.info(f"📬 Brevo Cancellation Email (Guest) sent. Status: {response1.status_code}")

        # 2. Send to Admin
        refund_status = '⚠️ FAILED / MANUAL CHECK REQUIRED' if refund_failed_warning else f'✅ {refund_text}'
        admin_payload = {
            "sender": {"name": "Booking Engine", "email": sender_email},
            "to": [{"email": admin_recipient}],
            "subject": f"Reservation Cancelled: {reservation.guest_name} [REFUND {refund_status}]",
            "htmlContent": render_template(
                'email_admin_cancellation.html',
                reservation=reservation,
                refund_failed=refund_failed_warning,
                refund_status=refund_status,
                refund_percentage=refund_percentage,
                refund_amount=refund_amount
            )
        }
        
        response2 = requests.post(url, headers=headers, data=json.dumps(admin_payload))
        current_app.logger.info(f"📬 Brevo Cancellation Email (Admin) sent. Status: {response2.status_code}")

        return response1.status_code in [200, 201, 202] and response2.status_code in [200, 201, 202]
    except Exception as e:
        current_app.logger.error(f"!!! BREVO CANCELLATION EMAIL FAILURE FOR RESERVATION #{reservation.id} !!!: {str(e)}")
        return False


def calculate_refund_percentage(check_in_date):
    """
    Calculate refund percentage based on days until check-in.
    Returns percentage as float (e.g., 1.0 = 100%, 0.5 = 50%)
    """
    days_until = (check_in_date - date.today()).days
    
    if days_until > 14:
        return 1.0  # 100% refund
    elif days_until >= 7:
        return 0.5  # 50% refund
    else:
        return 0.0  # No refund


def calculate_dynamic_total(check_in, check_out, num_guests=2, base_rate=130.0):
    """
    Loops day-by-day from check_in up to (but excluding) check_out.
    Applies surcharges for Italian bank holidays / weekends, and extra guest fees.
    Applies 10% discount for stays longer than 7 nights.
    """
    # Initialize the Italian holiday registry
    it_holidays = holidays.Italy(years=[check_in.year, check_out.year])
    
    total_cost = 0.0
    current_date = check_in
    
    # Calculate extra guest surcharge per night (+15€ for each guest over 2)
    extra_guests = max(0, num_guests - 2)
    guest_surcharge = extra_guests * 15.0
    
    nights = (check_out - check_in).days
    
    while current_date < check_out:
        day_rate = base_rate
        
        # 1. Check for Italian National Bank Holidays (10% surcharge)
        if current_date in it_holidays:
            day_rate = base_rate * 1.10
            
        # 2. Check for Weekends (Friday and Saturday nights, 10% surcharge)
        elif current_date.weekday() in [4, 5]:
            day_rate = base_rate * 1.10
            
        # Add the fixed per-night extra guest fee
        day_rate += guest_surcharge
        
        total_cost += day_rate
        current_date += timedelta(days=1)
    
# 10% discount for stays of 7 nights or more
    if nights >= 7:
        total_cost *= 0.90
    
    return round(total_cost, 2)


# ── Public pages ──────────────────────────────────────────────────────────────
@bp.route('/')
def home():
    apartment = get_apartment()
    testimonials = get_testimonials()
    form = TestimonialForm()
    return render_template('apartment.html', apartment=apartment, testimonials=testimonials, form=form)


@bp.route('/faq')
def faq():
    return render_template('faq.html')


@bp.route('/terms')
def terms():
    return render_template('policies/terms.html')


@bp.route('/cancellation-policy')
def cancellation_policy():
    return render_template('policies/cancellation.html')


@bp.route('/refund-policy')
def refund_policy():
    return render_template('policies/refund.html')


@bp.route('/house-rules')
def house_rules():
    return render_template('policies/house_rules.html')


@bp.route('/privacy')
def privacy():
    return render_template('policies/privacy.html')


@bp.route('/food_recommendations')
def food_recommendations():
    return render_template('food_recommendations.html')


@bp.route('/attractions')
def attractions():
    return render_template('attractions.html')

@bp.route('/set-language/<lang>')
def set_language(lang):
    if lang in ['en', 'it', 'de', 'fr', 'es']:
        session['language'] = lang
    
    # Safely redirect back to the page the user was looking at.
    # Updated fallback namespace from 'bp.home' to 'routes.home' to match your template
    return redirect(request.referrer or url_for('routes.home'))

# ── Reservation / booking flow ────────────────────────────────────────────────
@bp.route('/reserve', methods=['GET', 'POST'])
def reserve():
    apartment = get_apartment()
    form = ReservationForm()

    if not apartment:
        return render_template(
            'reservation.html',
            form=form,
            apartment=None,
            disabled_dates=[]
        )

    reservations = Reservation.query.filter(
        Reservation.status != 'cancelled'
    ).all()

    disabled_dates = []
    for r in reservations:
        current = r.check_in
        last_night = r.check_out - timedelta(days=1)
        while current <= last_night:
            disabled_dates.append(current.isoformat())
            current += timedelta(days=1)

    if form.validate_on_submit():
        check_in  = form.check_in.data
        check_out = form.check_out.data

        if check_out <= check_in:
            flash("Check-out must be after check-in.", "danger")
            return redirect(request.url)

        nights = (check_out - check_in).days
        if nights > 28:
            flash("You can book a maximum of 28 nights.", "danger")
            return redirect(request.url)

        session.pop('pending_reservation', None)

        if not is_available(check_in, check_out):
            flash('Selected dates are not available.', 'danger')
            return redirect(request.url)

        # ── BACKEND COUPON VALIDATION ──
        coupon_code = request.form.get('applied_coupon_code', '').strip().upper()
        num_guests = form.num_guests.data
        base_total = calculate_dynamic_total(check_in, check_out, num_guests=num_guests, base_rate=apartment.price_per_night)
        final_total = base_total
        validated_code = None

        if coupon_code:
            coupon = Coupon.query.filter_by(code=coupon_code, active=True).first()
            if coupon:
                final_total = coupon.apply_discount(base_total)
                validated_code = coupon.code

        session['pending_reservation'] = {
            'guest_name':  form.guest_name.data,
            'guest_email': form.guest_email.data,
            'check_in':    check_in.isoformat(),
            'check_out':   check_out.isoformat(),
            'num_guests':  form.num_guests.data,
            'base_total':  base_total,
            'total_price': final_total,        # Track the actual discounted price
            'coupon_code': validated_code       # Attach code identifier to session payload
        }
        return redirect(url_for('routes.checkout'))

    return render_template(
        'reservation.html',
        form=form,
        apartment=apartment,
        disabled_dates=disabled_dates,
    )


@bp.route('/checkout')
def checkout():
    pending = session.get('pending_reservation')
    if not pending:
        flash(_('Please fill in the booking form first.'), 'warning')
        return redirect(url_for('routes.reserve'))

    apartment = get_apartment()
    check_in  = date.fromisoformat(pending['check_in'])
    check_out = date.fromisoformat(pending['check_out'])
    nights    = (check_out - check_in).days
    
    # Calcola il prezzo base dinamico usando la tua funzione giorno per giorno
    base_rate = apartment.price_per_night if apartment else 0
    num_guests = pending.get('num_guests', 2)
    calculated_base = calculate_dynamic_total(check_in, check_out, num_guests=num_guests, base_rate=base_rate)
    
    # Se esiste già un prezzo scontato/modificato da coupon in sessione usa quello, altrimenti usa il calcolato dinamico
    base_total = pending.get('base_total', calculated_base)
    total_price = pending.get('total_price', base_total)

    stripe_pub = current_app.config.get('STRIPE_PUBLISHABLE_KEY', '')

    return render_template(
        'checkout.html',
        pending=pending,
        apartment=apartment,
        nights=nights,
        base_total=base_total,
        total=total_price,               
        stripe_publishable_key=stripe_pub,
        check_in=check_in,
        check_out=check_out,
    )

@bp.route('/process-payment', methods=['POST'])
def process_payment():
    method = request.form.get('payment_method')
    
    if method == 'stripe':
        return create_checkout_session()
        
    elif method == 'wire_transfer':
            pending = session.get('pending_reservation')
            if not pending:
                flash(_('Session expired. Please try again.'), 'danger')
                return redirect(url_for('routes.reserve'))
            
            apartment = get_apartment()
            check_in_dt = date.fromisoformat(pending['check_in'])
            check_out_dt = date.fromisoformat(pending['check_out'])
            
            # Fallback sicuro sul prezzo dinamico calcolato se total_price manca
            base_rate = apartment.price_per_night if apartment else 0
            num_guests = int(pending['num_guests'])
            fallback_total = calculate_dynamic_total(check_in_dt, check_out_dt, num_guests=num_guests, base_rate=base_rate)
            total_price = pending.get('total_price', fallback_total)
            
            new_reservation = Reservation(
                guest_name=pending['guest_name'],
                guest_email=pending['guest_email'],
                check_in=check_in_dt,
                check_out=check_out_dt,
                num_guests=int(pending['num_guests']),
                status='pending',
                source='direct',
                total_price=total_price,
                coupon_code=pending.get('coupon_code'),
                payment_status='unpaid',
                payment_method='wire_transfer',
                cancel_token=secrets.token_urlsafe(32)
            )
            
            db.session.add(new_reservation)
            db.session.commit()
            
            # Send pending payment confirmation email
            send_pending_payment_email(new_reservation)
            
            session['completed_wire_res_id'] = new_reservation.id
            session['completed_wire_total'] = new_reservation.total_price
            
            session.pop('pending_reservation', None)
            return redirect(url_for('routes.wire_transfer_instructions'))

    return redirect(url_for('routes.checkout'))

@bp.route('/checkout/wire-transfer')
def wire_transfer_instructions():
    reservation_id = session.get('completed_wire_res_id')
    total = session.get('completed_wire_total')
    
    if not reservation_id:
        return redirect(url_for('routes.home'))
        
    session.pop('completed_wire_res_id', None)
    session.pop('completed_wire_total', None)
    
    return render_template('wire_transfer.html', reservation_id=reservation_id, total=total)

@bp.route('/checkout/create-session', methods=['POST'])
def create_checkout_session():
    pending = session.get('pending_reservation')
    if not pending:
        flash(_('Session expired. Please start again.'), 'warning')
        return redirect(url_for('routes.reserve'))

    apartment = get_apartment() 
    check_in  = date.fromisoformat(pending['check_in'])
    check_out = date.fromisoformat(pending['check_out'])
    nights    = (check_out - check_in).days
    
    # INTEGRATA: Forza il calcolo dinamico se total_price non è presente nel dizionario session
    total_price = pending.get('total_price')
    if total_price is None:
        base_rate = apartment.price_per_night if apartment else 0
        num_guests = int(pending['num_guests'])
        total_price = calculate_dynamic_total(check_in, check_out, num_guests=num_guests, base_rate=base_rate)

    total_cents = int(total_price * 100)

    if not is_available(check_in, check_out):
        session.pop('pending_reservation', None)
        flash(_('Sorry, those dates were just booked. Please choose again.'), 'danger')
        return redirect(url_for('routes.reserve'))

    try:
        stripe.api_key = current_app.config['STRIPE_SECRET_KEY']
        base_url = current_app.config.get('BASE_URL', request.host_url.rstrip('/'))
        payment_method_types = ['card', 'paypal', 'klarna', 'satispay', 'revolut_pay', 'amazon_pay']

        checkout_session = stripe.checkout.Session.create(
            payment_method_types=payment_method_types,
            line_items=[{
                'price_data': {
                    'currency': 'eur',
                    'unit_amount': total_cents,
                    'product_data': {
                        'name': f"{apartment.name if apartment else 'Apartment'} — {nights} night{'s' if nights != 1 else ''}",
                        'description': f"Check-in: {check_in} / Check-out: {check_out}" + (f" (Promo: {pending['coupon_code']})" if pending.get('coupon_code') else ""),
                    },
                },
                'quantity': 1,
            }],
            mode='payment',
            customer_email=pending['guest_email'],
            success_url=base_url + url_for('routes.payment_success') + '?session_id={CHECKOUT_SESSION_ID}',
            cancel_url=base_url + url_for('routes.checkout'),
            metadata={
                'guest_name':  pending['guest_name'],
                'guest_email': pending['guest_email'],
                'check_in':    pending['check_in'],
                'check_out':   pending['check_out'],
                'num_guests':  str(pending['num_guests']),
                'coupon_code': pending.get('coupon_code', ''), 
                'total_price': str(total_price)
            }
        )
        return redirect(checkout_session.url, code=303)

    except Exception as exc:
        current_app.logger.error('Stripe error: %s', exc)
        flash(_('Payment provider error. Please try again later.'), 'danger')
        return redirect(url_for('routes.checkout'))
    


@bp.route('/payment/success')
def payment_success():
    session_id = request.args.get('session_id')
    reservation = None

    if session_id:
        try:
            stripe.api_key = current_app.config['STRIPE_SECRET_KEY']
            cs = stripe.checkout.Session.retrieve(session_id)
            pi_id = cs.payment_intent

            if pi_id:
                reservation = Reservation.query.filter_by(
                    stripe_payment_intent_id=pi_id
                ).first()

            if not reservation and cs.payment_status == 'paid':
                reservation = _create_reservation_from_stripe(cs)

        except Exception as exc:
            current_app.logger.error('payment_success lookup error: %s', exc)

    session.pop('pending_reservation', None)
    return render_template('booking_confirmed.html', reservation=reservation)


def _create_reservation_from_stripe(cs) -> Reservation:
    data = cs.to_dict() if hasattr(cs, 'to_dict') else cs
    pi_id = data.get('payment_intent') or f"stripe_session_{data.get('id')}"
    
    existing = Reservation.query.filter_by(stripe_payment_intent_id=pi_id).first()
    if existing:
        return existing

    meta = data.get('metadata') or {}
    guest_name  = meta.get('guest_name', 'Guest')
    guest_email = data.get('customer_email') or meta.get('guest_email') or 'info@myapartment.com'
    
    try:
        check_in  = date.fromisoformat(meta.get('check_in'))
        check_out = date.fromisoformat(meta.get('check_out'))
    except (TypeError, ValueError):
        pending = session.get('pending_reservation') or {}
        check_in  = date.fromisoformat(pending.get('check_in', date.today().isoformat()))
        check_out = date.fromisoformat(pending.get('check_out', (date.today() + timedelta(days=1)).isoformat()))
    
    # Determine absolute system costs safely across fallback profiles
    try:
        total_price = float(meta.get('total_price'))
    except (TypeError, ValueError):
        apartment = get_apartment()
        num_guests = int(meta.get('num_guests', 2))
        total_price = calculate_dynamic_total(check_in, check_out, num_guests=num_guests, base_rate=apartment.price_per_night) if apartment else 0

    reservation = Reservation(
        guest_name               = guest_name,
        guest_email              = guest_email,
        check_in                 = check_in,
        check_out                = check_out,
        num_guests               = int(meta.get('num_guests', 1)),
        status                   = 'confirmed',
        source                   = 'direct',
        cancel_token             = secrets.token_urlsafe(32),
        total_price              = total_price,
        coupon_code              = meta.get('coupon_code') if meta.get('coupon_code') else None, # Saves applied promo code string via Stripe Webhook payload pipeline
        payment_status           = 'paid' if data.get('payment_status') == 'paid' else 'unpaid',
        payment_method           = 'stripe',
        stripe_payment_intent_id = pi_id,
    )
    
    db.session.add(reservation)
    db.session.commit()
    
    try:
        _send_confirmation_emails(reservation)
    except Exception as exc:
        current_app.logger.error('Failed to send confirmation email: %s', exc)

    return reservation


@bp.route('/stripe/webhook', methods=['POST'])
@csrf.exempt
def stripe_webhook():
    payload = request.data
    sig_header = request.headers.get('Stripe-Signature')
    endpoint_secret = current_app.config['STRIPE_WEBHOOK_SECRET']

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, endpoint_secret)
    except Exception:
        return "Invalid signature", 400

    if event.type == 'checkout.session.completed':
        cs = event.data.object
        data = cs.to_dict() if hasattr(cs, 'to_dict') else cs
        
        if data.get('payment_status') == 'paid':
            try:
                _create_reservation_from_stripe(cs)
            except Exception as e:
                current_app.logger.error("Error creating reservation: %s", e)
                return "Internal database error", 500

    return "Success", 200

@bp.route("/booking/confirmed/<int:reservation_id>")
def booking_confirmed(reservation_id):
    reservation = Reservation.query.get_or_404(reservation_id)
    return render_template("booking_confirmed.html", reservation=reservation)


@bp.route("/cancel/<token>")
def cancel_reservation(token):
    reservation = Reservation.query.filter_by(cancel_token=token).first_or_404()
    today = date.today()

    if reservation.status == "cancelled":
        return render_template(
            "cancellation_result.html",
            success=False,
            message="This reservation has already been cancelled."
        )

    if reservation.status != "confirmed":
        return render_template(
            "cancellation_result.html",
            success=False,
            message="This reservation cannot be cancelled."
        )

    if today >= reservation.check_in:
        return render_template(
            "cancellation_result.html",
            success=False,
            message="Cancellation is no longer possible after check-in."
        )

    # Calculate refund percentage based on cancellation policy
    refund_percentage = calculate_refund_percentage(reservation.check_in)
    refund_amount = round(reservation.total_price * refund_percentage, 2)
    
    refund_failed_warning = False
    refund_issued = False
    
    if reservation.stripe_payment_intent_id and refund_amount > 0:
        try:
            stripe.api_key = current_app.config['STRIPE_SECRET_KEY']
            stripe.Refund.create(
                payment_intent=reservation.stripe_payment_intent_id,
                amount=int(refund_amount * 100),  # Amount in cents
                reason='requested_by_customer'
            )
            current_app.logger.info(f"Stripe Refund issued for Guest Cancel: Res #{reservation.id}, Amount: €{refund_amount} ({int(refund_percentage * 100)}%)")
            refund_issued = True
            
        except stripe.error.StripeError as e:
            current_app.logger.error(f"Stripe refund transaction failed: {str(e)}")
            refund_failed_warning = True

    reservation.status = "cancelled"
    db.session.commit()

    # Send cancellation emails to guest and admin using Brevo API
    send_cancellation_emails(reservation, refund_failed_warning, refund_percentage, refund_amount)

    # Build UI message based on refund percentage
    if refund_percentage == 1.0:
        refund_text = "A full refund (100%) has been issued back to your payment card."
    elif refund_percentage == 0.5:
        refund_text = "A partial refund (50%) has been issued back to your payment card."
    else:
        refund_text = "No refund is available per our cancellation policy (cancelled within 7 days of check-in)."

    if refund_failed_warning:
        refund_text += " However, there was an issue processing your automatic refund. We will review it manually."

    final_ui_message = "Your reservation has been cancelled successfully. " + refund_text

    return render_template(
        "cancellation_result.html",
        success=True,
        message=final_ui_message
    )

@bp.route('/review-and-pay')
def review_and_pay():
    booking_data = session.get('booking_data')
    if not booking_data:
        return redirect(url_for('routes.index'))
        
    # FIX: Read the pre-calculated, discounted total price from the session!
    total_to_charge = booking_data['total_price']
    
    # When sending total_to_charge to Stripe, remember to convert to cents:
    # int(total_to_charge * 100)
    
    return render_template('review_and_pay.html', booking=booking_data, total=total_to_charge)

def get_payment_summary(reservation):
    if reservation.payment_method == 'stripe':
        return f"Paid via Stripe (Total: €{reservation.total_price:.2f})"
    elif reservation.payment_method == 'iban':
        return f"Pending Bank Transfer (Total: €{reservation.total_price:.2f})"
    elif reservation.payment_method == 'cash':
        return f"Cash on Arrival (Total: €{reservation.total_price:.2f})"
    return f"Total: €{reservation.total_price:.2f}"

# ── Contact ───────────────────────────────────────────────────────────────────

@bp.route('/contact', methods=['GET', 'POST'])
def contact():
    form = ContactForm()
    if form.validate_on_submit():
        mail.send(Message(
            subject=f"New contact message from {form.name.data}",
            recipients=[current_app.config['ADMIN_EMAIL']],
            body=(
                f"Name: {form.name.data}\n"
                f"Email: {form.email.data}\n\n"
                f"Message:\n{form.message.data}"
            )
        ))
        flash('Your message has been sent successfully!', 'success')
        return redirect(url_for('routes.contact'))
    return render_template('contact.html', form=form)


@bp.route('/testimonial/submit', methods=['GET', 'POST'])
@csrf.exempt
def submit_testimonial():
    form = TestimonialForm()
    if form.validate_on_submit():
        testimonial = Testimonial(
            guest_name=form.guest_name.data,
            guest_location=form.guest_location.data,
            rating=form.rating.data,
            content=form.content.data,
            stay_date=form.stay_date.data,
            source='direct',
            is_published=False
        )
        db.session.add(testimonial)
        db.session.commit()
        flash(_('Thank you for your review! It will be published after moderation.'), 'success')
        return redirect(url_for('routes.home'))
    if form.errors:
        for field, errors in form.errors.items():
            for error in errors:
                flash(f'{field}: {error}', 'danger')
    return render_template('apartment.html', apartment=get_apartment(), testimonials=get_testimonials(), form=form)


# ── iCal export ───────────────────────────────────────────────────────────────

@bp.route("/ical/apartment.ics")
def export_ical():
    try:
        reservations = Reservation.query.filter(
            Reservation.check_out > Reservation.check_in
        ).all()

        lines = [
            "BEGIN:VCALENDAR",
            "VERSION:2.0",
            "PRODID:-//Lotto235 Garbatella//Booking Calendar//EN",
            "CALSCALE:GREGORIAN",
            "METHOD:PUBLISH",
        ]

        status_map = {
            "confirmed": "CONFIRMED",
            "pending": "TENTATIVE",
            "cancelled": "CANCELLED",
        }

        for r in reservations:
            # Skip if missing required dates
            if not r.check_in or not r.check_out:
                continue
            
            guest_name = (r.guest_name or "Reserved").replace(",", "\\,").replace(";", "\\;")
            source = (r.source or "Direct").replace(",", "\\,")
            summary = f"{guest_name} ({source})"
            
            description_parts = [
                f"Guest: {r.guest_name or 'N/A'}",
                f"Source: {r.source or 'Direct'}",
                f"Status: {r.status or 'pending'}",
            ]
            if r.guest_email:
                description_parts.append(f"Email: {r.guest_email}")
            
            # Escape newlines and special chars for iCal
            description = "\\n".join(description_parts)

            lines.extend([
                "BEGIN:VEVENT",
                f"UID:{r.id}@lotto235garbatella.it",
                f"DTSTAMP:{datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')}",
                f"DTSTART;VALUE=DATE:{r.check_in.strftime('%Y%m%d')}",
                f"DTEND;VALUE=DATE:{r.check_out.strftime('%Y%m%d')}",
                f"SUMMARY:{summary}",
                f"DESCRIPTION:{description}",
                f"STATUS:{status_map.get(r.status, 'CONFIRMED')}",
                "END:VEVENT",
            ])

        lines.append("END:VCALENDAR")

        return Response(
            "\r\n".join(lines),
            mimetype="text/calendar; charset=utf-8",
            headers={
                "Content-Disposition": "inline; filename=apartment.ics",
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Pragma": "no-cache",
                "Expires": "0",
            }
        )
    except Exception as e:
        current_app.logger.error(f"iCal export error: {e}")
        # Return minimal valid iCal on error
        lines = [
            "BEGIN:VCALENDAR",
            "VERSION:2.0",
            "PRODID:-//Lotto235 Garbatella//Booking Calendar//EN",
            "CALSCALE:GREGORIAN",
            "METHOD:PUBLISH",
            "END:VCALENDAR",
        ]
        return Response(
            "\r\n".join(lines),
            mimetype="text/calendar; charset=utf-8",
            status=500,
            headers={"Cache-Control": "no-cache"}
        )


# ── Admin dashboard ───────────────────────────────────────────────────────────

# 1. MAIN HUB (Overview Dashboard)
@bp.route('/admin')
@login_required
def admin_dashboard():
    if not current_user.is_admin: 
        abort(403)
        
    status_filter = request.args.get('status', 'all')
    query = Reservation.query.order_by(Reservation.check_in.desc())
    if status_filter != 'all':
        query = query.filter(Reservation.status == status_filter)
        
    stats = {
        'total':     Reservation.query.count(),
        'confirmed': Reservation.query.filter_by(status='confirmed').count(),
        'pending':   Reservation.query.filter_by(status='pending').count(),
        'cancelled': Reservation.query.filter_by(status='cancelled').count(),
        'testimonials_total':     Testimonial.query.count(),
        'testimonials_published': Testimonial.query.filter_by(is_published=True).count(),
        'testimonials_pending':   Testimonial.query.filter_by(is_published=False).count(),
        'testimonials_featured':  Testimonial.query.filter_by(is_featured=True).count(),
    }
    
    feeds = ICalFeed.query.order_by(ICalFeed.source).all()
    
    return render_template(
        'admin_dashboard.html', 
        reservations=query.all(), 
        status_filter=status_filter, 
        stats=stats, 
        feeds=feeds
    )

# 2. VISUAL CALENDAR MAP PAGE
@bp.route('/admin/calendar')
@login_required
def admin_calendar():
    if not current_user.is_admin: 
        abort(403)
    return render_template('admin_calendar.html')

# 3. SETTINGS & PRICING PAGE
@bp.route('/admin/pricing', methods=['GET', 'POST'])
@login_required
def admin_pricing():
    if not current_user.is_admin: 
        abort(403)
        
    # Attempt to fetch the apartment row; create a default if database is empty
    apartment = Apartment.query.first()
    if not apartment:
        apartment = Apartment(price_per_night=130.00)
        db.session.add(apartment)
        db.session.commit()
    
    if request.method == 'POST':
        new_price = request.form.get('price_per_night')
        if new_price:
            try:
                apartment.price_per_night = float(new_price)
                db.session.commit()
                flash('Nightly base rate updated successfully!', 'success')
            except ValueError:
                flash('Invalid price format entered.', 'danger')
            return redirect(url_for('routes.admin_pricing'))

    all_coupons = Coupon.query.all()
    return render_template('admin_pricing.html', apartment=apartment, coupons=all_coupons)


# ── Smart Access Admin Page ────────────────────────────────────────────────────
@bp.route('/admin/smart-access', methods=['GET', 'POST'])
@login_required
def admin_smart_access():
    if not current_user.is_admin:
        abort(403)

    apartment = Apartment.query.first()
    if not apartment:
        apartment = Apartment(price_per_night=130.00)
        db.session.add(apartment)
        db.session.commit()

    if request.method == 'POST':
        apartment.shelly_enabled = bool(request.form.get('shelly_enabled'))
        apartment.shelly_host = request.form.get('shelly_host', '').strip() or None
        apartment.shelly_auth_key = request.form.get('shelly_auth_key', '').strip() or None
        apartment.shelly_relay_channel = request.form.get('shelly_relay_channel', type=int) or 0
        apartment.shelly_pulse_duration = request.form.get('shelly_pulse_duration', type=int) or 3

        apartment.nuki_enabled = bool(request.form.get('nuki_enabled'))
        apartment.nuki_smartlock_id = request.form.get('nuki_smartlock_id', '').strip() or None
        apartment.nuki_web_token = request.form.get('nuki_web_token', '').strip() or None
        apartment.nuki_web_base_url = request.form.get('nuki_web_base_url', '').strip() or 'https://api.nuki.io'
        apartment.nuki_unlock_action = request.form.get('nuki_unlock_action', 'unlock')

        apartment.whatsapp_number = request.form.get('whatsapp_number', '').strip() or None
        apartment.whatsapp_default_message = request.form.get('whatsapp_default_message', '').strip() or None

        db.session.commit()
        flash(_('Smart Access & WhatsApp settings saved!'), 'success')
        return redirect(url_for('routes.admin_smart_access'))

    return render_template('admin_smart_access.html', apartment=apartment)


# ── Trust Badges & Widgets Admin Page ─────────────────────────────────────────
@bp.route('/admin/trust-badges', methods=['GET', 'POST'])
@login_required
def admin_trust_badges():
    if not current_user.is_admin:
        abort(403)

    apartment = Apartment.query.first()
    if not apartment:
        apartment = Apartment(price_per_night=130.00)
        db.session.add(apartment)
        db.session.commit()

    if request.method == 'POST':
        # Review platform IDs (for widget embeds)
        apartment.booking_property_id = request.form.get('booking_property_id', '').strip() or None
        apartment.airbnb_listing_id = request.form.get('airbnb_listing_id', '').strip() or None
        apartment.google_place_id = request.form.get('google_place_id', '').strip() or None
        apartment.tripadvisor_location_id = request.form.get('tripadvisor_location_id', '').strip() or None
        apartment.vrbo_listing_id = request.form.get('vrbo_listing_id', '').strip() or None

        # Custom badges
        for i in [1, 2, 3]:
            setattr(apartment, f'custom_badge_{i}_image', request.form.get(f'custom_badge_{i}_image', '').strip() or None)
            setattr(apartment, f'custom_badge_{i}_link', request.form.get(f'custom_badge_{i}_link', '').strip() or None)
            setattr(apartment, f'custom_badge_{i}_alt', request.form.get(f'custom_badge_{i}_alt', '').strip() or None)

        # Display settings
        apartment.show_reviews_in_footer = bool(request.form.get('show_reviews_in_footer'))
        apartment.show_reviews_on_homepage = bool(request.form.get('show_reviews_on_homepage'))
        apartment.show_reviews_on_booking = bool(request.form.get('show_reviews_on_booking'))

        # Official widget embeds
        apartment.booking_widget_js = request.form.get('booking_widget_js', '').strip() or None
        apartment.airbnb_widget_js = request.form.get('airbnb_widget_js', '').strip() or None
        apartment.google_widget_js = request.form.get('google_widget_js', '').strip() or None
        apartment.trustpilot_widget_js = request.form.get('trustpilot_widget_js', '').strip() or None

        db.session.commit()
        flash(_('Trust Badges & Widgets settings saved!'), 'success')
        return redirect(url_for('routes.admin_trust_badges'))

    return render_template('admin_trust_badges.html', apartment=apartment)


# CREATE COUPON ACTION
@bp.route('/admin/coupons/create', methods=['POST'])
@login_required
def admin_create_coupon():
    if not current_user.is_admin: abort(403)
    
    code = request.form.get('coupon_code', '').strip().upper()
    discount_type = request.form.get('discount_type', 'percentage')
    try:
        discount_value = float(request.form.get('discount_value', 0))
    except ValueError:
        flash('Discount value must be a valid number.', 'danger')
        return redirect(url_for('routes.admin_pricing'))

    if not code:
        flash('Voucher string code field cannot be empty.', 'danger')
        return redirect(url_for('routes.admin_pricing'))

    existing = Coupon.query.filter_by(code=code).first()
    if existing:
        flash('A coupon voucher with this exact string code already exists!', 'warning')
        return redirect(url_for('routes.admin_pricing'))

    new_coupon = Coupon(code=code, discount_type=discount_type, discount_value=discount_value, active=True)
    db.session.add(new_coupon)
    db.session.commit()
    
    flash(f'Promotional code "{code}" has been published successfully!', 'success')
    return redirect(url_for('routes.admin_pricing'))


# REMOVE/DELETE COUPON ACTION
@bp.route('/admin/coupons/<int:coupon_id>/delete', methods=['POST'])
@login_required
def admin_delete_coupon(coupon_id):
    if not current_user.is_admin: abort(403)
    
    coupon = Coupon.query.get_or_404(coupon_id)
    db.session.delete(coupon)
    db.session.commit()
    
    flash('Promotional voucher successfully purged from system.', 'success')
    return redirect(url_for('routes.admin_pricing'))

@bp.route('/api/validate-coupon')
def validate_coupon():
    code = request.args.get('code', '').strip().upper()
    try:
        subtotal = float(request.args.get('subtotal', 0))
    except ValueError:
        return jsonify({"valid": False, "message": "Invalid price subtotal format."}), 400

    coupon = Coupon.query.filter_by(code=code, active=True).first()
    if not coupon:
        return jsonify({"valid": False, "message": "Invalid or expired voucher code."})

    new_total = coupon.apply_discount(subtotal)
    return jsonify({
        "valid": True,
        "code": coupon.code,
        "new_total": new_total,
        "message": "Coupon successfully calculated."
    })

# 4. JSON STREAM FOR FULLCALENDAR 
@bp.route('/api/admin/calendar-reservations')
@login_required
def api_calendar_reservations():
    if not current_user.is_admin:
        return jsonify({"error": "Unauthorized"}), 403
        
    reservations = Reservation.query.all()
    events = []
    
    for r in reservations:
        if r.status == 'cancelled':
            color = '#dc3545'
        elif r.source in ['direct', 'stripe']:
            color = '#28a745'
        elif r.source == 'airbnb':
            color = '#ff5a5f'
        elif r.source == 'booking_com':
            color = '#003580'
        else:
            color = '#6c757d'
            
        status_flag = " (CANCELLED)" if r.status == 'cancelled' else ""
        
        events.append({
            "id": r.id,
            "title": f"#{r.id} {r.guest_name}{status_flag}",
            "start": r.check_in.isoformat() if hasattr(r.check_in, 'isoformat') else str(r.check_in),
            "end": r.check_out.isoformat() if hasattr(r.check_out, 'isoformat') else str(r.check_out),
            "backgroundColor": color,
            "borderColor": color,
            "extendedProps": {
                "source": r.source,
                "payment_status": r.payment_status or 'unpaid',
                "status": r.status,
                "total": f"€{r.total_price:.2f}" if r.total_price else "—"
            }
        })
        
    return jsonify(events)

@bp.route('/api/calculate-price')
def api_calculate_price():
    check_in_str = request.args.get('check_in')
    check_out_str = request.args.get('check_out')
    num_guests = request.args.get('num_guests', 2, type=int)
    
    if not check_in_str or not check_out_str:
        return jsonify({"error": "Missing dates"}), 400
        
    try:
        check_in = date.fromisoformat(check_in_str)
        check_out = date.fromisoformat(check_out_str)
        apartment = get_apartment()
        
        base_rate = apartment.price_per_night if apartment else 130.00
        dynamic_total = calculate_dynamic_total(check_in, check_out, num_guests=num_guests, base_rate=base_rate)
        nights = (check_out - check_in).days
        
        return jsonify({
            "nights": nights,
            "total": dynamic_total
        })
    except ValueError:
        return jsonify({"error": "Invalid date format"}), 400
    
@bp.route('/admin/reservations/<int:res_id>/confirm', methods=['POST'])
@login_required
def admin_confirm_reservation(res_id):
    if not current_user.is_admin:
        abort(403)
    reservation = Reservation.query.get_or_404(res_id)
    
    if reservation.status != 'pending':
        flash('Only pending reservations can be confirmed.', 'warning')
    else:
        # Aggiungi questo: il pagamento è ora confermato manualmente
        reservation.status = 'confirmed'
        reservation.payment_status = 'paid' 
        db.session.commit()
        
        # Invio email
        email_sent = send_payment_verified_email(reservation)
        
        if email_sent:
            flash(f'Reservation #{res_id} confirmed and payment marked as PAID.', 'success')
        else:
            flash(f'Reservation #{res_id} confirmed, but email failed.', 'warning')
            
    return redirect(url_for('routes.admin_dashboard'))

@bp.route('/admin/reservations/<int:res_id>/cancel', methods=['POST'])
@login_required
def admin_cancel_reservation(res_id):
    """Native interactive admin dashboard cancellation point (Triggered via standard POST forms)."""
    if not current_user.is_admin:
        abort(403)
    reservation = Reservation.query.get_or_404(res_id)
    if reservation.status == 'cancelled':
        flash('Reservation is already cancelled.', 'warning')
    else:
        # Calculate refund percentage based on cancellation policy
        refund_percentage = calculate_refund_percentage(reservation.check_in)
        refund_amount = round(reservation.total_price * refund_percentage, 2)
        
        refund_failed_warning = False
        if reservation.stripe_payment_intent_id and not reservation.stripe_payment_intent_id.startswith("test_bypass_") and refund_amount > 0:
            try:
                stripe.api_key = current_app.config['STRIPE_SECRET_KEY']
                stripe.Refund.create(
                    payment_intent=reservation.stripe_payment_intent_id,
                    amount=int(refund_amount * 100),  # Amount in cents
                    reason='requested_by_customer'
                )
                current_app.logger.info(f"Stripe Refund issued by Admin for Res #{reservation.id}, Amount: €{refund_amount} ({int(refund_percentage * 100)}%)")
            except stripe.error.StripeError as e:
                current_app.logger.error(f"Admin Stripe refund failed: {str(e)}")
                refund_failed_warning = True

        reservation.status = 'cancelled'
        reservation.payment_status = 'refunded' if not refund_failed_warning and reservation.stripe_payment_intent_id and refund_amount > 0 else 'cancelled'
        db.session.commit()
        
        # Send cancellation emails to guest and admin
        send_cancellation_emails(reservation, refund_failed_warning, refund_percentage, refund_amount)
        
        # Build flash message based on refund percentage
        if refund_percentage == 1.0:
            refund_text = "fully refunded (100%)"
        elif refund_percentage == 0.5:
            refund_text = "partially refunded (50%)"
        else:
            refund_text = "no refund (cancelled within 7 days of check-in)"
            
        if refund_failed_warning:
            flash(f'Reservation #{res_id} cancelled locally, but Stripe refund failed.', 'warning')
        else:
            flash(f'Reservation #{res_id} cancelled and {refund_text}.', 'success')
            
    return redirect(url_for('routes.admin_dashboard'))

# ── Unique Blueprint Fixed Token Route ────────────────────────────────────────

@bp.route('/admin/cancel-booking/<token>', methods=['GET'])
def admin_cancel_via_token(token):
    """Secure unauthenticated admin entry point (Triggered via link embedded in email alerts)."""
    reservation = Reservation.query.filter_by(cancel_token=token).first_or_404()

    if reservation.status == 'cancelled':
        flash("This reservation is already cancelled.", "warning")
        return redirect(url_for('routes.admin_dashboard'))

    try:
        refund_failed_warning = False
        # Syncing correct structural database reference matching models file schema block
        pi_id = reservation.stripe_payment_intent_id
        
        # Calculate refund percentage based on cancellation policy
        refund_percentage = calculate_refund_percentage(reservation.check_in)
        refund_amount = round(reservation.total_price * refund_percentage, 2)

        if pi_id and not pi_id.startswith("test_bypass_") and refund_amount > 0:
            print(f"💰 STRIPE: Initiating email-triggered token refund for intent {pi_id}...", flush=True)
            try:
                stripe.api_key = current_app.config['STRIPE_SECRET_KEY']
                stripe.Refund.create(
                    payment_intent=pi_id,
                    amount=int(refund_amount * 100),  # Amount in cents
                    reason="requested_by_customer"
                )
                print(f"✅ STRIPE: Token refund complete! Amount: €{refund_amount} ({int(refund_percentage * 100)}%)", flush=True)
            except stripe.error.StripeError as e:
                print(f"❌ STRIPE REFUND ERROR: {str(e)}", flush=True)
                refund_failed_warning = True

        reservation.status = 'cancelled'
        reservation.payment_status = 'refunded' if not refund_failed_warning and pi_id and refund_amount > 0 else 'cancelled'
        db.session.commit()
        
        # Send cancellation emails to guest and admin
        send_cancellation_emails(reservation, refund_failed_warning, refund_percentage, refund_amount)
        
        if refund_failed_warning:
            flash(f"Booking for {reservation.guest_name} cancelled locally, but Stripe refund failed.", "danger")
        else:
            if refund_percentage == 1.0:
                refund_text = "fully refunded (100%)"
            elif refund_percentage == 0.5:
                refund_text = "partially refunded (50%)"
            else:
                refund_text = "no refund (cancelled within 7 days of check-in)"
            flash(f"Success! Booking for {reservation.guest_name} has been cancelled and {refund_text}.", "success")
        
    except Exception as exc:
        db.session.rollback()
        print(f"❌ CANCELLATION EXCEPTION: {str(exc)}", flush=True)
        flash("An internal system error occurred during cancellation.", "danger")

    return redirect(url_for('routes.admin_dashboard'))


# ── iCal feed management ──────────────────────────────────────────────────────

@bp.route('/admin/feeds/add', methods=['GET', 'POST'])
@login_required
def add_feed():
    if not current_user.is_admin:
        abort(403)
    form = ICalFeedForm()
    if form.validate_on_submit():
        feed = ICalFeed(source=form.source.data, url=form.url.data, active=True)
        db.session.add(feed)
        db.session.commit()
        flash(f'Feed "{form.source.data}" added.', 'success')
        return redirect(url_for('routes.admin_dashboard'))
    return render_template('admin_feed_form.html', form=form, title='Add iCal Feed')


@bp.route('/admin/feeds/<int:feed_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_feed(feed_id):
    if not current_user.is_admin:
        abort(403)
    feed = ICalFeed.query.get_or_404(feed_id)
    form = ICalFeedForm(obj=feed)
    if form.validate_on_submit():
        feed.source = form.source.data
        feed.url    = form.url.data
        feed.active = form.active.data
        db.session.commit()
        flash('Feed updated.', 'success')
        return redirect(url_for('routes.admin_dashboard'))
    return render_template('admin_feed_form.html', form=form, title='Edit iCal Feed')


@bp.route('/admin/feeds/<int:feed_id>/delete', methods=['POST'])
@login_required
def delete_feed(feed_id):
    if not current_user.is_admin:
        abort(403)
    feed = ICalFeed.query.get_or_404(feed_id)
    db.session.delete(feed)
    db.session.commit()
    flash('Feed deleted.', 'success')
    return redirect(url_for('routes.admin_dashboard'))


@bp.route('/admin/feeds/sync', methods=['POST'])
@login_required
def sync_feeds_now():
    if not current_user.is_admin:
        abort(403)
    added, removed, errors = sync_all_feeds() # Assumes this background function exists in your system
    if errors:
        flash(f'Sync finished with warnings: {"; ".join(errors)}', 'warning')
    else:
        flash(f'Sync complete! Added {added}, removed {removed}.', 'success')
    return redirect(url_for('routes.admin_dashboard'))


# ── Testimonial Admin ────────────────────────────────────────────────────────────
@bp.route('/admin/testimonials')
@login_required
def admin_testimonials():
    if not current_user.is_admin:
        abort(403)
    testimonials = Testimonial.query.order_by(Testimonial.created_at.desc()).all()
    stats = {
        'testimonials_total':     Testimonial.query.count(),
        'testimonials_published': Testimonial.query.filter_by(is_published=True).count(),
        'testimonials_pending':   Testimonial.query.filter_by(is_published=False).count(),
        'testimonials_featured':  Testimonial.query.filter_by(is_featured=True).count(),
    }
    return render_template('admin_testimonials.html', testimonials=testimonials, stats=stats)


@bp.route('/admin/testimonials/<int:testimonial_id>/publish', methods=['POST'])
@login_required
def admin_toggle_testimonial_publish(testimonial_id):
    if not current_user.is_admin:
        abort(403)
    testimonial = Testimonial.query.get_or_404(testimonial_id)
    testimonial.is_published = not testimonial.is_published
    db.session.commit()
    flash(f'Testimonial {"published" if testimonial.is_published else "unpublished"} successfully.', 'success')
    return redirect(url_for('routes.admin_testimonials'))


@bp.route('/admin/testimonials/<int:testimonial_id>/feature', methods=['POST'])
@login_required
def admin_toggle_testimonial_feature(testimonial_id):
    if not current_user.is_admin:
        abort(403)
    testimonial = Testimonial.query.get_or_404(testimonial_id)
    testimonial.is_featured = not testimonial.is_featured
    db.session.commit()
    flash(f'Testimonial {"featured" if testimonial.is_featured else "unfeatured"} successfully.', 'success')
    return redirect(url_for('routes.admin_testimonials'))


@bp.route('/admin/testimonials/<int:testimonial_id>/delete', methods=['POST'])
@login_required
def admin_delete_testimonial(testimonial_id):
    if not current_user.is_admin:
        abort(403)
    testimonial = Testimonial.query.get_or_404(testimonial_id)
    db.session.delete(testimonial)
    db.session.commit()
    flash('Testimonial deleted successfully.', 'success')
    return redirect(url_for('routes.admin_testimonials'))


# ── Auth ──────────────────────────────────────────────────────────────────────

@bp.route('/login', methods=['GET', 'POST'])
@limiter.limit("5 per 15 minutes")
def login():
    form = LoginForm()
    if form.validate_on_submit():
        user = User.query.filter_by(username=form.username.data).first()
        if user and user.check_password(form.password.data):
            login_user(user, remember=form.remember.data)
            return redirect(url_for('routes.admin_dashboard'))
        else:
            flash('Login unsuccessful. Check username/password.', 'danger')
    return render_template('login.html', form=form)


@bp.route('/logout')
def logout():
    logout_user()
    return redirect(url_for('routes.home'))


@bp.route('/sitemap.xml', methods=['GET'])
def sitemap():
    """Generate dynamic sitemap with all public pages"""
    base_url = current_app.config.get('BASE_URL', 'https://www.lotto235garbatella.it').rstrip('/')
    
    # Static public pages
    static_pages = [
        ('/', 'weekly', 1.0),
        ('/reserve', 'monthly', 0.9),
        ('/faq', 'monthly', 0.7),
        ('/terms', 'yearly', 0.5),
        ('/cancellation-policy', 'yearly', 0.5),
        ('/refund-policy', 'yearly', 0.5),
        ('/house-rules', 'yearly', 0.5),
        ('/privacy', 'yearly', 0.5),
        ('/food_recommendations', 'monthly', 0.6),
        ('/attractions', 'monthly', 0.6),
        ('/contact', 'monthly', 0.5),
    ]
    
    # Generate URLs
    urls = []
    for path, changefreq, priority in static_pages:
        urls.append(f'        <url><loc>{base_url}{path}</loc><changefreq>{changefreq}</changefreq><priority>{priority}</priority></url>')
    
    # Add language variants for main pages
    languages = ['en', 'it', 'de', 'fr', 'es']
    main_pages = ['/', '/reserve', '/faq', '/contact']
    for lang in languages:
        if lang != 'en':  # English is default
            for path in main_pages:
                lang_path = f'/{lang}{path}' if path != '/' else f'/{lang}/'
                urls.append(f'        <url><loc>{base_url}{lang_path}</loc><changefreq>monthly</changefreq><priority>0.6</priority></url>')
    
    xml_content = f"""<?xml version="1.0" encoding="UTF-8"?>
    <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
{chr(10).join(urls)}
    </urlset>"""
    
    return Response(xml_content, mimetype='text/xml')


# ── Italian Compliance Routes ────────────────────────────────────────────────

@bp.route('/admin/compliance')
@login_required
def compliance_dashboard():
    """Compliance dashboard showing status overview"""
    if not current_user.is_admin:
        abort(403)
    
    today = date.today()
    
    # Questura stats
    questura_pending = Reservation.query.filter(
        Reservation.questura_status.in_([None, 'pending']),
        Reservation.status == 'confirmed',
        Reservation.check_in <= today
    ).count()
    
    questura_rejected = Reservation.query.filter_by(questura_status='rejected').count()
    questura_accepted = Reservation.query.filter_by(questura_status='accepted').count()
    
    # Upcoming check-ins needing data
    upcoming = Reservation.query.filter(
        Reservation.status == 'confirmed',
        Reservation.check_in >= today,
        Reservation.check_in <= today + timedelta(days=7)
    ).all()
    
    needing_data = [r for r in upcoming if not r.questura_ready()]
    
    # Tourist tax stats
    apt = Apartment.query.first()
    tax_service = get_tax_service(apt) if apt else None
    current_month_tax = 0
    if tax_service:
        report = tax_service.generate_detailed_report(today.year, today.month)
        current_month_tax = report['total_tax']
    
    # Configuration status
    config_keys = [
        'questura_wsdl_url', 'questura_username', 'questura_password',
        'questura_cert_path', 'questura_cert_password', 'questura_protocol_number'
    ]
    config_status = {k: bool(ComplianceConfig.get(k)) for k in config_keys}
    
    return render_template('admin_compliance.html',
        questura_pending=questura_pending,
        questura_rejected=questura_rejected,
        questura_accepted=questura_accepted,
        needing_data=needing_data,
        current_month_tax=current_month_tax,
        config_status=config_status,
        today=today
    )


@bp.route('/admin/compliance/questura')
@login_required
def questura_list():
    """List all reservations with Questura status"""
    if not current_user.is_admin:
        abort(403)
    
    status_filter = request.args.get('status', 'all')
    page = request.args.get('page', 1, type=int)
    
    query = Reservation.query.order_by(Reservation.check_in.desc())
    
    if status_filter != 'all':
        query = query.filter(Reservation.questura_status == status_filter)
    
    reservations = query.paginate(page=page, per_page=25, error_out=False)
    
    return render_template('admin_questura.html',
        reservations=reservations,
        status_filter=status_filter
    )


@bp.route('/admin/compliance/questura/submit', methods=['POST'])
@login_required
def questura_submit():
    """Manually trigger Questura submission for selected reservations"""
    if not current_user.is_admin:
        abort(403)
    
    reservation_ids = request.json.get('reservation_ids', [])
    if not reservation_ids:
        return jsonify({'error': 'No reservations selected'}), 400
    
    from app.tasks.compliance import retry_failed_questura
    task = retry_failed_questura.delay(reservation_ids)
    return jsonify({'task_id': task.id, 'message': 'Submission queued'})


@bp.route('/admin/compliance/questura/run-daily', methods=['POST'])
@login_required
def questura_run_daily():
    """Manually trigger daily Questura submission"""
    if not current_user.is_admin:
        abort(403)
    
    from app.tasks.compliance import submit_questura_daily
    task = submit_questura_daily.delay()
    return jsonify({'task_id': task.id, 'message': 'Daily submission queued'})


@bp.route('/admin/compliance/questura/logs')
@login_required
def questura_logs():
    """View Questura submission logs"""
    if not current_user.is_admin:
        abort(403)
    
    page = request.args.get('page', 1, type=int)
    logs = QuesturaLog.query.order_by(QuesturaLog.created_at.desc()).paginate(
        page=page, per_page=50, error_out=False)
    
    return render_template('admin_questura_logs.html', logs=logs)


@bp.route('/admin/compliance/tourist-tax')
@login_required
def tourist_tax():
    """Tourist tax management"""
    if not current_user.is_admin:
        abort(403)
    
    year = request.args.get('year', date.today().year, type=int)
    month = request.args.get('month', date.today().month, type=int)
    
    apt = Apartment.query.first()
    service = get_tax_service(apt)
    report = service.generate_detailed_report(year, month) if service else None
    
    return render_template('admin_tourist_tax.html',
        report=report,
        year=year,
        month=month,
        apt=apt
    )


@bp.route('/admin/compliance/tourist-tax/export')
@login_required
def tourist_tax_export():
    """Download CSV for Roma Capitale"""
    if not current_user.is_admin:
        abort(403)
    
    year = request.args.get('year', date.today().year, type=int)
    month = request.args.get('month', date.today().month, type=int)
    
    apt = Apartment.query.first()
    service = get_tax_service(apt)
    csv_data = service.export_monthly_csv(year, month) if service else ''
    
    output = io.BytesIO()
    output.write(csv_data.encode('utf-8'))
    output.seek(0)
    
    filename = f'tassa_soggiorno_{year}_{month:02d}.csv'
    return send_file(output, as_attachment=True, download_name=filename, mimetype='text/csv')


@bp.route('/admin/compliance/tourist-tax/generate-report', methods=['POST'])
@login_required
def tourist_tax_generate():
    """Manually trigger monthly report generation"""
    if not current_user.is_admin:
        abort(403)
    
    from app.tasks.compliance import generate_monthly_tourist_tax_report
    task = generate_monthly_tourist_tax_report.delay()
    return jsonify({'task_id': task.id, 'message': 'Report generation queued'})


@bp.route('/admin/compliance/tourist-tax/update/<int:reservation_id>', methods=['POST'])
@login_required
def tourist_tax_update_reservation(reservation_id):
    """Manually override tax amount or paid status for a reservation"""
    if not current_user.is_admin:
        abort(403)

    res = Reservation.query.get_or_404(reservation_id)

    nights = request.form.get('nights', type=int)
    guests = request.form.get('num_guests', type=int)
    tax_override = request.form.get('tourist_tax_amount', type=float)
    tax_paid = request.form.get('tourist_tax_paid') == '1'

    if nights is not None and nights > 0:
        res.check_out = res.check_in + timedelta(days=nights)

    if guests is not None and guests > 0:
        res.num_guests = guests

    if tax_override is not None:
        res.tourist_tax_amount = max(0.0, tax_override)

    res.tourist_tax_paid = tax_paid
    db.session.commit()

    flash(f'Reservation #{reservation_id} updated.', 'success')
    return redirect(url_for('routes.tourist_tax', year=request.form.get('year'), month=request.form.get('month')))


@bp.route('/admin/compliance/tourist-tax/toggle-exclude/<int:reservation_id>', methods=['POST'])
@login_required
def tourist_tax_toggle_exclude(reservation_id):
    """Toggle exclusion of a reservation from tourist tax reports"""
    if not current_user.is_admin:
        abort(403)

    res = Reservation.query.get_or_404(reservation_id)
    res.tourist_tax_excluded = not res.tourist_tax_excluded
    db.session.commit()

    status = 'excluded from' if res.tourist_tax_excluded else 'included in'
    flash(f'Reservation #{reservation_id} {status} tourist tax.', 'success')
    return redirect(url_for('routes.tourist_tax', year=request.form.get('year'), month=request.form.get('month')))


@bp.route('/admin/compliance/config')
@login_required
def compliance_config():
    """View/edit compliance configuration"""
    if not current_user.is_admin:
        abort(403)
    
    if request.method == 'POST':
        key = request.form.get('key')
        value = request.form.get('value')
        description = request.form.get('description')
        
        if key and value:
            ComplianceConfig.set(key, value, description)
            flash(f'Configuration "{key}" updated', 'success')
        return redirect(url_for('routes.compliance_config'))
    
    # Get all config (masked)
    configs = ComplianceConfig.query.order_by(ComplianceConfig.key).all()
    masked = []
    for c in configs:
        masked.append({
            'key': c.key,
            'value': '***' if c.value_encrypted else '',
            'description': c.description,
            'updated_at': c.updated_at
        })
    
    return render_template('admin_compliance_config.html', configs=masked, ComplianceConfig=ComplianceConfig)


@bp.route('/admin/compliance/config/set', methods=['POST'])
@login_required
def config_set():
    """API endpoint to set config value"""
    if not current_user.is_admin:
        abort(403)
    
    data = request.get_json()
    key = data.get('key')
    value = data.get('value')
    description = data.get('description')
    
    if not key or value is None:
        return jsonify({'error': 'Missing key or value'}), 400
    
    ComplianceConfig.set(key, value, description)
    return jsonify({'success': True, 'message': f'Config "{key}" updated'})


@bp.route('/checkin/<token>', methods=['GET', 'POST'])
def guest_self_checkin(token):
    """Guest self-service check-in form accessible via unique token"""
    # Find reservation by check-in token
    res = Reservation.query.filter_by(checkin_token=token).first_or_404()
    
    # Check if token is valid
    if res.checkin_token_used:
        return render_template('guest_checkin_done.html', 
            message=_('Check-in già completato per questa prenotazione.'))
    
    if res.status != 'confirmed':
        return render_template('guest_checkin_done.html', 
            message=_('Questa prenotazione non è confermata.'))
    
    # Check if check-in date is within reasonable window (e.g., 30 days before check-in)
    days_until_checkin = (res.check_in - date.today()).days
    if days_until_checkin > 30:
        return render_template('guest_checkin_done.html', 
            message=_('Il check-in è disponibile solo 30 giorni prima dell\'arrivo.'))
    
    if request.method == 'POST':
        # Update guest data from form
        res.guest_surname = request.form.get('guest_surname')
        res.guest_first_name = request.form.get('guest_first_name')
        res.guest_birth_date = datetime.strptime(request.form.get('guest_birth_date'), '%Y-%m-%d').date() \
            if request.form.get('guest_birth_date') else None
        res.guest_birth_place = request.form.get('guest_birth_place')
        res.guest_nationality = request.form.get('guest_nationality')
        res.guest_document_type = request.form.get('guest_document_type')
        res.guest_document_number = request.form.get('guest_document_number')
        res.guest_document_expiry = datetime.strptime(request.form.get('guest_document_expiry'), '%Y-%m-%d').date() \
            if request.form.get('guest_document_expiry') else None
        res.guest_document_country = request.form.get('guest_document_country')
        res.guest_gender = request.form.get('guest_gender')
        
        # Mark check-in as completed
        res.checkin_token_used = True
        res.checkin_completed_at = datetime.utcnow()
        
        db.session.commit()
        
        # Auto-submit to Questura if ready
        if res.questura_ready():
            from app.tasks.compliance import retry_failed_questura
            retry_failed_questura.delay([res.id])
            flash(_('Check-in completato! Dati inviati alle autorità competenti.'), 'success')
        else:
            flash(_('Check-in completato! Grazie per aver fornito i dati.'), 'success')
        
        return redirect(url_for('routes.guest_self_checkin', token=token))
    
    return render_template('guest_self_checkin.html', reservation=res)


@bp.route('/admin/compliance/send-checkin-link', methods=['POST'])
@login_required
def send_checkin_link():
    """Send check-in link to guest via email"""
    if not current_user.is_admin:
        abort(403)
    
    reservation_id = request.json.get('reservation_id')
    if not reservation_id:
        return jsonify({'error': 'Missing reservation_id'}), 400
    
    res = Reservation.query.get_or_404(reservation_id)
    
    if res.status != 'confirmed':
        return jsonify({'error': 'Reservation must be confirmed'}), 400
    
    # Generate check-in token if not exists
    if not res.checkin_token:
        res.checkin_token = secrets.token_urlsafe(32)
        db.session.commit()
    
    # Send email with check-in link
    checkin_url = url_for('routes.guest_self_checkin', token=res.checkin_token, _external=True)
    
    # Use existing email function or create new one
    try:
        from app.services.email_service import send_checkin_email
        send_checkin_email(res, checkin_url)
    except ImportError:
        # Fallback: just return the URL for now
        pass
    
    return jsonify({
        'success': True, 
        'checkin_url': checkin_url,
        'message': 'Check-in link generated'
    })


@bp.route('/admin/compliance/regenerate-checkin-token', methods=['POST'])
@login_required
def regenerate_checkin_token():
    """Regenerate check-in token for a reservation"""
    if not current_user.is_admin:
        abort(403)
    
    reservation_id = request.json.get('reservation_id')
    if not reservation_id:
        return jsonify({'error': 'Missing reservation_id'}), 400
    
    res = Reservation.query.get_or_404(reservation_id)
    
    # Generate new token
    res.checkin_token = secrets.token_urlsafe(32)
    res.checkin_token_used = False
    res.checkin_completed_at = None
    db.session.commit()
    
    checkin_url = url_for('routes.guest_self_checkin', token=res.checkin_token, _external=True)
    
    return jsonify({
        'success': True, 
        'checkin_url': checkin_url,
        'token': res.checkin_token
    })


# ── Guest Access (Gate & Door) ─────────────────────────────────────────────────
@bp.route('/access/<token>')
def guest_access(token):
    """Guest access page - valid only during stay dates"""
    res = Reservation.query.filter_by(access_token=token).first_or_404()
    
    # Check if reservation is confirmed
    if res.status != 'confirmed':
        return render_template('guest_access_denied.html',
            reason=_('Reservation is not confirmed'),
            reservation=res), 403
    
    # Check if current date is within stay period
    today = date.today()
    if today < res.check_in:
        return render_template('guest_access_denied.html',
            reason=_('Access not yet available. Your stay starts on %(date)s', date=res.check_in.strftime('%d/%m/%Y')),
            reservation=res), 403
    
    if today > res.check_out:
        return render_template('guest_access_denied.html',
            reason=_('Access expired. Your stay ended on %(date)s', date=res.check_out.strftime('%d/%m/%Y')),
            reservation=res), 403
    
    # Get apartment for device config
    apt = Apartment.query.first()
    
    return render_template('guest_access.html',
        reservation=res,
        apartment=apt,
        gate_configured=bool(apt and apt.shelly_host),
        door_configured=bool(apt and apt.nuki_smartlock_id and apt.nuki_web_token)
    )


# ── Guest Portal (Check-in + Access Combined) ────────────────────────────────────
@bp.route('/portal/<token>')
def guest_portal(token):
    """Unified guest portal: check-in + gate/door access"""
    res = Reservation.query.filter_by(checkin_token=token).first_or_404()
    
    if res.status != 'confirmed':
        return render_template('guest_access_denied.html',
            reason=_('Reservation is not confirmed'),
            reservation=res), 403
    
    today = date.today()
    apt = Apartment.query.first()
    
    # Show check-in if not completed and within 30 days of check-in
    show_checkin = not res.checkin_token_used and (res.check_in - today).days <= 30
    
    # Show access if check-in completed AND during stay
    show_access = res.checkin_token_used and today >= res.check_in and today <= res.check_out
    
    # Also show access preview if check-in done but not yet stay dates
    access_preview = res.checkin_token_used and today < res.check_in
    
    # Ensure access token exists
    if not res.access_token:
        res.access_token = secrets.token_urlsafe(32)
        db.session.commit()
    
    return render_template('guest_portal.html',
        reservation=res,
        apartment=apt,
        show_checkin=show_checkin,
        show_access=show_access or access_preview,
        gate_configured=bool(apt and apt.shelly_host),
        door_configured=bool(apt and apt.nuki_smartlock_id and apt.nuki_web_token),
        access_token=res.access_token
    )


@bp.route('/api/access/gate/open', methods=['POST'])
def api_gate_open():
    """Open gate via Shelly - token validated via header or query param"""
    token = request.headers.get('X-Access-Token') or request.args.get('token')
    if not token:
        return jsonify({'error': 'Missing access token'}), 401
    
    res = Reservation.query.filter_by(access_token=token).first()
    if not res or res.status != 'confirmed':
        return jsonify({'error': 'Invalid or expired access'}), 403
    
    # Validate stay dates
    today = date.today()
    if today < res.check_in or today > res.check_out:
        return jsonify({'error': 'Access not valid for current date'}), 403
    
    apt = Apartment.query.first()
    if not apt or not apt.shelly_url:
        return jsonify({'error': 'Gate not configured'}), 503
    
    try:
        success, message = trigger_gate_open(apt)
        return jsonify({'success': success, 'message': message})
    except Exception as e:
        current_app.logger.error(f"Gate open error: {e}")
        return jsonify({'error': 'Failed to open gate'}), 500


@bp.route('/api/access/door/open', methods=['POST'])
def api_door_open():
    """Open apartment door via Nuki - token validated via header or query param"""
    token = request.headers.get('X-Access-Token') or request.args.get('token')
    if not token:
        return jsonify({'error': 'Missing access token'}), 401
    
    res = Reservation.query.filter_by(access_token=token).first()
    if not res or res.status != 'confirmed':
        return jsonify({'error': 'Invalid or expired access'}), 403
    
    # Validate stay dates
    today = date.today()
    if today < res.check_in or today > res.check_out:
        return jsonify({'error': 'Access not valid for current date'}), 403
    
    apt = Apartment.query.first()
    if not apt or not apt.nuki_smartlock_id or not apt.nuki_web_token:
        return jsonify({'error': 'Door not configured'}), 503
    
    try:
        success, message = trigger_door_unlock(apt)
        return jsonify({'success': success, 'message': message})
    except Exception as e:
        current_app.logger.error(f"Door open error: {e}")
        return jsonify({'error': 'Failed to open door'}), 500


@bp.route('/admin/access/generate-link', methods=['POST'])
@login_required
def admin_generate_access_link():
    """Generate access link for a confirmed reservation"""
    if not current_user.is_admin:
        abort(403)
    
    reservation_id = request.json.get('reservation_id')
    if not reservation_id:
        return jsonify({'error': 'Missing reservation_id'}), 400
    
    res = Reservation.query.get_or_404(reservation_id)
    
    if res.status != 'confirmed':
        return jsonify({'error': 'Reservation must be confirmed'}), 400
    
    # Generate access token if not exists
    if not res.access_token:
        res.access_token = secrets.token_urlsafe(32)
        db.session.commit()
    
    access_url = url_for('routes.guest_access', token=res.access_token, _external=True)
    
    return jsonify({
        'success': True,
        'access_url': access_url,
        'token': res.access_token,
        'valid_from': res.check_in.isoformat(),
        'valid_until': res.check_out.isoformat()
    })


@bp.route('/admin/access/send-link', methods=['POST'])
@login_required
def admin_send_access_link():
    """Send access link to guest via email"""
    if not current_user.is_admin:
        abort(403)
    
    reservation_id = request.json.get('reservation_id')
    if not reservation_id:
        return jsonify({'error': 'Missing reservation_id'}), 400
    
    res = Reservation.query.get_or_404(reservation_id)
    
    if res.status != 'confirmed':
        return jsonify({'error': 'Reservation must be confirmed'}), 400
    
    if not res.guest_email:
        return jsonify({'error': 'Guest email not available'}), 400
    
    # Generate access token if not exists
    if not res.access_token:
        res.access_token = secrets.token_urlsafe(32)
        db.session.commit()
    
    access_url = url_for('routes.guest_access', token=res.access_token, _external=True)
    
    # Send email
    try:
        from app.services.email_service import send_access_email
        sent = send_access_email(res, access_url)
        if not sent:
            return jsonify({'error': 'Failed to send email'}), 500
    except ImportError:
        return jsonify({'error': 'Email service not available'}), 500
    except Exception as e:
        current_app.logger.error(f"Send access email error: {e}")
        return jsonify({'error': 'Failed to send email'}), 500
    
    return jsonify({
        'success': True,
        'message': f'Access link sent to {res.guest_email}',
        'access_url': access_url
    })


@bp.route('/admin/access/regenerate-token', methods=['POST'])
@login_required
def admin_regenerate_access_token():
    """Regenerate access token for a reservation"""
    if not current_user.is_admin:
        abort(403)
    
    reservation_id = request.json.get('reservation_id')
    if not reservation_id:
        return jsonify({'error': 'Missing reservation_id'}), 400
    
    res = Reservation.query.get_or_404(reservation_id)
    
    # Generate new token
    res.access_token = secrets.token_urlsafe(32)
    db.session.commit()
    
    access_url = url_for('routes.guest_access', token=res.access_token, _external=True)
    
    return jsonify({
        'success': True,
        'access_url': access_url,
        'token': res.access_token,
        'valid_from': res.check_in.isoformat(),
        'valid_until': res.check_out.isoformat()
    })


@bp.route('/admin/smart-access/test-gate', methods=['POST'])
@login_required
def admin_test_gate():
    """Test gate opening via Shelly"""
    if not current_user.is_admin:
        abort(403)
    
    apt = Apartment.query.first()
    if not apt or not apt.shelly_enabled or not apt.shelly_host:
        return jsonify({'error': 'Gate not configured'}), 400
    
    try:
        success, message = trigger_gate_open(apt)
        return jsonify({'success': success, 'message': message})
    except Exception as e:
        current_app.logger.error(f"Test gate error: {e}")
        return jsonify({'error': str(e)}), 500


@bp.route('/admin/smart-access/test-door', methods=['POST'])
@login_required
def admin_test_door():
    """Test door unlocking via Nuki"""
    if not current_user.is_admin:
        abort(403)
    
    apt = Apartment.query.first()
    if not apt or not apt.nuki_enabled or not apt.nuki_smartlock_id or not apt.nuki_web_token:
        return jsonify({'error': 'Door not configured'}), 400
    
    try:
        success, message = trigger_door_unlock(apt)
        return jsonify({'success': success, 'message': message})
    except Exception as e:
        current_app.logger.error(f"Test door error: {e}")
        return jsonify({'error': str(e)}), 500


# ── Guest Communication Templates (for external platforms) ──────────────────────
@bp.route('/admin/communication/guest-message/<int:reservation_id>')
@login_required
def admin_guest_message(reservation_id):
    """Get pre-configured message templates for guest communication"""
    if not current_user.is_admin:
        abort(403)
    
    res = Reservation.query.get_or_404(reservation_id)
    apt = Apartment.query.first()
    
    # Generate tokens if not exist
    if not res.checkin_token:
        res.checkin_token = secrets.token_urlsafe(32)
    if not res.access_token:
        res.access_token = secrets.token_urlsafe(32)
    db.session.commit()
    
    checkin_url = url_for('routes.guest_self_checkin', token=res.checkin_token, _external=True)
    access_url = url_for('routes.guest_access', token=res.access_token, _external=True)
    portal_url = url_for('routes.guest_portal', token=res.checkin_token, _external=True)
    
    # Message templates
    checkin_message = f"""Ciao {res.guest_name},

Grazie per aver prenotato presso {apt.name if apt else 'Lotto 235 Garbatella'}!

Per completare il check-in online (obbligatorio per legge italiana), clicca qui:
{checkin_url}

Il link è valido dal {res.check_in.strftime('%d/%m/%Y')} al {res.check_out.strftime('%d/%m/%Y')}.

Durante il soggiorno potrai aprire il cancello e la porta dell'appartamento da questo link:
{access_url}

Oppure usa il portale unico per tutto:
{portal_url}

A presto!
{apt.name if apt else 'Lotto 235 Garbatella'}"""

    whatsapp_message = f"""Ciao {res.guest_name}! 👋

Grazie per aver prenotato da {apt.name if apt else 'Lotto 235 Garbatella'}!

🔑 *Check-in online (obbligatorio)*:
{checkin_url}

🚪 *Apri cancello e porta* (valido durante il soggiorno):
{access_url}

📱 *Portale unico* (check-in + accessi):
{portal_url}

Disponibile dal {res.check_in.strftime('%d/%m/%Y')} al {res.check_out.strftime('%d/%m/%Y')}.

A presto!"""

    airbnb_message = f"""Hi {res.guest_name},

Thanks for booking at {apt.name if apt else 'Lotto 235 Garbatella'}!

🔑 *Online Check-in (required by Italian law)*:
{checkin_url}

🚪 *Gate & Door Access* (valid during your stay):
{access_url}

📱 *All-in-one Portal*:
{portal_url}

Available from {res.check_in.strftime('%b %d')} to {res.check_out.strftime('%b %d, %Y')}.

See you soon!"""

    return jsonify({
        'success': True,
        'reservation_id': res.id,
        'guest_name': res.guest_name,
        'checkin_url': checkin_url,
        'access_url': access_url,
        'portal_url': portal_url,
        'templates': {
            'standard': checkin_message,
            'whatsapp': whatsapp_message,
            'airbnb': airbnb_message,
        }
    })
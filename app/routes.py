from flask import (
    Blueprint, Response, render_template, redirect, url_for,
    flash, request, current_app, session, abort, jsonify
)
from app.forms import ReservationForm, LoginForm, ContactForm, ICalFeedForm
from app.models import Reservation, User, Apartment, ICalFeed, Coupon, Testimonial
from app.services.ical_sync import sync_all_feeds
from app import db, mail, csrf
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

        payload = {
            "sender": {"name": "Lotto235 Garbatella", "email": sender_email},
            "to": [{"email": reservation.guest_email}],
            "subject": f"✅ Pagamento Verificato e Confermato — Prenotazione #{reservation.id}",
            "htmlContent": render_template(
                'email_payment_verified.html',
                reservation=reservation
            )
        }
        
        response = requests.post(url, headers=headers, data=json.dumps(payload))
        current_app.logger.info(f"📬 Brevo Verified Payment Email sent. Status: {response.status_code}")
        return response.status_code in [200, 201, 202]
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
                payment_summary=payment_summary
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
    
def calculate_dynamic_total(check_in, check_out, base_rate):
    """
    Loops day-by-day from check_in up to (but excluding) check_out.
    Applies surcharges based on the day of the week or Italian bank holidays.
    """
    # Initialize the Italian holiday registry (targeting IT / Lazio region)
    it_holidays = holidays.Italy(years=[check_in.year, check_out.year])
    
    total_cost = 0.0
    current_date = check_in
    
    while current_date < check_out:
        day_rate = base_rate
        is_premium_day = False
        reason = "Base Rate"
        
        # 1. Check for Italian National Bank Holidays
        if current_date in it_holidays:
            day_rate = base_rate * 1.30  # 30% increase for holidays
            is_premium_day = True
            reason = f"Holiday ({it_holidays.get(current_date)})"
            
        # 2. Check for Summer Season peak (June, July, August)
        elif current_date.month in [6, 7, 8]:
            day_rate = base_rate * 1.15  # 15% seasonal increase
            reason = "Summer Peak Season"
            
        # 3. Check for Weekends (Friday night and Saturday night check-ins)
        # weekday(): 4 is Friday, 5 is Saturday
        if not is_premium_day and current_date.weekday() in [4, 5]:
            day_rate = base_rate * 1.10  # 10% increase for weekends
            reason = "Weekend Pricing"
            
        total_cost += day_rate
        current_date += timedelta(days=1)
        
    return round(total_cost, 2)

# ── Public pages ──────────────────────────────────────────────────────────────
@bp.route('/')
def home():
    apartment = get_apartment()
    testimonials=get_testimonials()
    return render_template('apartment.html', apartment=apartment, testimonials=testimonials)


@bp.route('/faq')
def faq():
    return render_template('faq.html')


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
        base_total = calculate_dynamic_total(check_in, check_out, apartment.price_per_night)
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
    calculated_base = calculate_dynamic_total(check_in, check_out, base_rate)
    
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
            fallback_total = calculate_dynamic_total(check_in_dt, check_out_dt, base_rate)
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
        
    elif method == 'paypal':
        pending = session.get('pending_reservation')
        if not pending:
            flash(_('Session expired. Please try again.'), 'danger')
            return redirect(url_for('routes.reserve'))
            
        apartment = get_apartment()
        check_in_dt = date.fromisoformat(pending['check_in'])
        check_out_dt = date.fromisoformat(pending['check_out'])
        
        # Fallback sicuro sul prezzo dinamico calcolato se total_price manca
        base_rate = apartment.price_per_night if apartment else 0
        fallback_total = calculate_dynamic_total(check_in_dt, check_out_dt, base_rate)
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
            payment_method='paypal',
            cancel_token=secrets.token_urlsafe(32)
        )
        
        db.session.add(new_reservation)
        db.session.commit()
        
        # Send pending payment confirmation email
        send_pending_payment_email(new_reservation)
        
        paypal_username = "il_tuo_username" 
        formatted_price = "%.2f" % total_price
        paypal_url = f"https://paypal.me/{paypal_username}/{formatted_price}EUR"
        
        session['completed_paypal_res_id'] = new_reservation.id
        session['paypal_redirect_url'] = paypal_url
        
        session.pop('pending_reservation', None)
        
        return redirect(url_for('routes.paypal_redirect_page'))    
        
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

@bp.route('/checkout/paypal-redirect')
def paypal_redirect_page():
    reservation_id = session.get('completed_paypal_res_id')
    paypal_url = session.get('paypal_redirect_url')
    
    if not reservation_id or not paypal_url:
        return redirect(url_for('routes.home'))
        
    session.pop('completed_paypal_res_id', None)
    session.pop('paypal_redirect_url', None)
    
    return render_template('paypal_redirect.html', reservation_id=reservation_id, paypal_url=paypal_url)

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
        total_price = calculate_dynamic_total(check_in, check_out, base_rate)

    total_cents = int(total_price * 100)

    if not is_available(check_in, check_out):
        session.pop('pending_reservation', None)
        flash(_('Sorry, those dates were just booked. Please choose again.'), 'danger')
        return redirect(url_for('routes.reserve'))

    try:
        stripe.api_key = current_app.config['STRIPE_SECRET_KEY']
        base_url = current_app.config.get('BASE_URL', request.host_url.rstrip('/'))

        checkout_session = stripe.checkout.Session.create(
            payment_method_types=['card'],
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
        total_price = calculate_dynamic_total(check_in, check_out, apartment.price_per_night) if apartment else 0

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

    refund_failed_warning = False
    if reservation.stripe_payment_intent_id:
        try:
            stripe.api_key = current_app.config['STRIPE_SECRET_KEY']
            stripe.Refund.create(
                payment_intent=reservation.stripe_payment_intent_id,
                reason='requested_by_customer'
            )
            current_app.logger.info(f"Stripe Refund issued for Guest Cancel: Res #{reservation.id}")
            
        except stripe.error.StripeError as e:
            current_app.logger.error(f"Stripe refund transaction failed: {str(e)}")
            refund_failed_warning = True

    reservation.status = "cancelled"
    db.session.commit()

    refund_notice = ""
    if refund_failed_warning:
        refund_notice = "\n\n⚠️ Note: There was a delay processing your automatic refund. Our team has been flagged to verify it manually."

    sender_email = current_app.config.get('MAIL_USERNAME', 'lotto235roma@gmail.com')

    mail.send(Message(
        subject="Your reservation has been cancelled",
        sender=sender_email,
        recipients=[reservation.guest_email],
        body=(
            f"Hello {reservation.guest_name},\n\n"
            f"Your reservation from {reservation.check_in} to {reservation.check_out} "
            f"has been successfully cancelled.{refund_notice}\n\n— My Apartment"
        )
    ))
    
    mail.send(Message(
        subject="Reservation cancelled" + (" [REFUND MANUAL CHECK REQUIRED]" if refund_failed_warning else ""),
        sender=sender_email,
        recipients=[current_app.config['ADMIN_EMAIL']],
        body=(
            f"Reservation cancelled:\n\n"
            f"Guest: {reservation.guest_name}\n"
            f"Dates: {reservation.check_in} → {reservation.check_out}\n"
            f"Stripe Refund Status: {'⚠️ FAILED / MANUAL CHECK REQUIRED' if refund_failed_warning else '✅ Fully Refunded Automatically'}\n"
        )
    ))

    final_ui_message = "Your reservation has been cancelled successfully."
    if not refund_failed_warning and reservation.stripe_payment_intent_id:
        final_ui_message += " A full refund has been issued back to your payment card."
    elif refund_failed_warning:
        final_ui_message += " Your dates are released, but there was an issue processing your automatic refund. We will review it manually."

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


# ── iCal export ───────────────────────────────────────────────────────────────

@bp.route("/ical/apartment.ics")
def export_ical():
    reservations = Reservation.query.filter(
        Reservation.status == "confirmed",
        Reservation.check_out > Reservation.check_in
    ).all()

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//My Apartment//Booking Calendar//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
    ]

    for r in reservations:
        lines.extend([
            "BEGIN:VEVENT",
            f"UID:{r.id}-{r.check_in}@myapartment",
            f"DTSTAMP:{datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')}",
            f"DTSTART;VALUE=DATE:{r.check_in.strftime('%Y%m%d')}",
            f"DTEND;VALUE=DATE:{r.check_out.strftime('%Y%m%d')}",
            "SUMMARY:Reserved",
            "END:VEVENT",
        ])

    lines.append("END:VCALENDAR")

    return Response(
        "\r\n".join(lines),
        mimetype="text/calendar; charset=utf-8",
        headers={
            "Content-Disposition": "inline; filename=apartment.ics",
            "Cache-Control": "no-cache",
        }
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
        apartment = Apartment(price_per_night=120.00)
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
    
    if not check_in_str or not check_out_str:
        return jsonify({"error": "Missing dates"}), 400
        
    try:
        check_in = date.fromisoformat(check_in_str)
        check_out = date.fromisoformat(check_out_str)
        apartment = get_apartment()
        
        base_rate = apartment.price_per_night if apartment else 120.00
        dynamic_total = calculate_dynamic_total(check_in, check_out, base_rate)
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
        refund_failed_warning = False
        if reservation.stripe_payment_intent_id and not reservation.stripe_payment_intent_id.startswith("test_bypass_"):
            try:
                stripe.api_key = current_app.config['STRIPE_SECRET_KEY']
                stripe.Refund.create(
                    payment_intent=reservation.stripe_payment_intent_id,
                    reason='requested_by_customer'
                )
                current_app.logger.info(f"Stripe Refund issued by Admin for Res #{reservation.id}")
            except stripe.error.StripeError as e:
                current_app.logger.error(f"Admin Stripe refund failed: {str(e)}")
                refund_failed_warning = True

        reservation.status = 'cancelled'
        reservation.payment_status = 'refunded' if not refund_failed_warning and reservation.stripe_payment_intent_id else 'cancelled'
        db.session.commit()
        
        if refund_failed_warning:
            flash(f'Reservation #{res_id} cancelled locally, but Stripe refund failed.', 'warning')
        else:
            flash(f'Reservation #{res_id} cancelled and fully refunded successfully.', 'success')
            
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

        if pi_id and not pi_id.startswith("test_bypass_"):
            print(f"💰 STRIPE: Initiating full email-triggered token refund for intent {pi_id}...", flush=True)
            try:
                stripe.api_key = current_app.config['STRIPE_SECRET_KEY']
                stripe.Refund.create(
                    payment_intent=pi_id,
                    reason="requested_by_customer"
                )
                print(f"✅ STRIPE: Token refund complete!", flush=True)
            except stripe.error.StripeError as e:
                print(f"❌ STRIPE REFUND ERROR: {str(e)}", flush=True)
                refund_failed_warning = True

        reservation.status = 'cancelled'
        reservation.payment_status = 'refunded' if not refund_failed_warning and pi_id else 'cancelled'
        db.session.commit()
        
        if refund_failed_warning:
            flash(f"Booking for {reservation.guest_name} cancelled locally, but Stripe refund failed.", "danger")
        else:
            flash(f"Success! Booking for {reservation.guest_name} has been cancelled and fully refunded.", "success")
        
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


# ── Auth ──────────────────────────────────────────────────────────────────────

@bp.route('/login', methods=['GET', 'POST'])
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
    xml_content = """<?xml version="1.0" encoding="UTF-8"?>
    <urlset xmlns="http://www.sitemapindex.org/schemas/sitemap/0.9">
        <url><loc>https://www.lotto235garbatella.it/</loc><changefreq>weekly</changefreq><priority>1.0</priority></url>
        <url><loc>https://www.lotto235garbatella.it/reserve</loc><changefreq>monthly</changefreq><priority>0.8</priority></url>
        <url><loc>https://www.lotto235garbatella.it/contact</loc><changefreq>monthly</changefreq><priority>0.5</priority></url>
    </urlset>"""
    return Response(xml_content, mimetype='text/xml')
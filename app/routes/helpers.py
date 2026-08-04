import json
import secrets
from datetime import date, datetime, timedelta

import holidays
import requests
import stripe
from flask import current_app, render_template, url_for

from app import db
from app.models import Apartment, Reservation, Testimonial

FULL_PAYMENT_DISCOUNT_PCT = 5.0


def get_apartment():
    return Apartment.query.first()


def get_testimonials():
    return (
        Testimonial.query.filter_by(is_published=True)
        .order_by(Testimonial.is_featured.desc(), Testimonial.created_at.desc())
        .limit(6)
        .all()
    )


def is_available(check_in, check_out):
    conflicts = Reservation.query.filter(
        Reservation.status != 'cancelled', Reservation.check_in < check_out, Reservation.check_out > check_in
    ).count()
    return conflicts == 0


def get_payment_summary(reservation):
    tax_note = ''
    if reservation.tourist_tax_amount:
        tax_note = f' (incl. city tax €{reservation.tourist_tax_amount:.2f})'
    if reservation.payment_method == 'stripe':
        paid = reservation.amount_paid or reservation.total_price
        if reservation.payment_status == 'deposit_paid':
            return (
                f'Stripe deposit paid (€{paid:.2f} of €{reservation.total_price:.2f} total, '
                f'balance due €{reservation.total_price - paid:.2f}){tax_note}'
            )
        return f'Paid via Stripe (€{reservation.total_price:.2f}){tax_note}'
    elif reservation.payment_method == 'iban':
        return f'Pending Bank Transfer (Total: €{reservation.total_price:.2f}){tax_note}'
    elif reservation.payment_method == 'cash':
        return f'Cash on Arrival (Total: €{reservation.total_price:.2f}){tax_note}'
    return f'Total: €{reservation.total_price:.2f}{tax_note}'


def calculate_refund_percentage(check_in_date):
    days_until = (check_in_date - date.today()).days
    if days_until > 14:
        return 1.0
    elif days_until >= 7:
        return 0.5
    else:
        return 0.0


def calculate_dynamic_total(check_in, check_out, num_guests=2, base_rate=130.0):
    it_holidays = holidays.Italy(years=[check_in.year, check_out.year])
    total_cost = 0.0
    current_date = check_in
    extra_guests = max(0, num_guests - 2)
    guest_surcharge = extra_guests * 15.0
    nights = (check_out - check_in).days

    while current_date < check_out:
        day_rate = base_rate
        if current_date in it_holidays or current_date.weekday() in [4, 5]:
            day_rate = base_rate * 1.10
        day_rate += guest_surcharge
        total_cost += day_rate
        current_date += timedelta(days=1)

    if nights >= 7:
        total_cost *= 0.90

    return round(total_cost, 2)


def apply_full_payment_discount(total: float) -> float:
    """Apply the full-payment-now discount (3%) to a total."""
    return round(total * (1 - FULL_PAYMENT_DISCOUNT_PCT / 100.0), 2)


def calculate_city_tax(check_in, check_out, num_adults=2, apartment=None) -> float:
    """Rome city tax (€6/night/adult, max 10 taxable nights). Children 3-9 are exempt."""
    from app.services.tourist_tax import get_tax_service

    if apartment:
        rate = get_tax_service(apartment).rate
    else:
        rate = 6.0
    nights = min(max(0, (check_out - check_in).days), 10)
    return round(nights * max(int(num_adults), 1) * rate, 2)


def _send_brevo_email(payload):
    brevo_api_key = current_app.config.get('MAIL_PASSWORD')
    url = 'https://api.brevo.com/v3/smtp/email'
    headers = {'accept': 'application/json', 'content-type': 'application/json', 'api-key': brevo_api_key}
    return requests.post(url, headers=headers, data=json.dumps(payload))


def _send_confirmation_emails(reservation):
    try:
        cancel_url = url_for('routes.cancel_reservation', token=reservation.cancel_token, _external=True)
        apt = get_apartment()
        payment_summary = get_payment_summary(reservation)
        sender_email = 'lotto235roma@gmail.com'

        if not reservation.checkin_token:
            reservation.checkin_token = secrets.token_urlsafe(32)
            db.session.commit()

        guest_payload = {
            'sender': {'name': 'Lotto235 Garbatella', 'email': sender_email},
            'to': [{'email': reservation.guest_email}],
            'subject': f'Booking confirmation — {apt.name if apt else "My Apartment"}',
            'htmlContent': render_template(
                'email_confirmation.html',
                reservation=reservation,
                cancel_url=cancel_url,
                nights=reservation.nights,
                total=reservation.total_price,
                apartment=apt,
                payment_summary=payment_summary,
            ),
        }
        _send_brevo_email(guest_payload)

        admin_recipient = current_app.config.get('ADMIN_EMAIL') or 'lotto235roma@gmail.com'
        admin_cancel_url = url_for('routes.admin_cancel_via_token', token=reservation.cancel_token, _external=True)
        admin_payload = {
            'sender': {'name': 'Booking Engine', 'email': sender_email},
            'to': [{'email': admin_recipient}],
            'subject': f'🔔 New Booking Alert: {reservation.guest_name}',
            'htmlContent': render_template(
                'email_admin_alert.html',
                reservation=reservation,
                payment_summary=payment_summary,
                admin_cancel_url=admin_cancel_url,
            ),
        }
        _send_brevo_email(admin_payload)

    except Exception as exc:
        print('!!! BREVO API FAILURE !!!', flush=True)
        print(f'Error detail: {str(exc)}', flush=True)


def send_payment_verified_email(reservation):
    try:
        sender_email = 'lotto235roma@gmail.com'

        guest_payload = {
            'sender': {'name': 'Lotto235 Garbatella', 'email': sender_email},
            'to': [{'email': reservation.guest_email}],
            'subject': f'✅ Pagamento Verificato e Confermato — Prenotazione #{reservation.id}',
            'htmlContent': render_template('email_payment_verified.html', reservation=reservation),
        }
        r1 = _send_brevo_email(guest_payload)

        admin_recipient = current_app.config.get('ADMIN_EMAIL') or 'lotto235roma@gmail.com'
        admin_payload = {
            'sender': {'name': 'Booking Engine', 'email': sender_email},
            'to': [{'email': admin_recipient}],
            'subject': f'✅ Payment Confirmed: {reservation.guest_name} — Reservation #{reservation.id}',
            'htmlContent': render_template('email_admin_payment_confirmed.html', reservation=reservation),
        }
        r2 = _send_brevo_email(admin_payload)

        return r1.status_code in [200, 201, 202] and r2.status_code in [200, 201, 202]
    except Exception as e:
        current_app.logger.error(f'!!! BREVO API FAILURE FOR RESERVATION #{reservation.id} !!!: {str(e)}')
        return False


def send_pending_payment_email(reservation):
    try:
        sender_email = 'lotto235roma@gmail.com'
        cancel_url = url_for('routes.cancel_reservation', token=reservation.cancel_token, _external=True)
        apt = get_apartment()
        payment_summary = get_payment_summary(reservation)

        if not reservation.checkin_token:
            reservation.checkin_token = secrets.token_urlsafe(32)
            db.session.commit()

        checkin_url = url_for('routes.guest_self_checkin', token=reservation.checkin_token, _external=True)

        guest_payload = {
            'sender': {'name': 'Lotto235 Garbatella', 'email': sender_email},
            'to': [{'email': reservation.guest_email}],
            'subject': f'Booking received — {apt.name if apt else "Lotto 235 Garbatella"}',
            'htmlContent': render_template(
                'email_pending_payment.html',
                reservation=reservation,
                cancel_url=cancel_url,
                checkin_url=checkin_url,
                days_until_checkin=(reservation.check_in - date.today()).days,
                payment_summary=payment_summary,
                apartment=apt,
            ),
        }
        r1 = _send_brevo_email(guest_payload)

        admin_recipient = current_app.config.get('ADMIN_EMAIL') or 'lotto235roma@gmail.com'
        admin_payload = {
            'sender': {'name': 'Booking Engine', 'email': sender_email},
            'to': [{'email': admin_recipient}],
            'subject': f'🆕 New Pending Booking: {reservation.guest_name}',
            'htmlContent': render_template(
                'email_admin_alert.html', reservation=reservation, payment_summary=payment_summary
            ),
        }
        r2 = _send_brevo_email(admin_payload)

        return r1.status_code in [200, 201, 202] and r2.status_code in [200, 201, 202]
    except Exception as e:
        current_app.logger.error(f'!!! BREVO PENDING PAYMENT EMAIL FAILURE FOR #{reservation.id} !!!: {str(e)}')
        return False


def send_cancellation_emails(reservation, refund_failed_warning=False, refund_percentage=1.0, refund_amount=None):
    try:
        sender_email = 'lotto235roma@gmail.com'
        admin_recipient = current_app.config.get('ADMIN_EMAIL') or 'lotto235roma@gmail.com'

        if refund_percentage == 1.0:
            refund_text = '100% (full refund)'
        elif refund_percentage == 0.5:
            refund_text = '50% (partial refund)'
        else:
            refund_text = '0% (no refund per policy)'

        if refund_amount is not None:
            refund_text += f' — €{refund_amount:.2f}'

        refund_note = ''
        if refund_failed_warning:
            refund_note = '\n\n⚠️ Note: There was a delay processing your automatic refund. Our team has been flagged to verify it manually.'

        guest_payload = {
            'sender': {'name': 'Lotto235 Garbatella', 'email': sender_email},
            'to': [{'email': reservation.guest_email}],
            'subject': 'Your reservation has been cancelled — Lotto 235 Garbatella',
            'htmlContent': render_template(
                'email_cancellation.html',
                reservation=reservation,
                refund_note=refund_note,
                refund_failed=refund_failed_warning,
                refund_percentage=refund_percentage,
                refund_amount=refund_amount,
            ),
        }
        r1 = _send_brevo_email(guest_payload)

        refund_status = '⚠️ FAILED / MANUAL CHECK REQUIRED' if refund_failed_warning else f'✅ {refund_text}'
        admin_payload = {
            'sender': {'name': 'Booking Engine', 'email': sender_email},
            'to': [{'email': admin_recipient}],
            'subject': f'Reservation Cancelled: {reservation.guest_name} [REFUND {refund_status}]',
            'htmlContent': render_template(
                'email_admin_cancellation.html',
                reservation=reservation,
                refund_failed=refund_failed_warning,
                refund_status=refund_status,
                refund_percentage=refund_percentage,
                refund_amount=refund_amount,
            ),
        }
        r2 = _send_brevo_email(admin_payload)

        return r1.status_code in [200, 201, 202] and r2.status_code in [200, 201, 202]
    except Exception as e:
        current_app.logger.error(
            f'!!! BREVO CANCELLATION EMAIL FAILURE FOR RESERVATION #{reservation.id} !!!: {str(e)}'
        )
        return False


def create_balance_payment_session(reservation):
    """Create a Stripe checkout Session to collect a reservation's outstanding balance."""
    remaining = round((reservation.total_price - (reservation.amount_paid or 0.0)), 2)
    if remaining <= 0:
        return None

    stripe.api_key = current_app.config.get('STRIPE_SECRET_KEY')
    if not stripe.api_key:
        return None

    try:
        return stripe.checkout.Session.create(
            line_items=[
                {
                    'price_data': {
                        'currency': 'eur',
                        'product_data': {'name': 'Balance Payment — Lotto 235 Garbatella'},
                        'unit_amount': int(remaining * 100),
                    },
                    'quantity': 1,
                }
            ],
            mode='payment',
            success_url=url_for('routes.balance_payment_success', _external=True)
            + '?session_id={CHECKOUT_SESSION_ID}',
            cancel_url=url_for('routes.home', _external=True),
            customer_email=reservation.guest_email,
            metadata={
                'reservation_id': str(reservation.id),
                'type': 'balance_payment',
            },
        )
    except Exception as exc:
        current_app.logger.error('Failed to create balance payment session for reservation #%s: %s', reservation.id, exc)
        return None


def send_balance_invoice_email(reservation, payment_url):
    try:
        apt = get_apartment()
        sender_email = 'lotto235roma@gmail.com'
        remaining = round((reservation.total_price - (reservation.amount_paid or 0.0)), 2)

        guest_payload = {
            'sender': {'name': 'Lotto235 Garbatella', 'email': sender_email},
            'to': [{'email': reservation.guest_email}],
            'subject': f'📄 Balance Payment Due — Booking #{reservation.id}',
            'htmlContent': render_template(
                'email_balance_invoice.html',
                reservation=reservation,
                remaining=remaining,
                payment_url=payment_url,
                apartment=apt,
            ),
        }
        r1 = _send_brevo_email(guest_payload)
        return r1.status_code in [200, 201, 202]
    except Exception as e:
        current_app.logger.error(f'Balance invoice email failure for reservation #{reservation.id}: {str(e)}')
        return False


def send_balance_invoice_reminders():
    """Send balance payment invoices to deposit-paid reservations checking in within 7 days.

    Runs daily; each reservation is invoiced at most once (guarded by balance_invoice_sent_at).
    """
    today = date.today()
    cutoff = today + timedelta(days=7)
    reservations = Reservation.query.filter(
        Reservation.status == 'confirmed',
        Reservation.payment_status == 'deposit_paid',
        Reservation.balance_invoice_sent_at.is_(None),
        Reservation.check_in >= today,
        Reservation.check_in <= cutoff,
    ).all()

    sent = 0
    failed = []
    for res in reservations:
        remaining = round((res.total_price - (res.amount_paid or 0.0)), 2)
        if remaining <= 0:
            continue
        session = create_balance_payment_session(res)
        if not session:
            failed.append(res.id)
            continue
        if send_balance_invoice_email(res, session.url):
            res.balance_invoice_sent_at = datetime.utcnow()
            db.session.commit()
            sent += 1
        else:
            db.session.rollback()
            failed.append(res.id)

    if failed:
        current_app.logger.warning('Balance invoices could not be sent for reservations: %s', failed)
    return {'sent': sent, 'failed': failed}

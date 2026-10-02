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


BREVO_API_URL = 'https://api.brevo.com/v3/smtp/email'
BREVO_TIMEOUT = 10


def _send_brevo_email(payload):
    """Send email via the Brevo HTTPS API (works on Railway; SMTP ports are blocked there).

    Keeps the original ``payload`` dict contract (sender/to/subject/htmlContent,
    optional cc/bcc/replyTo) and returns an object with ``status_code`` so all
    existing callers keep working. Never raises, never hangs (10s timeout).
    """
    if current_app.config.get('MAIL_SUPPRESS_SEND') or current_app.config.get('TESTING'):
        current_app.logger.info('Email suppressed (TESTING): %s', payload.get('subject'))

        class _MockResp:  # mimic success response
            status_code = 201
            text = 'suppressed'

            def json(self):
                return {}

        return _MockResp()
    api_key = current_app.config.get('BREVO_API_KEY')
    recipients = [r['email'] for r in payload.get('to', [])]
    if not api_key:
        current_app.logger.warning('Email NOT sent (no BREVO_API_KEY): %s -> %s', payload.get('subject'), recipients)

        class _NoKeyResp:
            status_code = 500
            text = 'missing BREVO_API_KEY'

            def json(self):
                return {'error': 'missing BREVO_API_KEY'}

        return _NoKeyResp()
    sender = payload.get('sender', {})
    sender_name = sender.get('name') or 'Lotto235 Garbatella'
    sender_email = current_app.config.get('MAIL_DEFAULT_SENDER') or sender.get('email') or 'lotto235roma@gmail.com'
    body = {
        'sender': {'name': sender_name, 'email': sender_email},
        'to': [{'email': r['email']} for r in payload.get('to', [])],
        'subject': payload.get('subject', ''),
        'htmlContent': payload.get('htmlContent') or payload.get('html_content') or '',
    }
    if payload.get('cc'):
        body['cc'] = [{'email': r['email']} for r in payload.get('cc', [])]
    if payload.get('bcc'):
        body['bcc'] = [{'email': r['email']} for r in payload.get('bcc', [])]
    if payload.get('replyTo'):
        body['replyTo'] = {'email': payload['replyTo'].get('email')}
    try:
        resp = requests.post(
            BREVO_API_URL,
            json=body,
            headers={'api-key': api_key, 'Content-Type': 'application/json', 'Accept': 'application/json'},
            timeout=BREVO_TIMEOUT,
        )
        current_app.logger.info('Brevo email %s to %s: %s', resp.status_code, recipients, payload.get('subject'))

        class _Resp:
            status_code = resp.status_code
            text = resp.text[:300]

            def json(self):
                try:
                    return resp.json()
                except Exception:
                    return {}

        return _Resp()
    except Exception as e:
        current_app.logger.error('Brevo send failed to %s: %s', recipients, e)

        class _ErrResp:
            status_code = 500
            text = str(e)

            def json(self):
                return {'error': str(e)}

        return _ErrResp()


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

        # Admin side is Slack-only (Brevo is reserved for guest emails).
        from app.services.slack import notify_new_booking

        notify_new_booking(reservation)

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

        # Admin side is Slack-only (Brevo is reserved for guest emails).
        from app.services.slack import notify_payment_confirmed

        notify_payment_confirmed(reservation)

        return r1.status_code in [200, 201, 202]
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

        # Admin side is Slack-only (Brevo is reserved for guest emails).
        from app.services.slack import notify_new_booking

        notify_new_booking(reservation, pending=True)

        return r1.status_code in [200, 201, 202]
    except Exception as e:
        current_app.logger.error(f'!!! BREVO PENDING PAYMENT EMAIL FAILURE FOR #{reservation.id} !!!: {str(e)}')
        return False


def send_cancellation_emails(reservation, refund_failed_warning=False, refund_percentage=1.0, refund_amount=None):
    try:
        sender_email = 'lotto235roma@gmail.com'

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

        # Admin side is Slack-only (Brevo is reserved for guest emails).
        from app.services.slack import notify_cancellation

        notify_cancellation(reservation, refund_text)

        return r1.status_code in [200, 201, 202]
    except Exception as e:
        current_app.logger.error(
            f'!!! BREVO CANCELLATION EMAIL FAILURE FOR RESERVATION #{reservation.id} !!!: {str(e)}'
        )
        return False


def create_tourist_tax_payment_session(reservation):
    """Create a Stripe checkout Session to collect the city tax (tassa di soggiorno).

    Calculates the tax amount for the reservation and returns the Session, or
    None if the amount is zero / Stripe is not configured.
    """
    tax_amount = round(float(reservation.tourist_tax_amount or 0.0), 2)
    if tax_amount <= 0:
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
                        'product_data': {'name': 'City Tax (Tassa di Soggiorno) — Lotto 235 Garbatella'},
                        'unit_amount': int(tax_amount * 100),
                    },
                    'quantity': 1,
                }
            ],
            mode='payment',
            success_url=url_for('routes.tourist_tax_payment_success', _external=True)
            + '?session_id={CHECKOUT_SESSION_ID}',
            cancel_url=url_for('routes.home', _external=True),
            customer_email=reservation.guest_email,
            metadata={
                'reservation_id': str(reservation.id),
                'type': 'tourist_tax',
            },
        )
    except Exception as exc:
        current_app.logger.error('Failed to create tourist tax session for reservation #%s: %s', reservation.id, exc)
        return None


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
            success_url=url_for('routes.balance_payment_success', _external=True) + '?session_id={CHECKOUT_SESSION_ID}',
            cancel_url=url_for('routes.home', _external=True),
            customer_email=reservation.guest_email,
            metadata={
                'reservation_id': str(reservation.id),
                'type': 'balance_payment',
            },
        )
    except Exception as exc:
        current_app.logger.error(
            'Failed to create balance payment session for reservation #%s: %s', reservation.id, exc
        )
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

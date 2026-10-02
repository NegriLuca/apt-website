"""Email service for guest communications including check-in links.

Uses the Brevo transactional-email HTTPS API (``BREVO_API_KEY`` env var).
Railway blocks outbound SMTP ports, so Gmail SMTP cannot work in
production — plain HTTPS to api.brevo.com always does.

NOTE: the sender address (``MAIL_DEFAULT_SENDER``, default
lotto235roma@gmail.com) must be registered as a validated sender in the
Brevo dashboard (Senders → Add), otherwise Brevo rejects the request.

All functions return True/False and never raise.
"""

from flask import current_app, render_template

BREVO_API_URL = 'https://api.brevo.com/v3/smtp/email'
BREVO_TIMEOUT = 10


def send_email(
    recipients: list[str],
    subject: str,
    html: str,
    sender_name: str = 'Lotto235 Garbatella',
    reply_to: str | None = None,
) -> bool:
    """Send one transactional email via Brevo. Never raises."""
    if current_app.config.get('MAIL_SUPPRESS_SEND') or current_app.config.get('TESTING'):
        current_app.logger.info('Email suppressed (TESTING): %s -> %s', subject, recipients)
        return True
    api_key = current_app.config.get('BREVO_API_KEY')
    if not api_key:
        current_app.logger.warning('Email NOT sent (no BREVO_API_KEY): %s -> %s', subject, recipients)
        return False
    sender_email = (
        current_app.config.get('MAIL_DEFAULT_SENDER')
        or current_app.config.get('ADMIN_EMAIL')
        or 'lotto235roma@gmail.com'
    )
    payload: dict = {
        'sender': {'name': sender_name, 'email': sender_email},
        'to': [{'email': r} for r in recipients],
        'subject': subject,
        'htmlContent': html,
    }
    if reply_to:
        payload['replyTo'] = {'email': reply_to}
    try:
        import requests

        resp = requests.post(
            BREVO_API_URL,
            json=payload,
            headers={'api-key': api_key, 'Content-Type': 'application/json', 'Accept': 'application/json'},
            timeout=BREVO_TIMEOUT,
        )
        if resp.status_code in (200, 201, 202):
            current_app.logger.info('📬 Brevo email sent to %s: %s', recipients, subject)
            return True
        current_app.logger.error(
            '!!! BREVO EMAIL FAILURE %s -> %s: %s %s', subject, recipients, resp.status_code, resp.text[:300]
        )
        return False
    except Exception as e:
        current_app.logger.error('!!! BREVO EMAIL FAILURE %s -> %s: %s', subject, recipients, e)
        return False


def send_checkin_email(reservation, checkin_url):
    """Send check-in link to guest via email"""
    try:
        if current_app.config.get('MAIL_SUPPRESS_SEND') or current_app.config.get('TESTING'):
            current_app.logger.info('Email suppressed (TESTING): check-in for #%s', reservation.id)
            return True
        from app.models import Apartment

        apt = Apartment.query.first()
        subject = f'🔑 Completa il tuo Check-in Online — {apt.name if apt else "Lotto 235 Garbatella"}'
        html_content = render_template(
            'email_guest_checkin.html', reservation=reservation, checkin_url=checkin_url, apartment=apt
        )
        return send_email([reservation.guest_email], subject, html_content)
    except Exception as e:
        current_app.logger.error(f'!!! CHECK-IN EMAIL FAILURE FOR RESERVATION #{reservation.id} !!!: {str(e)}')
        return False


def send_access_email(reservation, access_url):
    """Send gate/door access link to guest via email"""
    try:
        if current_app.config.get('MAIL_SUPPRESS_SEND') or current_app.config.get('TESTING'):
            current_app.logger.info('Email suppressed (TESTING): access for #%s', reservation.id)
            return True
        from app.models import Apartment

        apt = Apartment.query.first()
        subject = f'🔑 Il tuo Accesso Gate & Porta — {apt.name if apt else "Lotto 235 Garbatella"}'
        html_content = render_template(
            'email_guest_access.html', reservation=reservation, access_url=access_url, apartment=apt
        )
        return send_email([reservation.guest_email], subject, html_content)
    except Exception as e:
        current_app.logger.error(f'!!! ACCESS EMAIL FAILURE FOR RESERVATION #{reservation.id} !!!: {str(e)}')
        return False


def send_admin_checkin_notification(reservation):
    """Notify admin when guest completes check-in — Slack-only (Brevo is guests-only)."""
    try:
        from app.services.slack import notify_checkin_completed

        return notify_checkin_completed(reservation)
    except Exception as e:
        current_app.logger.error(f'!!! ADMIN CHECK-IN NOTIFICATION FAILURE !!!: {str(e)}')
        return False

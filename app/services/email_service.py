"""
Email service for guest communications including check-in links.
Uses Gmail SMTP via Flask-Mail (MAIL_* env vars, see config.py / .env.example).
"""

from flask import current_app, render_template

from app import mail, db
from flask_mail import Message


def _smtp_send(subject: str, recipients: list[str], html: str, sender_name: str = 'Lotto235 Garbatella', reply_to: str | None = None) -> bool:
    if current_app.config.get('MAIL_SUPPRESS_SEND') or current_app.config.get('TESTING'):
        current_app.logger.info('Email suppressed (TESTING): %s -> %s', subject, recipients)
        return True
    sender_email = current_app.config.get('MAIL_DEFAULT_SENDER') or current_app.config.get('MAIL_USERNAME') or 'lotto235roma@gmail.com'
    try:
        msg = Message(subject=subject, recipients=recipients, html=html, sender=(sender_name, sender_email), reply_to=reply_to)
        mail.send(msg)
        current_app.logger.info('📬 SMTP email sent to %s: %s', recipients, subject)
        return True
    except Exception as e:
        current_app.logger.error('!!! SMTP EMAIL FAILURE %s -> %s: %s', subject, recipients, e)
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
        html_content = render_template('email_guest_checkin.html', reservation=reservation, checkin_url=checkin_url, apartment=apt)
        return _smtp_send(subject, [reservation.guest_email], html_content, sender_name='Lotto235 Garbatella')
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
        html_content = render_template('email_guest_access.html', reservation=reservation, access_url=access_url, apartment=apt)
        return _smtp_send(subject, [reservation.guest_email], html_content, sender_name='Lotto235 Garbatella')
    except Exception as e:
        current_app.logger.error(f'!!! ACCESS EMAIL FAILURE FOR RESERVATION #{reservation.id} !!!: {str(e)}')
        return False


def send_admin_checkin_notification(reservation):
    """Notify admin when guest completes check-in"""
    try:
        if current_app.config.get('MAIL_SUPPRESS_SEND') or current_app.config.get('TESTING'):
            current_app.logger.info('Email suppressed (TESTING): admin check-in for #%s', reservation.id)
            return True
        admin_recipient = current_app.config.get('ADMIN_EMAIL') or 'lotto235roma@gmail.com'
        subject = f'✅ Guest Check-in Completed: {reservation.guest_name} — Reservation #{reservation.id}'
        html = render_template('email_admin_checkin_completed.html', reservation=reservation)
        return _smtp_send(subject, [admin_recipient], html, sender_name='Lotto235 Booking Engine')
    except Exception as e:
        current_app.logger.error(f'!!! ADMIN CHECK-IN NOTIFICATION FAILURE !!!: {str(e)}')
        return False

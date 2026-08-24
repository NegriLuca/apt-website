"""
Email service for guest communications including check-in links.
"""

import json

import requests
from flask import current_app, render_template

from app import db


def send_checkin_email(reservation, checkin_url):
    """Send check-in link to guest via email"""
    try:
        if current_app.config.get('MAIL_SUPPRESS_SEND') or current_app.config.get('TESTING'):
            current_app.logger.info('Email suppressed (TESTING): check-in for #%s', reservation.id)
            return True
        brevo_api_key = current_app.config.get('MAIL_PASSWORD')
        sender_email = 'lotto235roma@gmail.com'
        apt = db.session.query(current_app.models.Apartment).first() if hasattr(current_app, 'models') else None

        # Get apartment name
        from app.models import Apartment

        apt = Apartment.query.first()

        url = 'https://api.brevo.com/v3/smtp/email'
        headers = {'accept': 'application/json', 'content-type': 'application/json', 'api-key': brevo_api_key}

        subject = f'🔑 Completa il tuo Check-in Online — {apt.name if apt else "Lotto 235 Garbatella"}'

        html_content = render_template(
            'email_guest_checkin.html', reservation=reservation, checkin_url=checkin_url, apartment=apt
        )

        payload = {
            'sender': {'name': 'Lotto235 Garbatella', 'email': sender_email},
            'to': [{'email': reservation.guest_email}],
            'subject': subject,
            'htmlContent': html_content,
        }

        response = requests.post(url, headers=headers, data=json.dumps(payload))
        current_app.logger.info(f'📬 Check-in email sent to {reservation.guest_email}. Status: {response.status_code}')
        return response.status_code in [200, 201, 202]

    except Exception as e:
        current_app.logger.error(f'!!! CHECK-IN EMAIL FAILURE FOR RESERVATION #{reservation.id} !!!: {str(e)}')
        return False


def send_access_email(reservation, access_url):
    """Send gate/door access link to guest via email"""
    try:
        if current_app.config.get('MAIL_SUPPRESS_SEND') or current_app.config.get('TESTING'):
            current_app.logger.info('Email suppressed (TESTING): access for #%s', reservation.id)
            return True
        brevo_api_key = current_app.config.get('MAIL_PASSWORD')
        sender_email = 'lotto235roma@gmail.com'

        from app.models import Apartment

        apt = Apartment.query.first()

        url = 'https://api.brevo.com/v3/smtp/email'
        headers = {'accept': 'application/json', 'content-type': 'application/json', 'api-key': brevo_api_key}

        subject = f'🔑 Il tuo Accesso Gate & Porta — {apt.name if apt else "Lotto 235 Garbatella"}'

        html_content = render_template(
            'email_guest_access.html', reservation=reservation, access_url=access_url, apartment=apt
        )

        payload = {
            'sender': {'name': 'Lotto235 Garbatella', 'email': sender_email},
            'to': [{'email': reservation.guest_email}],
            'subject': subject,
            'htmlContent': html_content,
        }

        response = requests.post(url, headers=headers, data=json.dumps(payload))
        current_app.logger.info(f'📬 Access email sent to {reservation.guest_email}. Status: {response.status_code}')
        return response.status_code in [200, 201, 202]

    except Exception as e:
        current_app.logger.error(f'!!! ACCESS EMAIL FAILURE FOR RESERVATION #{reservation.id} !!!: {str(e)}')
        return False


def send_admin_checkin_notification(reservation):
    """Notify admin when guest completes check-in"""
    try:
        if current_app.config.get('MAIL_SUPPRESS_SEND') or current_app.config.get('TESTING'):
            current_app.logger.info('Email suppressed (TESTING): admin check-in for #%s', reservation.id)
            return True
        brevo_api_key = current_app.config.get('MAIL_PASSWORD')
        sender_email = 'lotto235roma@gmail.com'
        admin_recipient = current_app.config.get('ADMIN_EMAIL') or 'lotto235roma@gmail.com'

        url = 'https://api.brevo.com/v3/smtp/email'
        headers = {'accept': 'application/json', 'content-type': 'application/json', 'api-key': brevo_api_key}

        payload = {
            'sender': {'name': 'Lotto235 Booking Engine', 'email': sender_email},
            'to': [{'email': admin_recipient}],
            'subject': f'✅ Guest Check-in Completed: {reservation.guest_name} — Reservation #{reservation.id}',
            'htmlContent': render_template('email_admin_checkin_completed.html', reservation=reservation),
        }

        response = requests.post(url, headers=headers, data=json.dumps(payload))
        return response.status_code in [200, 201, 202]

    except Exception as e:
        current_app.logger.error(f'!!! ADMIN CHECK-IN NOTIFICATION FAILURE !!!: {str(e)}')
        return False

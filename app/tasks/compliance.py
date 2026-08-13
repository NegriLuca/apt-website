"""
Celery tasks for Italian compliance automation.
Run these via Celery Beat schedule.
"""

import logging
from datetime import date, datetime, timedelta

from flask import current_app

from app import db
from app.models import Reservation
from app.services.questura import get_questura_service
from app.services.tourist_tax import get_tax_service

logger = logging.getLogger(__name__)

# Try to import Celery, make it optional
try:
    from celery import shared_task

    CELERY_AVAILABLE = True
except ImportError:
    CELERY_AVAILABLE = False

    def shared_task(*args, **kwargs):
        """Decorator that does nothing when Celery not available"""

        def wrapper(f):
            return f

        return wrapper


@shared_task(bind=True, max_retries=3, default_retry_delay=3600)
def submit_questura_daily(self):
    """
    Daily task: Submit today's check-ins to Questura.
    Runs at 08:00 via APScheduler (or Celery Beat if available).
    Only submits reservations where guest data is complete.
    """
    try:
        today = date.today()
        logger.info('Starting daily Questura submission for %s', today)

        # Find confirmed reservations checking in today with complete guest data
        reservations = Reservation.query.filter(
            Reservation.status == 'confirmed',
            Reservation.check_in == today,
            Reservation.questura_status.in_([None, 'pending', 'rejected']),
        ).all()

        if not reservations:
            logger.info('No reservations checking in today')
            return {'success': True, 'message': 'No check-ins today', 'count': 0}

        # Filter to only those with complete guest data
        ready = [r for r in reservations if r.questura_ready()]
        not_ready = [r for r in reservations if not r.questura_ready()]

        results = {'submitted': 0, 'failed': 0, 'not_ready': len(not_ready), 'errors': []}

        if not_ready:
            logger.warning('%d reservations missing guest data for Questura', len(not_ready))
            for r in not_ready:
                logger.warning('  Reservation #%s: missing guest identification data', r.id)

        if not ready:
            return {
                'success': True,
                'message': f'No reservations ready (out of {len(reservations)} with complete guest data)',
                **results,
            }

        service = get_questura_service()
        if not service.is_configured():
            return {'success': False, 'error': 'Questura service not configured'}

        # Submit each reservation (service.submit_reservation builds the guest
        # list including companions and updates the reservation status itself)
        for res in ready:
            result = service.submit_reservation(res)

            if result.get('success'):
                results['submitted'] += 1
            else:
                results['failed'] += 1
                results['errors'].append(f'Reservation #{res.id}: {result.get("error", "Unknown error")}')

        logger.info('Daily Questura submission complete: %s', results)
        return results

    except Exception as e:
        logger.exception('Daily Questura submission failed')
        if self is not None and hasattr(self, 'retry'):
            self.retry(exc=e)
        return {'success': False, 'error': str(e)}


@shared_task(bind=True, max_retries=3, default_retry_delay=1800)
def retry_failed_questura(self, reservation_ids: list[int] = None):
    """Retry failed Questura submissions"""
    try:
        query = Reservation.query.filter(Reservation.questura_status == 'rejected')
        if reservation_ids:
            query = query.filter(Reservation.id.in_(reservation_ids))

        reservations = query.all()
        if not reservations:
            return {'success': True, 'message': 'No failed submissions to retry'}

        service = get_questura_service()
        results = {'retried': 0, 'succeeded': 0, 'failed': 0}

        for res in reservations:
            if not res.questura_ready():
                logger.warning('Reservation #%s still missing guest data, skipping', res.id)
                continue

            result = service.submit_reservation(res)

            results['retried'] += 1
            if result.get('success'):
                results['succeeded'] += 1
            else:
                results['failed'] += 1

        logger.info('Questura retry complete: %s', results)
        return results

    except Exception as e:
        logger.exception('Questura retry failed')
        if self is not None and hasattr(self, 'retry'):
            self.retry(exc=e)
        return {'success': False, 'error': str(e)}


@shared_task(bind=True)
def generate_monthly_tourist_tax_report(self):
    """
    Monthly task: Generate tourist tax report for previous month.
    Runs on 1st of each month at 02:00 via Celery Beat.
    """
    try:
        today = date.today()
        year = today.year if today.month > 1 else today.year - 1
        month = today.month - 1 if today.month > 1 else 12

        logger.info(f'Generating tourist tax report for {month:02d}/{year}')

        apt = current_app.config.get('APARTMENT')  # Would be set in app context
        if not apt:
            from app.models import Apartment

            apt = Apartment.query.first()

        service = get_tax_service(apt)
        report = service.generate_detailed_report(year, month)

        # Save CSV to storage or send via email
        csv_data = report['csv_data']

        # Option 1: Save to file (if using local storage)
        # Option 2: Upload to S3/GCS
        # Option 3: Email to admin

        logger.info(
            f'Tourist tax report generated: {report["total_reservations"]} reservations, €{report["total_tax"]:.2f}'
        )

        return {
            'success': True,
            'period': f'{month:02d}/{year}',
            'reservations': report['total_reservations'],
            'total_tax': report['total_tax'],
            'csv_size': len(csv_data),
        }

    except Exception as e:
        logger.exception('Monthly tourist tax report failed')
        return {'success': False, 'error': str(e)}


@shared_task(bind=True)
def send_guest_checkin_reminder(self):
    """
    Daily task: Send reminder to collect guest data for upcoming check-ins.
    Runs at 09:00 via Celery Beat.
    """
    try:
        # Find reservations checking in next 2 days that lack guest data
        target_date = date.today() + timedelta(days=2)
        reservations = Reservation.query.filter(
            Reservation.status == 'confirmed',
            Reservation.check_in == target_date,
            Reservation.questura_status.in_([None, 'pending']),
        ).all()

        not_ready = [r for r in reservations if not r.questura_ready()]

        if not_ready:
            # Send email to admin
            guest_list = '\n'.join(
                [f'  #{r.id}: {r.guest_name} - {r.check_in} (missing: {_missing_fields(r)})' for r in not_ready]
            )

            logger.warning(f'Guest data incomplete for {len(not_ready)} upcoming check-ins:\n{guest_list}')

            # Would send email here using existing email service
            # send_admin_alert(
            #     subject=f"⚠️ Guest data needed for {len(not_ready)} upcoming check-ins",
            #     body=f"The following reservations check in on {target_date} but lack Questura data:\n{guest_list}"
            # )

        return {'success': True, 'total_checkins': len(reservations), 'incomplete': len(not_ready)}

    except Exception as e:
        logger.exception('Guest check-in reminder failed')
        return {'success': False, 'error': str(e)}


def _missing_fields(reservation: Reservation) -> list[str]:
    """Return list of missing guest fields for Questura"""
    missing = []
    if not reservation.guest_surname:
        missing.append('surname')
    if not reservation.guest_first_name:
        missing.append('first_name')
    if not reservation.guest_birth_date:
        missing.append('birth_date')
    if not reservation.guest_birth_place:
        missing.append('birth_place')
    if not reservation.guest_nationality:
        missing.append('nationality')
    if not reservation.guest_document_type:
        missing.append('document_type')
    if not reservation.guest_document_number:
        missing.append('document_number')
    if not reservation.guest_document_expiry:
        missing.append('document_expiry')
    if not reservation.guest_document_country:
        missing.append('document_country')
    if not reservation.guest_gender:
        missing.append('gender')
    return missing


@shared_task(bind=True)
def sync_ross1000_property(self):
    """
    Sync property data to Ross1000 (Regione Lazio).
    This is typically manual/annual - just generates the data package.
    """
    from app.models import Apartment

    apt = Apartment.query.first()
    if not apt:
        return {'success': False, 'error': 'No apartment configured'}

    data = {
        'denominazione': apt.name,
        'tipologia': 'CAV',  # Case per Vacanze
        'indirizzo': 'Lotto 235, Via...',  # Would need address fields
        'comune': 'Roma',
        'provincia': 'RM',
        'cap': '001XX',
        'cin': apt.cin_code,
        'cir': apt.cir_code,
        'capacita_ricettiva': apt.max_guests,
        'numero_camere': 1,  # Would need field
        'numero_posti_letto': apt.max_guests,
        'periodo_apertura': 'Annuale',
        'titolare': 'Negri Luca',
        'email': 'lotto235roma@gmail.com',
        'telefono': '+39...',
    }

    logger.info(f'Ross1000 data package ready for {apt.name}: {data}')
    return {'success': True, 'data': data, 'message': 'Manual submission to Ross1000 portal required'}


@shared_task(bind=True, max_retries=3, default_retry_delay=3600)
def submit_ross1000_daily(self):
    """
    Daily task: Submit today's check-ins to ROSS1000 (Regione Lazio).
    """
    try:
        today = date.today()
        logger.info(f'Starting daily ROSS1000 submission for {today}')

        from app.services.ross1000 import get_ross1000_service

        service = get_ross1000_service()
        if not service.is_configured():
            return {'success': False, 'error': 'ROSS1000 service not configured'}

        reservations = Reservation.query.filter(
            Reservation.status == 'confirmed',
            Reservation.check_in == today,
            Reservation.ross1000_status.in_([None, 'pending', 'rejected']),
        ).all()

        if not reservations:
            logger.info('No reservations checking in today for ROSS1000')
            return {'success': True, 'message': 'No check-ins today', 'count': 0}

        results = {'submitted': 0, 'failed': 0, 'errors': []}

        for res in reservations:
            result = service.submit_reservation(res)

            if result.get('success'):
                results['submitted'] += 1
                res.ross1000_status = 'accepted'
                res.ross1000_submitted_at = datetime.utcnow()
            else:
                results['failed'] += 1
                results['errors'].append(f'Reservation #{res.id}: {result.get("error", "Unknown error")}')
                res.ross1000_status = 'rejected'
                res.ross1000_error = result.get('error', 'Unknown error')

            db.session.add(res)

        db.session.commit()
        logger.info(f'Daily ROSS1000 submission complete: {results}')
        return results

    except Exception as e:
        logger.exception('Daily ROSS1000 submission failed')
        self.retry(exc=e)


def run_daily_ross1000():
    """Run daily ROSS1000 submission synchronously (for testing/cron)"""
    return submit_ross1000_daily()


# Celery Beat Schedule Configuration
CELERY_BEAT_SCHEDULE = {
    'questura-daily-submission': {
        'task': 'app.tasks.compliance.submit_questura_daily',
        'schedule': 86400.0,  # Daily at 08:00 (configure in celery config)
        'options': {'queue': 'compliance'},
    },
    'questura-retry-failed': {
        'task': 'app.tasks.compliance.retry_failed_questura',
        'schedule': 21600.0,  # Every 6 hours
        'options': {'queue': 'compliance'},
    },
    'tourist-tax-monthly-report': {
        'task': 'app.tasks.compliance.generate_monthly_tourist_tax_report',
        'schedule': 'cron: 0 2 1 * *',  # 1st of month at 02:00
        'options': {'queue': 'compliance'},
    },
    'guest-checkin-reminder': {
        'task': 'app.tasks.compliance.send_guest_checkin_reminder',
        'schedule': 86400.0,  # Daily at 09:00
        'options': {'queue': 'compliance'},
    },
}


# Fallback functions for running without Celery
def _run_sync(task, *args):
    """Run a shared_task synchronously, whether Celery is installed or not."""
    if hasattr(task, 'run'):  # Celery Task object
        return task.run(*args)
    return task(None, *args)


def run_daily_questura():
    """Run daily Questura submission synchronously (for testing/cron)"""
    return _run_sync(submit_questura_daily)


def run_monthly_tax_report():
    """Run monthly tax report synchronously"""
    return generate_monthly_tourist_tax_report()


def run_guest_reminder():
    """Run guest check-in reminder synchronously"""
    return send_guest_checkin_reminder()


def run_questura_retry(reservation_ids: list[int] = None):
    """Retry failed Questura submissions synchronously"""
    return _run_sync(retry_failed_questura, reservation_ids)

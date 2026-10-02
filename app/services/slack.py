"""Slack admin alerts via Incoming Webhook — Lotto 235.

Railway blocks outbound SMTP, so Gmail notifications never arrive. Slack
webhooks are plain HTTPS POSTs and always work from Railway.

Setup (2 min): Slack workspace → Apps → *Incoming Webhooks* → add to a
channel (e.g. #lotto235) → copy the URL → set ``SLACK_WEBHOOK_URL`` in
Railway Variables.

All functions are fire-and-forget: they never raise, return ``True`` on
delivery and ``False`` otherwise (missing URL, test mode, network error).
"""

from __future__ import annotations

from flask import current_app, url_for

SLACK_TIMEOUT = 10


def _webhook_url() -> str | None:
    return current_app.config.get('SLACK_WEBHOOK_URL') or None


def notify(text: str) -> bool:
    """Post a plain-text (mrkdwn) message to the admin Slack channel."""
    url = _webhook_url()
    if not url:
        return False
    if current_app.config.get('SLACK_SUPPRESS_SEND') or current_app.config.get('TESTING'):
        current_app.logger.info('Slack suppressed (TESTING): %s', text[:120])
        return True
    try:
        import requests

        resp = requests.post(url, json={'text': text}, timeout=SLACK_TIMEOUT)
        if resp.status_code == 200:
            return True
        current_app.logger.warning('Slack webhook returned %s: %s', resp.status_code, resp.text[:200])
        return False
    except Exception as e:
        current_app.logger.warning('Slack notify failed (non-blocking): %s', e)
        return False


def _admin_link(reservation_id: int) -> str:
    """Absolute admin URL for a reservation; falls back to BASE_URL/admin."""
    base = (current_app.config.get('BASE_URL') or '').rstrip('/')
    try:
        path = url_for('routes.admin_guest_message', reservation_id=reservation_id)
    except Exception:
        return f'{base}/admin' if base else ''
    if path.startswith('http'):
        return path
    return f'{base}{path}' if base else path


def _res_line(reservation) -> str:
    total = reservation.total_price or 0.0
    return (
        f'#{reservation.id} {reservation.guest_name} '
        f'{reservation.check_in.strftime("%d/%m")} → {reservation.check_out.strftime("%d/%m")} '
        f'({reservation.nights}n, {reservation.num_guests} guests) €{total:.2f} [{reservation.source}]'
    )


def notify_new_booking(reservation, pending: bool = False) -> bool:
    kind = 'wire-transfer (pending payment)' if pending else reservation.payment_method or 'booking'
    return notify(f'🔔 *New booking* ({kind})\n{_res_line(reservation)}\n<{_admin_link(reservation.id)}|Open in admin>')


def notify_cancellation(reservation, refund_text: str = '') -> bool:
    extra = f' — refund {refund_text}' if refund_text else ''
    return notify(f'❌ *Cancelled*{extra}\n{_res_line(reservation)}\n<{_admin_link(reservation.id)}|Open in admin>')


def notify_payment_confirmed(reservation) -> bool:
    return notify(f'✅ *Payment confirmed*\n{_res_line(reservation)}\n<{_admin_link(reservation.id)}|Open in admin>')


def notify_checkin_completed(reservation) -> bool:
    return notify(
        f'🔑 *Guest check-in completed*\n{_res_line(reservation)}\n<{_admin_link(reservation.id)}|Open in admin>'
    )


def notify_contact(name: str, email: str, message_text: str) -> bool:
    snippet = (message_text or '')[:300]
    return notify(f'📬 *Contact form* — {name} ({email})\n{snippet}')

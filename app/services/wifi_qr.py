"""Guest Wi-Fi QR code generation.

Encodes the apartment's guest network in the standard ``WIFI:`` QR format
(constraints: https://stackoverflow.com/questions/15809395) so that phones
scan-and-connect without typing the SSID or password.
"""

from __future__ import annotations

import io

from flask import current_app

from app.models import Apartment


def _qr(payload: str):
    """Build a segno QRCode with a decent error-correction level."""
    import segno

    return segno.make(payload, error='m')


def wifi_qr_data_uri(apartment: Apartment | None) -> str | None:
    """Return a base64 PNG data URI of the Wi-Fi QR, or None if unconfigured."""
    if not apartment or not apartment.wifi_configured:
        return None
    payload = apartment.wifi_payload()
    if not payload:
        return None

    import base64

    buf = io.BytesIO()
    _qr(payload).save(buf, kind='png', scale=10, border=2)
    encoded = base64.b64encode(buf.getvalue()).decode('ascii')
    return f'data:image/png;base64,{encoded}'


def wifi_qr_bytes(apartment: Apartment | None, scale: int = 10) -> bytes | None:
    """Return raw PNG bytes of the Wi-Fi QR, or None if unconfigured."""
    if not apartment or not apartment.wifi_configured:
        return None
    payload = apartment.wifi_payload()
    if not payload:
        return None

    buf = io.BytesIO()
    _qr(payload).save(buf, kind='png', scale=scale, border=2)
    return buf.getvalue()


def qr_from_payload(payload: str, scale: int = 10) -> bytes:
    """Render raw PNG bytes for an arbitrary payload (e.g. a guest page URL)."""
    buf = io.BytesIO()
    _qr(payload).save(buf, kind='png', scale=scale, border=2)
    return buf.getvalue()


def sync_wifi_from_env() -> None:
    """Overlay Wi-Fi settings from env vars onto the apartment on every startup.

    Follows the same pattern as the Nuki env sync in ``run.py``: env values act
    as the source of truth when present, otherwise the apartment keeps whatever
    was configured in the admin panel.
    """
    import os

    from app import db

    ssid = os.environ.get('WIFI_SSID', '').strip()
    password = os.environ.get('WIFI_PASSWORD', '').strip()
    band = os.environ.get('WIFI_BAND', '').strip()
    security = os.environ.get('WIFI_SECURITY', '').strip()
    hidden_raw = os.environ.get('WIFI_HIDDEN', '').strip()

    if not (ssid or password or band or security or hidden_raw):
        return False

    apartment = Apartment.query.first()
    if not apartment:
        return False

    changed = False
    if ssid:
        apartment.wifi_ssid = ssid
        changed = True
    if password:
        apartment.wifi_password = password
        changed = True
    if band:
        apartment.wifi_band = band
        changed = True
    if security:
        apartment.wifi_security = security
        changed = True
    if hidden_raw:
        apartment.wifi_hidden = hidden_raw.lower() in ('1', 'true', 'yes', 'on')
        changed = True

    if changed:
        db.session.commit()
        current_app.logger.info('Wi-Fi config synced from env vars (ssid=%s)', apartment.wifi_ssid)
    return changed

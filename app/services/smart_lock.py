"""
Smart Lock Service for Nuki Smart Lock Ultra and Shelly Mini 1 Gen4 integration.
Handles guest access control for apartment door and building gate.
"""

import os

import requests
from flask import current_app


class SmartLockError(Exception):
    """Custom exception for smart lock errors"""

    pass


class ShellyService:
    """Service to control Shelly Mini 1 Gen4 (Gate relay)"""

    def __init__(self, apartment):
        self.apartment = apartment
        self.enabled = apartment.shelly_enabled
        self.host = apartment.shelly_host
        self.auth_key = apartment.shelly_auth_key
        self.channel = apartment.shelly_relay_channel or 0

        # Cloud Control API config (used when SHELLY_CLOUD_SERVER + SHELLY_CLOUD_KEY are set).
        # Controlled by device ID, so it works from a cloud-hosted app and survives IP changes.
        self.cloud_server = (os.environ.get('SHELLY_CLOUD_SERVER') or '').strip()
        self.cloud_key = (os.environ.get('SHELLY_CLOUD_KEY') or self.auth_key or '').strip()
        self.cloud_device_id = os.environ.get('SHELLY_DEVICE_ID') or self.host or ''

    @property
    def in_cloud_mode(self):
        return bool(self.cloud_server and self.cloud_key and self.cloud_device_id)

    def _cloud_url(self, endpoint):
        base = self.cloud_server
        if not base.startswith(('http://', 'https://')):
            base = f'https://{base}'
        return f'{base}{endpoint}'

    def _cloud_get_status(self):
        """Get device status through the Shelly Cloud Control API (v2)."""
        resp = requests.post(
            self._cloud_url('/v2/devices/api/get'),
            params={'auth_key': self.cloud_key},
            json={'ids': [self.cloud_device_id], 'select': ['status']},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        return data[0] if isinstance(data, list) and data else data

    def _cloud_relay_control(self, on):
        """Control a relay channel through the Shelly Cloud Control API (v2)."""
        payload = {
            'id': self.cloud_device_id,
            'channel': self.channel,
            'on': on,
        }
        resp = requests.post(
            self._cloud_url('/v2/devices/api/set/switch'),
            params={'auth_key': self.cloud_key},
            json=payload,
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json() if resp.content else {}

    def _get_base_url(self):
        """Get base URL for Shelly API"""
        if not self.host:
            raise SmartLockError('Shelly host not configured')
        if not self.host.startswith(('http://', 'https://')):
            return f'http://{self.host}'
        return self.host

    def _get_headers(self):
        """Get headers for Shelly API (Gen 4 uses RPC)"""
        headers = {'Content-Type': 'application/json'}
        if self.auth_key:
            headers['Authorization'] = f'Bearer {self.auth_key}'
        return headers

    def is_configured(self):
        """Check if Shelly is properly configured"""
        return self.enabled and (self.in_cloud_mode or bool(self.host))

    def get_status(self):
        """Get Shelly device status"""
        if not self.is_configured():
            raise SmartLockError('Shelly not configured')

        if self.in_cloud_mode:
            try:
                return self._cloud_get_status()
            except requests.RequestException as e:
                raise SmartLockError(f'Failed to get Shelly status: {e}')

        url = f'{self._get_base_url()}/rpc/Shelly.GetStatus'
        try:
            resp = requests.post(url, headers=self._get_headers(), json={}, timeout=5)
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as e:
            raise SmartLockError(f'Failed to get Shelly status: {e}')

    def pulse_relay(self):
        """Open the gate. Relay auto-off is handled by the device's own config."""
        if not self.is_configured():
            raise SmartLockError('Shelly not configured')

        if self.in_cloud_mode:
            # Device is configured with auto_off on the relay, so a plain "on"
            # is enough — the Shelly closes the gate itself after auto_off_delay.
            try:
                result = self._cloud_relay_control(on=True)
                current_app.logger.info(f'Shelly gate pulse triggered via cloud: {result}')
                return True, 'Gate opened successfully'
            except requests.RequestException as e:
                current_app.logger.error(f'Shelly cloud pulse failed: {e}')
                raise SmartLockError(f'Failed to pulse gate: {e}')

        url = f'{self._get_base_url()}/rpc/Switch.Set'
        payload = {'id': self.channel, 'on': True}

        try:
            resp = requests.post(url, headers=self._get_headers(), json=payload, timeout=10)
            resp.raise_for_status()
            result = resp.json()
            current_app.logger.info(f'Shelly gate pulse triggered: {result}')
            return True, 'Gate opened successfully'
        except requests.RequestException as e:
            current_app.logger.error(f'Shelly pulse failed: {e}')
            raise SmartLockError(f'Failed to pulse gate: {e}')

    def turn_on(self):
        """Turn relay ON (keep gate open)"""
        if not self.is_configured():
            raise SmartLockError('Shelly not configured')

        if self.in_cloud_mode:
            try:
                result = self._cloud_relay_control(on=True)
                current_app.logger.info(f'Shelly relay ON via cloud: {result}')
                return True, 'Gate opened'
            except requests.RequestException as e:
                raise SmartLockError(f'Failed to open gate: {e}')

        url = f'{self._get_base_url()}/rpc/Switch.Set'
        payload = {'id': self.channel, 'on': True}

        try:
            resp = requests.post(url, headers=self._get_headers(), json=payload, timeout=5)
            resp.raise_for_status()
            return True, 'Gate opened'
        except requests.RequestException as e:
            raise SmartLockError(f'Failed to open gate: {e}')

    def turn_off(self):
        """Turn relay OFF (close gate)"""
        if not self.is_configured():
            raise SmartLockError('Shelly not configured')

        if self.in_cloud_mode:
            try:
                result = self._cloud_relay_control(on=False)
                current_app.logger.info(f'Shelly relay OFF via cloud: {result}')
                return True, 'Gate closed'
            except requests.RequestException as e:
                raise SmartLockError(f'Failed to close gate: {e}')

        url = f'{self._get_base_url()}/rpc/Switch.Set'
        payload = {'id': self.channel, 'on': False}

        try:
            resp = requests.post(url, headers=self._get_headers(), json=payload, timeout=5)
            resp.raise_for_status()
            return True, 'Gate closed'
        except requests.RequestException as e:
            raise SmartLockError(f'Failed to close gate: {e}')


class NukiService:
    """Service to control Nuki Smart Lock Ultra (Apartment door)"""

    def __init__(self, apartment):
        self.apartment = apartment
        self.enabled = apartment.nuki_enabled
        self.smartlock_id = apartment.nuki_smartlock_id
        self.token = apartment.nuki_web_token
        self.base_url = apartment.nuki_web_base_url or 'https://api.nuki.io'
        self.action = apartment.nuki_unlock_action or 'unlock'

    def is_configured(self):
        """Check if Nuki is properly configured"""
        return self.enabled and bool(self.smartlock_id) and bool(self.token)

    # Nuki Web API lock actions (generic /action endpoint).
    # 1 = unlock (turn cylinder), 2 = lock, 3 = unlatch (unlock + open door).
    ACTION_CODES = {'unlock': 1, 'lock': 2, 'unlatch': 3}

    def _get_headers(self):
        """Get headers for Nuki Web API"""
        return {
            'Authorization': f'Bearer {self.token}',
            'Content-Type': 'application/json',
            'Accept': 'application/json',
        }

    def _post_action(self, action_code):
        """Send a generic lock action via POST /smartlock/{id}/action.

        The dedicated /action/unlock and /action/unlatch routes don't exist in
        the Web API; unlatching must go through the generic endpoint.
        """
        url = f'{self.base_url}/smartlock/{self.smartlock_id}/action'
        payload = {'action': action_code}
        try:
            resp = requests.post(url, headers=self._get_headers(), json=payload, timeout=15)
            resp.raise_for_status()
            return resp
        except requests.RequestException as e:
            current_app.logger.error(f'Nuki action {action_code} failed: {e}')
            if hasattr(e, 'response') and e.response is not None:
                try:
                    error_detail = e.response.json()
                    raise SmartLockError(f'Nuki error: {error_detail}')
                except (ValueError, TypeError):
                    pass
            raise SmartLockError(f'Failed to send action {action_code}: {e}')

    def get_status(self):
        """Get Nuki Smart Lock status"""
        if not self.is_configured():
            raise SmartLockError('Nuki not configured')

        url = f'{self.base_url}/smartlock/{self.smartlock_id}'
        try:
            resp = requests.get(url, headers=self._get_headers(), timeout=10)
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as e:
            raise SmartLockError(f'Failed to get Nuki status: {e}')

    def _get_auths(self):
        """List all authorizations (incl. keypad codes) for the smart lock."""
        url = f'{self.base_url}/smartlock/{self.smartlock_id}/auth'
        try:
            resp = requests.get(url, headers=self._get_headers(), timeout=10)
            resp.raise_for_status()
            return resp.json() if isinstance(resp.json(), list) else []
        except requests.RequestException as e:
            raise SmartLockError(f'Failed to list Nuki authorizations: {e}')

    @staticmethod
    def _generate_keypad_code(existing_codes):
        """Generate a 6-digit code: no 0, must not start with '12', unique on device."""
        import secrets

        while True:
            code = ''.join(secrets.choice('123456789') for _ in range(6))
            if code.startswith('12'):
                continue
            if code not in existing_codes:
                return code

    def create_keypad_code(self, name, allowed_from, allowed_until):
        """Create a temporary Nuki Keypad 2 PIN.

        allowed_from / allowed_until must be timezone-aware UTC datetimes.
        Returns the generated 6-digit code (as str).
        """
        if not self.is_configured():
            raise SmartLockError('Nuki not configured')

        existing = {
            str(auth['code'])
            for auth in self._get_auths()
            if auth.get('type') == 13 and auth.get('code')
        }
        code = self._generate_keypad_code(existing)

        payload = {
            'name': (name or 'Guest')[:20],
            'type': 13,
            'code': int(code),
            'smartlockIds': [int(self.smartlock_id)],
            'allowedFromDate': allowed_from.strftime('%Y-%m-%dT%H:%M:%S.000Z'),
            'allowedUntilDate': allowed_until.strftime('%Y-%m-%dT%H:%M:%S.000Z'),
            'allowedWeekDays': 127,
            'allowedFromTime': 0,
            'allowedUntilTime': 0,
        }
        url = f'{self.base_url}/smartlock/auth'
        try:
            resp = requests.put(url, headers=self._get_headers(), json=payload, timeout=15)
            resp.raise_for_status()
        except requests.RequestException as e:
            current_app.logger.error(f'Nuki create keypad code failed: {e}')
            raise SmartLockError(f'Failed to create keypad code: {e}')

        current_app.logger.info(f'Nuki keypad code created for {name}')
        return code

    def find_keypad_auth_id(self, code, attempts=6):
        """Return the Nuki auth 'id' for a given keypad code, or None.

        Keypad auths are created asynchronously on the device, so we poll
        a few times before giving up.
        """
        import time

        for _ in range(attempts):
            try:
                for auth in self._get_auths():
                    if auth.get('type') == 13 and str(auth.get('code')) == str(code):
                        return auth.get('id')
            except SmartLockError:
                pass
            time.sleep(3)
        return None

    def revoke_keypad_code(self, auth_id):
        """Delete a keypad code authorization from the smart lock."""
        if not self.is_configured():
            raise SmartLockError('Nuki not configured')
        url = f'{self.base_url}/smartlock/{self.smartlock_id}/auth/{auth_id}'
        try:
            resp = requests.delete(url, headers=self._get_headers(), timeout=15)
            resp.raise_for_status()
            current_app.logger.info(f'Nuki keypad code {auth_id} revoked')
            return True
        except requests.RequestException as e:
            current_app.logger.error(f'Nuki revoke keypad code failed: {e}')
            raise SmartLockError(f'Failed to revoke keypad code: {e}')

    def unlock(self):
        """Unlock the door (or unlatch based on configuration)"""
        if not self.is_configured():
            raise SmartLockError('Nuki not configured')

        action_code = self.ACTION_CODES.get(self.action, 1)
        resp = self._post_action(action_code)
        current_app.logger.info(f'Nuki {self.action} triggered (action={action_code}): {resp.status_code}')
        return True, f'Door {self.action}ed successfully'

    def lock(self):
        """Lock the door"""
        if not self.is_configured():
            raise SmartLockError('Nuki not configured')

        resp = self._post_action(self.ACTION_CODES['lock'])
        current_app.logger.info(f'Nuki lock triggered (action=2): {resp.status_code}')
        return True, 'Door locked successfully'


def get_shelly_service(apartment):
    """Get Shelly service instance for apartment"""
    return ShellyService(apartment)


def get_nuki_service(apartment):
    """Get Nuki service instance for apartment"""
    return NukiService(apartment)


def revoke_reservation_keypad(reservation):
    """Revoke a reservation's Nuki keypad code, typically when it is cancelled.

    Best-effort against the Nuki API: even if the remote revoke fails (network,
    auth…) the stored code fields are cleared so guest-facing pages stop showing
    the code; the physical code is then only still valid if it was already
    programmed on the device (logged for manual follow-up). Returns True when a
    code existed.
    """
    from app.models import Apartment

    if not (reservation.keypad_code or reservation.keypad_auth_id):
        return False

    apartment = Apartment.query.first()
    if apartment and apartment.nuki_enabled:
        try:
            svc = get_nuki_service(apartment)
            if svc.is_configured() and reservation.keypad_auth_id:
                svc.revoke_keypad_code(reservation.keypad_auth_id)
        except SmartLockError:
            current_app.logger.warning(
                'Nuki keypad revoke failed for cancelled reservation #%s (code %s) — manual revoke needed',
                reservation.id,
                reservation.keypad_code,
            )

    reservation.keypad_code = None
    reservation.keypad_auth_id = None
    reservation.keypad_created_at = None
    return True


def revoke_expired_keypad_codes(apartment):
    """Revoke Nuki keypad codes for reservations whose stay has ended.

    Only codes with a confirmed keypad_auth_id are revoked; rows without one
    (created but not yet synced) are left alone and retried next run. Returns
    the number of codes revoked.
    """
    from datetime import date

    from app import db
    from app.models import Reservation

    if not apartment or not apartment.nuki_enabled:
        return 0

    svc = get_nuki_service(apartment)
    if not svc.is_configured():
        return 0

    today = date.today()
    expired = Reservation.query.filter(
        Reservation.keypad_code.isnot(None),
        Reservation.keypad_auth_id.isnot(None),
        Reservation.check_out <= today,
    ).all()

    revoked = 0
    for res in expired:
        try:
            svc.revoke_keypad_code(res.keypad_auth_id)
        except SmartLockError:
            current_app.logger.warning('Nuki keypad revoke failed for reservation #%s (code %s), retrying later', res.id, res.keypad_code)
            continue
        res.keypad_code = None
        res.keypad_auth_id = None
        res.keypad_created_at = None
        revoked += 1

    if revoked:
        db.session.commit()
        current_app.logger.info('Revoked %d expired Nuki keypad code(s)', revoked)
    return revoked


def trigger_gate_open(apartment):
    """Convenience function to pulse the gate relay"""
    service = get_shelly_service(apartment)
    return service.pulse_relay()


def trigger_door_unlock(apartment):
    """Convenience function to unlock the apartment door"""
    service = get_nuki_service(apartment)
    return service.unlock()


# Aliases for backward compatibility
def open_gate(shelly_url, shelly_auth_token=None):
    """Open gate using direct URL and auth token"""
    from types import SimpleNamespace

    apt = SimpleNamespace(
        shelly_enabled=True,
        shelly_host=shelly_url,
        shelly_auth_key=shelly_auth_token,
        shelly_relay_channel=0,
        shelly_pulse_duration=3,
    )
    service = ShellyService(apt)
    return service.pulse_relay()


def open_door(nuki_token, nuki_smartlock_id, nuki_base_url='https://api.nuki.io', nuki_action='unlock'):
    """Open door using direct Nuki credentials"""
    from types import SimpleNamespace

    apt = SimpleNamespace(
        nuki_enabled=True,
        nuki_smartlock_id=nuki_smartlock_id,
        nuki_web_token=nuki_token,
        nuki_web_base_url=nuki_base_url,
        nuki_unlock_action=nuki_action,
    )
    service = NukiService(apt)
    return service.unlock()

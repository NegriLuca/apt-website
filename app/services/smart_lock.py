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
        self.pulse_duration = apartment.shelly_pulse_duration or 3

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
        """Pulse the relay (open gate for pulse_duration seconds)"""
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

    def _get_headers(self):
        """Get headers for Nuki Web API"""
        return {
            'Authorization': f'Bearer {self.token}',
            'Content-Type': 'application/json',
            'Accept': 'application/json',
        }

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

    def unlock(self):
        """Unlock the door (or unlatch based on configuration)"""
        if not self.is_configured():
            raise SmartLockError('Nuki not configured')

        url = f'{self.base_url}/smartlock/{self.smartlock_id}/action/{self.action}'
        payload = {}

        try:
            resp = requests.post(url, headers=self._get_headers(), json=payload, timeout=15)
            resp.raise_for_status()
            result = resp.json()
            current_app.logger.info(f'Nuki {self.action} triggered: {result}')
            return True, f'Door {self.action}ed successfully'
        except requests.RequestException as e:
            current_app.logger.error(f'Nuki {self.action} failed: {e}')
            if hasattr(e, 'response') and e.response is not None:
                try:
                    error_detail = e.response.json()
                    raise SmartLockError(f'Nuki error: {error_detail}')
                except:
                    pass
            raise SmartLockError(f'Failed to {self.action} door: {e}')

    def lock(self):
        """Lock the door"""
        if not self.is_configured():
            raise SmartLockError('Nuki not configured')

        url = f'{self.base_url}/smartlock/{self.smartlock_id}/action/lock'
        payload = {}

        try:
            resp = requests.post(url, headers=self._get_headers(), json=payload, timeout=15)
            resp.raise_for_status()
            result = resp.json()
            current_app.logger.info(f'Nuki lock triggered: {result}')
            return True, 'Door locked successfully'
        except requests.RequestException as e:
            current_app.logger.error(f'Nuki lock failed: {e}')
            raise SmartLockError(f'Failed to lock door: {e}')


def get_shelly_service(apartment):
    """Get Shelly service instance for apartment"""
    return ShellyService(apartment)


def get_nuki_service(apartment):
    """Get Nuki service instance for apartment"""
    return NukiService(apartment)


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

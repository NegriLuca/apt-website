"""
Boiler Shelly automation — hot water boiler switch.

- ON  at 07:00 Europe/Rome on every check-in date (unconditional — ensures hot water even if gap logic skipped).
- OFF at 16:00 Europe/Rome on check-out dates ONLY when the gap to the next check-in is >=2 days (or no next booking).
  Gap 0 (same day) or 1 (next day) → keep boiler ON to avoid cycling.

Reuses the same Shelly Cloud credentials (SHELLY_CLOUD_SERVER / SHELLY_CLOUD_KEY) with a second device ID.
"""

import logging
import os
from datetime import date, timedelta

import requests
from flask import current_app

logger = logging.getLogger(__name__)


class BoilerShellyService:
    """Cloud-aware Shelly switch for the boiler. Mirrors ShellyService but for the boiler device."""

    def __init__(self, apartment):
        self.apartment = apartment
        # Prefer apartment column, fallback to env for initial setup
        env_device = (os.environ.get("SHELLY_BOILER_DEVICE_ID") or "").strip()
        env_host = (os.environ.get("SHELLY_BOILER_HOST") or "").strip()
        self.device_id = (getattr(apartment, "boiler_shelly_device_id", None) or env_device or getattr(apartment, "boiler_shelly_host", None) or env_host or "").strip()
        self.channel = getattr(apartment, "boiler_shelly_channel", 0) or 0
        try:
            self.channel = int(self.channel)
        except Exception:
            self.channel = 0
        # Cloud creds shared with gate shelly
        self.cloud_server = (os.environ.get("SHELLY_CLOUD_SERVER") or "").strip()
        self.cloud_key = (os.environ.get("SHELLY_CLOUD_KEY") or getattr(apartment, "shelly_auth_key", None) or "").strip()
        # Local fallback (rare)
        self.host = (getattr(apartment, "boiler_shelly_host", None) or env_host or "").strip()
        self.enabled = bool(getattr(apartment, "boiler_shelly_enabled", False))

    @property
    def in_cloud_mode(self):
        return bool(self.cloud_server and self.cloud_key and self.device_id)

    def is_configured(self):
        if not self.enabled:
            return False
        return bool(self.in_cloud_mode or self.host)

    def _cloud_url(self, endpoint):
        base = self.cloud_server
        if not base.startswith(("http://", "https://")):
            base = f"https://{base}"
        return f"{base}{endpoint}"

    def _cloud_relay_control(self, on: bool):
        payload = {"id": self.device_id, "channel": self.channel, "on": on}
        resp = requests.post(
            self._cloud_url("/v2/devices/api/set/switch"),
            params={"auth_key": self.cloud_key},
            json=payload,
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json() if resp.content else {}

    def _local_relay_control(self, on: bool):
        if not self.host:
            from app.services.smart_lock import SmartLockError
            raise SmartLockError("Boiler Shelly host not configured")
        base = self.host if self.host.startswith(("http://", "https://")) else f"http://{self.host}"
        url = f"{base}/rpc/Switch.Set"
        headers = {"Content-Type": "application/json"}
        auth = getattr(self.apartment, "shelly_auth_key", None)
        if auth:
            headers["Authorization"] = f"Bearer {auth}"
        resp = requests.post(url, headers=headers, json={"id": self.channel, "on": on}, timeout=10)
        resp.raise_for_status()
        return resp.json() if resp.content else {}

    def turn_on(self):
        if not self.is_configured():
            from app.services.smart_lock import SmartLockError
            raise SmartLockError("Boiler Shelly not configured (enable it and set device ID)")
        if self.in_cloud_mode:
            result = self._cloud_relay_control(True)
            logger.info("Boiler Shelly ON via cloud %s ch%s: %s", self.device_id, self.channel, result)
            return True, "Boiler turned ON"
        result = self._local_relay_control(True)
        logger.info("Boiler Shelly ON via local %s ch%s: %s", self.host, self.channel, result)
        return True, "Boiler turned ON"

    def turn_off(self):
        if not self.is_configured():
            from app.services.smart_lock import SmartLockError
            raise SmartLockError("Boiler Shelly not configured (enable it and set device ID)")
        if self.in_cloud_mode:
            result = self._cloud_relay_control(False)
            logger.info("Boiler Shelly OFF via cloud %s ch%s: %s", self.device_id, self.channel, result)
            return True, "Boiler turned OFF"
        result = self._local_relay_control(False)
        logger.info("Boiler Shelly OFF via local %s ch%s: %s", self.host, self.channel, result)
        return True, "Boiler turned OFF"

    def get_status(self):
        if not self.is_configured():
            from app.services.smart_lock import SmartLockError
            raise SmartLockError("Boiler Shelly not configured")
        if self.in_cloud_mode:
            resp = requests.post(
                self._cloud_url("/v2/devices/api/get"),
                params={"auth_key": self.cloud_key},
                json={"ids": [self.device_id], "select": ["status"]},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            return data[0] if isinstance(data, list) and data else data
        base = self.host if self.host.startswith(("http://", "https://")) else f"http://{self.host}"
        url = f"{base}/rpc/Shelly.GetStatus"
        headers = {"Content-Type": "application/json"}
        auth = getattr(self.apartment, "shelly_auth_key", None)
        if auth:
            headers["Authorization"] = f"Bearer {auth}"
        resp = requests.post(url, headers=headers, json={}, timeout=5)
        resp.raise_for_status()
        return resp.json()


def get_boiler_service(apartment=None):
    if apartment is None:
        from app.models import Apartment
        apartment = Apartment.query.first()
    return BoilerShellyService(apartment) if apartment else None


# ── Gap logic helpers ───────────────────────────────────────────────────────

def _confirmed_reservations():
    """All non-cancelled reservations sorted — covers direct/stripe + external iCal blocks."""
    from app.models import Reservation
    return Reservation.query.filter(Reservation.status != "cancelled").order_by(Reservation.check_in).all()


def _next_checkin_after(check_out: date):
    """Earliest check_in >= check_out among confirmed reservations, or None."""
    from app.models import Reservation
    nxt = (
        Reservation.query.filter(Reservation.status != "cancelled", Reservation.check_in >= check_out)
        .order_by(Reservation.check_in)
        .first()
    )
    return nxt.check_in if nxt else None


def should_turn_off_on_checkout(check_out: date) -> bool:
    """
    OFF rule for a checkout date: only if gap to next check-in is >=2 days (or no next booking).
    Gap 0 (same day turnover) and gap 1 (next-day check-in) keep boiler ON.
    """
    nxt = _next_checkin_after(check_out)
    if nxt is None:
        return True  # no future guest → save energy
    gap_days = (nxt - check_out).days
    return gap_days >= 2


def run_boiler_checkin_job(target_date: date | None = None) -> dict:
    """
    07:00 job — turn boiler ON for every check-in today (unconditional).
    Ensures hot water for arriving guest even if previous OFF was missed or boiler was manually off.
    Idempotent: sending ON when already ON is harmless.
    """
    if target_date is None:
        target_date = date.today()
    from app.models import Apartment, Reservation

    apartment = Apartment.query.first()
    if not apartment or not getattr(apartment, "boiler_shelly_enabled", False):
        return {"skipped": True, "reason": "boiler not enabled"}

    todays_checkins = Reservation.query.filter(Reservation.status != "cancelled", Reservation.check_in == target_date).all()
    if not todays_checkins:
        logger.info("Boiler check-in job %s: no check-ins", target_date)
        return {"skipped": True, "reason": "no check-ins", "date": str(target_date)}

    svc = get_boiler_service(apartment)
    if not svc or not svc.is_configured():
        logger.warning("Boiler check-in job %s: not configured", target_date)
        return {"skipped": True, "reason": "not configured"}

    try:
        svc.turn_on()
        logger.info("Boiler ON for check-in %s (%d reservation(s)): %s", target_date, len(todays_checkins), [r.id for r in todays_checkins])
        return {"success": True, "action": "ON", "date": str(target_date), "reservations": [r.id for r in todays_checkins]}
    except Exception as e:
        logger.exception("Boiler ON failed for check-in %s: %s", target_date, e)
        return {"success": False, "error": str(e), "date": str(target_date)}


def run_boiler_checkout_job(target_date: date | None = None) -> dict:
    """
    16:00 job — turn boiler OFF only when gap to next check-in >=2 days.
    If gap is 0 or 1, skip (keep boiler ON for imminent arrival).
    """
    if target_date is None:
        target_date = date.today()
    from app.models import Apartment, Reservation

    apartment = Apartment.query.first()
    if not apartment or not getattr(apartment, "boiler_shelly_enabled", False):
        return {"skipped": True, "reason": "boiler not enabled"}

    todays_checkouts = Reservation.query.filter(Reservation.status != "cancelled", Reservation.check_out == target_date).all()
    if not todays_checkouts:
        logger.info("Boiler check-out job %s: no check-outs", target_date)
        return {"skipped": True, "reason": "no check-outs", "date": str(target_date)}

    # OFF only if EVERY checkout today qualifies (avoids turning off when one turnover needs boiler).
    # If any reservation's next check-in is within 1 day, we must keep boiler ON for that imminent guest.
    should_off = all(should_turn_off_on_checkout(r.check_out) for r in todays_checkouts)
    if not should_off:
        # Log next gaps for visibility
        gaps = []
        for r in todays_checkouts:
            nxt = _next_checkin_after(r.check_out)
            gap = (nxt - r.check_out).days if nxt else None
            gaps.append(f"#{r.id} gap={gap} nxt={nxt}")
        logger.info("Boiler check-out job %s: skipped OFF (gap 0/1 keeps boiler ON) %s", target_date, gaps)
        return {"skipped": True, "reason": "gap 0/1 — keep ON", "date": str(target_date), "gaps": gaps}

    svc = get_boiler_service(apartment)
    if not svc or not svc.is_configured():
        logger.warning("Boiler check-out job %s: not configured", target_date)
        return {"skipped": True, "reason": "not configured"}

    try:
        svc.turn_off()
        logger.info("Boiler OFF for check-out %s (%d reservation(s))", target_date, len(todays_checkouts))
        return {"success": True, "action": "OFF", "date": str(target_date), "reservations": [r.id for r in todays_checkouts]}
    except Exception as e:
        logger.exception("Boiler OFF failed for check-out %s: %s", target_date, e)
        return {"success": False, "error": str(e), "date": str(target_date)}

"""
iCal sync service
─────────────────
Imports external calendars (Airbnb, Booking.com, VRBO, …) into the local DB.

Two-way logic:
  • NEW events in the feed   → create Reservation (status=confirmed, source=<platform>)
  • Events GONE from feed    → cancel the matching Reservation
  • Events already present   → no-op (idempotent by external_uid)
"""

import logging
import requests
from datetime import datetime, date
from icalendar import Calendar

from app import db
from app.models import ICalFeed, Reservation

log = logging.getLogger(__name__)


# ── low-level: sync a single feed URL ────────────────────────────────────────

def sync_feed(feed: ICalFeed) -> tuple[int, int]:
    """
    Pull one iCal feed and reconcile with DB.
    Returns (added, cancelled) counts.
    Raises requests.RequestException on network failure.
    """
    response = requests.get(feed.url, timeout=15)
    response.raise_for_status()

    cal = Calendar.from_ical(response.content)

    # Collect all UIDs present in the live feed
    live_uids: set[str] = set()
    to_add = []

    for component in cal.walk('VEVENT'):
        uid   = str(component.get('UID', ''))
        start = component.decoded('DTSTART', None)
        end   = component.decoded('DTEND',   None)

        if not uid or start is None or end is None:
            continue

        # Normalise datetime → date
        if isinstance(start, datetime):
            start = start.date()
        if isinstance(end, datetime):
            end = end.date()

        # Skip nonsensical events
        if end <= start:
            continue

        live_uids.add(uid)

        # Insert only if not already in DB
        if not Reservation.query.filter_by(external_uid=uid).first():
            to_add.append(Reservation(
                guest_name   = 'External booking',
                guest_email  = None,
                check_in     = start,
                check_out    = end,
                num_guests   = 1,
                status       = 'confirmed',
                source       = feed.source,
                external_uid = uid,
            ))
            log.info('iCal sync [%s]: adding %s (%s → %s)', feed.source, uid, start, end)

    for r in to_add:
        db.session.add(r)

    # ── Cancel DB reservations whose UID is no longer in the feed ──
    cancelled = 0
    if live_uids:
        orphans = Reservation.query.filter(
            Reservation.source      == feed.source,
            Reservation.external_uid.isnot(None),
            Reservation.status      == 'confirmed',
            Reservation.external_uid.notin_(live_uids),
        ).all()
    else:
        orphans = []  # empty feed → don't cancel everything (could be a fetch error)

    for r in orphans:
        r.status = 'cancelled'
        cancelled += 1
        log.info(
            'iCal sync [%s]: cancelling %s (%s → %s) — no longer in feed',
            feed.source, r.external_uid, r.check_in, r.check_out
        )

    db.session.commit()

    # Update last_synced_at
    feed.last_synced_at = datetime.utcnow()
    db.session.commit()

    return len(to_add), cancelled


# ── high-level: sync all active feeds ────────────────────────────────────────

def sync_all_feeds() -> tuple[int, int, list]:
    """
    Sync every active ICalFeed row.
    Returns (total_added, total_cancelled, list_of_error_strings).
    """
    feeds = ICalFeed.query.filter_by(active=True).all()
    total_added = total_cancelled = 0
    errors = []

    for feed in feeds:
        try:
            added, cancelled = sync_feed(feed)
            total_added     += added
            total_cancelled += cancelled
            log.info('iCal sync [%s]: +%d / -%d', feed.source, added, cancelled)
        except Exception as exc:
            msg = f'{feed.source}: {exc}'
            errors.append(msg)
            log.error('iCal sync failed — %s', msg)

    return total_added, total_cancelled, errors
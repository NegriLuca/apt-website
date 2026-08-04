"""
iCal sync service
─────────────────
Imports external calendars (Airbnb, Booking.com, VRBO, …) into the local DB.

Two-way logic:
  • NEW events in the feed   → create Reservation (status=confirmed, source=<platform>)
  • Events GONE from feed    → cancel the matching Reservation
  • Events already present   → no-op (idempotent by external_uid or check-in/out dates)
"""

import logging
import re
from datetime import date, datetime

import requests
from icalendar import Calendar

from app import db
from app.models import ICalFeed, Reservation

log = logging.getLogger(__name__)

# DB `source` values that represent OTA-imported (non-direct) reservations/blocks
EXTERNAL_SOURCES = {'airbnb', 'booking', 'booking_com', 'vrbo'}


def _classify_event(summary_text: str, description_text: str = '') -> tuple[bool, str]:
    """Return (is_block, guest_name) based on the iCal SUMMARY/DESCRIPTION text.

    iCal feeds only carry dates + a short label. Real Airbnb bookings contain a
    'Reservation'/'Reserved' marker or an HM-style code (also found in the
    reservation URL); 'Not available' / 'Blocked' entries are just calendar
    closures and should never be imported. Heuristic — not 100% reliable across
    platforms.
    """
    combined = f'{summary_text or ""} {description_text or ""}'.lower()
    if 'not available' in combined or 'blocked' in combined:
        return True, 'Blocked'
    if re.search(r'hm[a-z0-9]+', combined) or 'reservation' in combined or 'reserved' in combined:
        return False, 'External Guest'
    return True, 'Blocked'


def _source_variants(feed_source: str) -> set[str]:
    """Return the DB source values that belong to one platform feed.

    Feed labels are 'airbnb' / 'booking' / 'vrbo', but reservations are stored
    under the display source (e.g. 'booking_com'), so orphan/blocked lookups
    must match both spellings.
    """
    s = (feed_source or '').lower()
    variants = {s}
    if s == 'booking':
        variants.add('booking_com')
    elif s == 'booking_com':
        variants.add('booking')
    return variants


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
        uid = str(component.get('UID', ''))
        start = component.decoded('DTSTART', None)
        end = component.decoded('DTEND', None)

        if start is None or end is None:
            continue

        # Normalise datetime → date
        if isinstance(start, datetime):
            start = start.date()
        if isinstance(end, datetime):
            end = end.date()

        # Skip nonsensical events
        if end <= start:
            continue

        # Skip events that already ended — past nights are cleaned up daily
        if end < date.today():
            continue

        # If a UID exists, track it to prevent deletion downstream
        if uid:
            live_uids.add(uid)

        # ── DUP CHECK 1: Query by unique iCal UID string ──────────────────────
        existing_by_uid = None
        if uid:
            existing_by_uid = Reservation.query.filter_by(external_uid=uid, status='confirmed').first()

        # ── DUP CHECK 2: Fallback query by exact dates (For existing rows/manual blocks) ──
        existing_by_date = Reservation.query.filter_by(check_in=start, check_out=end, status='confirmed').first()

        # If it matches either check, skip it entirely (No-Op)
        if existing_by_uid or existing_by_date:
            # If the row exists but lacks a UID, update it in place so it's tracked correctly next time
            if existing_by_date and not existing_by_date.external_uid and uid:
                existing_by_date.external_uid = uid
            continue

        # ── SMART TEXT PARSING ──
        summary_text = str(component.get('summary', 'External Booking'))
        description_text = str(component.get('description') or '')

        # Determine a cleaner platform channel string based on the URL or title texts
        display_source = feed.source.lower()
        if 'airbnb' in feed.url.lower() or 'airbnb' in summary_text.lower():
            display_source = 'airbnb'
        elif 'booking.com' in feed.url.lower() or 'booking' in summary_text.lower():
            display_source = 'booking_com'

        # Skip calendar closures ("Not available", "Blocked", prep buffers, …) —
        # they are NOT real bookings and should not block the calendar.
        is_block, _ = _classify_event(summary_text, description_text)
        if is_block:
            log.info('iCal sync [%s]: skipping block %s (%s → %s)', display_source, uid, start, end)
            continue

        # Attempt to extract platform codes if explicitly present in titles
        # Example: Airbnb strings look like "Reservation Reserved - HMXXXXXXXX"
        booking_code = ''
        match = re.search(r'HM[A-Z0-9]+', f'{summary_text} {description_text}', re.IGNORECASE)
        if match:
            booking_code = f' ({match.group(0)})'
        clean_guest_name = f'External Guest{booking_code}'

        # If it doesn't exist anywhere, it's a completely new booking!
        to_add.append(
            Reservation(
                guest_name=clean_guest_name,
                guest_email=None,
                check_in=start,
                check_out=end,
                num_guests=1,
                status='confirmed',
                source=display_source,
                external_uid=uid if uid else None,
                is_block=False,
                # Standardized parameters matching your database updates
                total_price=0.0,
                payment_status='n/a',
                payment_method='automatic',
            )
        )
        log.info('iCal sync [%s]: adding %s (%s → %s)', display_source, uid, start, end)

    for r in to_add:
        db.session.add(r)

    # ── Cancel DB reservations whose UID is no longer in the feed ──
    cancelled = 0
    if live_uids:
        orphans = Reservation.query.filter(
            Reservation.source.in_(_source_variants(feed.source)),
            Reservation.external_uid.isnot(None),
            Reservation.status == 'confirmed',
            Reservation.external_uid.notin_(live_uids),
        ).all()
    else:
        orphans = []  # empty feed → don't cancel everything (could be a fetch error)

    for r in orphans:
        r.status = 'cancelled'
        cancelled += 1
        log.info(
            'iCal sync [%s]: cancelling %s (%s → %s) — no longer in feed',
            feed.source,
            r.external_uid,
            r.check_in,
            r.check_out,
        )

    db.session.commit()

    # Update last_synced_at using modern timezone-aware logic
    feed.last_synced_at = datetime.now()
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
            total_added += added
            total_cancelled += cancelled
            log.info('iCal sync [%s]: +%d / -%d', feed.source, added, cancelled)
        except Exception as exc:
            msg = f'{feed.source}: {exc}'
            errors.append(msg)
            log.error('iCal sync failed — %s', msg)

    return total_added, total_cancelled, errors


def get_blocked_dates():
    """Return a sorted list of ISO-date strings that are blocked by iCal feeds."""
    blocked = set()
    active_feeds = ICalFeed.query.filter_by(active=True).all()
    for feed in active_feeds:
        reservations = Reservation.query.filter(
            Reservation.source.in_(_source_variants(feed.source)),
            Reservation.status == 'confirmed',
        ).all()
        for r in reservations:
            current = r.check_in
            while current < r.check_out:
                blocked.add(current.isoformat())
                current += __import__('datetime').timedelta(days=1)
    return sorted(blocked)


def cleanup_past_external_reservations() -> int:
    """Hard-delete only KNOWN calendar blocks (is_block) that ended before today.

    Real OTA reservations are never auto-deleted — they keep their record.
    Blocks that are no longer imported (and any legacy ones) are removed to
    avoid clutter. Runs daily via APScheduler. Returns the number deleted.
    """
    from app.models import QuesturaLog, Ross1000Log

    cutoff = date.today()
    stale_blocks = Reservation.query.filter(
        Reservation.source.in_(EXTERNAL_SOURCES),
        Reservation.is_block.is_(True),
        Reservation.check_out < cutoff,
    ).all()

    for r in stale_blocks:
        QuesturaLog.query.filter_by(reservation_id=r.id).delete()
        Ross1000Log.query.filter_by(reservation_id=r.id).delete()
        db.session.delete(r)
        log.info('cleanup: deleting past %s block #%s (%s → %s)', r.source, r.id, r.check_in, r.check_out)

    if stale_blocks:
        db.session.commit()
    return len(stale_blocks)

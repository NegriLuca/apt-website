"""
iCal sync service
─────────────────
Imports external calendars (Airbnb, Booking.com, VRBO, …) into the local DB.

Two-way logic:
  • NEW events in the feed   → create Reservation (status=confirmed, source=<platform>)
  • Events GONE from feed    → cancel the matching Reservation (by UID or by dates)
  • Events already present   → no-op (idempotent by external_uid or check-in/out dates)
"""

import logging
import re
from datetime import date, datetime

import requests
from icalendar import Calendar
from sqlalchemy import or_

from app import db
from app.models import ICalFeed, Reservation

log = logging.getLogger(__name__)

# DB `source` values that represent OTA-imported (non-direct) reservations/blocks
EXTERNAL_SOURCES = {'airbnb', 'booking', 'booking_com', 'vrbo'}


def _classify_event(summary_text: str, description_text: str = '') -> tuple[bool, str]:
    """Return (is_block, guest_name) based on the iCal SUMMARY/DESCRIPTION text.

    Used only for non-Booking feeds (Airbnb, VRBO): iCal feeds carry dates + a
    short label, and real bookings are recognised by an HM-style code, or a
    'Reservation'/'Reserved' marker. Genuine calendar closures ('Blocked',
    plain 'Not available', prep buffers, …) are marked as blocks and must never
    be imported as reservations. Booking.com feeds never reach this function —
    they export only booked dates, so every event is a real reservation.
    Heuristic — not 100% reliable across platforms.
    """
    combined = f'{summary_text or ""} {description_text or ""}'.lower()
    # Real booking markers (Airbnb HM code, Reservation/Reserved wording…)
    if re.search(r'hm[a-z0-9]+', combined) or 'reservation' in combined or 'reserved' in combined:
        return False, 'External Guest'
    # Booking.com labels its real reservations "CLOSED - Not available" and
    # usually appends "Booking" / "Non disponibile" to the event text.
    if 'closed' in f'{summary_text or ""}'.lower() and ('booking' in combined or 'non disponibile' in combined):
        return False, 'External Guest'
    # Genuine calendar closures — skip these entirely.
    if 'not available' in combined or 'blocked' in combined:
        return True, 'Blocked'
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


def _display_source(feed_source: str, url: str = '', summary: str = '') -> str:
    """Return the canonical DB source for a feed event.

    Prefers the feed's configured source; falls back to URL/summary text hints
    so 'booking' feeds are always stored as 'booking_com' for consistency.
    """
    s = (feed_source or '').lower()
    if s in {'airbnb', 'booking', 'booking_com', 'vrbo'}:
        return 'booking_com' if s == 'booking' else s
    if 'airbnb' in url.lower() or 'airbnb' in summary.lower():
        return 'airbnb'
    if 'booking.com' in url.lower() or 'booking' in summary.lower():
        return 'booking_com'
    return s


_PLATFORM_LABELS = {
    'airbnb': 'Airbnb',
    'booking': 'Booking',
    'booking_com': 'Booking',
    'vrbo': 'VRBO',
}


def _guest_display_name(source: str, summary_text: str, description_text: str, uid: str) -> str:
    """Build a readable guest name for an OTA reservation.

    Uses the platform label plus an identifier: the HM code when present
    (Airbnb), otherwise a short fragment of the feed UID. Falls back to a
    generic 'External Guest' if nothing usable is found.
    """
    label = _PLATFORM_LABELS.get(source, (source or 'External').title())

    match = re.search(r'HM[A-Z0-9]+', f'{summary_text or ""} {description_text or ""}', re.IGNORECASE)
    if match:
        return f'{label} Guest ({match.group(0).upper()})'

    # Fall back to the UID — trim URL prefixes and long hashes to the last chunk
    uid_frag = ''
    if uid:
        frag = uid.rstrip('/').split('/')[-1].split(':')[-1]
        if frag and frag.lower() != 'vevent':
            uid_frag = frag[:24]

    if uid_frag:
        return f'{label} Guest ({uid_frag})'

    return f'{label} Guest'


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

    # Collect all UIDs and exact (start, end) date pairs present in the live feed
    live_uids: set[str] = set()
    live_date_pairs: set[tuple[date, date]] = set()
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
        live_date_pairs.add((start, end))

        # ── SMART TEXT PARSING ──
        summary_text = str(component.get('summary', 'External Booking'))
        description_text = str(component.get('description') or '')

        # Determine a cleaner platform channel string based on the feed source
        display_source = _display_source(feed.source, feed.url, summary_text)

        # Booking.com exports only booked dates, so every event in a Booking
        # feed is a real reservation — never a block. Other platforms (Airbnb,
        # VRBO) still carry genuine calendar closures ("Blocked", "Not
        # available", prep buffers, …) which are skipped entirely.
        feed_is_booking = (feed.source or '').lower() in {'booking', 'booking_com'}
        is_block = False if feed_is_booking else _classify_event(summary_text, description_text)[0]
        if is_block:
            log.info('iCal sync [%s]: skipping closure %s (%s → %s)', display_source, uid, start, end)
            continue

        # ── DUP CHECK 1: Query by unique iCal UID string ──────────────────────
        existing_by_uid = None
        if uid:
            existing_by_uid = Reservation.query.filter_by(external_uid=uid, status='confirmed').first()

        # ── DUP CHECK 2: Fallback query by exact dates (For existing rows/manual blocks) ──
        existing_by_date = Reservation.query.filter_by(check_in=start, check_out=end, status='confirmed').first()

        # If it matches either check, skip it entirely (No-Op) — but repair any
        # legacy row that was previously tagged as a calendar block. The old
        # classifier marked Booking.com's "CLOSED - Not available" real
        # reservations as blocks; re-syncing now flips those to reservations.
        if existing_by_uid or existing_by_date:
            existing = existing_by_uid or existing_by_date
            # If the row exists but lacks a UID, update it in place so it's tracked correctly next time
            if existing_by_date and not existing_by_date.external_uid and uid:
                existing_by_date.external_uid = uid
            if existing.is_block:
                existing.is_block = False
                existing.source = display_source
                log.info('iCal sync [%s]: repaired legacy block #%s → real reservation (%s → %s)', display_source, existing.id, start, end)
            continue

        # Build a readable guest name from the platform + HM code (Airbnb) or
        # the feed UID fragment.
        clean_guest_name = _guest_display_name(display_source, summary_text, description_text, uid)

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

    # ── Cancel DB reservations whose UID AND dates are no longer in the feed ──
    # A confirmed OTA reservation is stale if it matches no live event by UID
    # *and* no live event by exact dates. Checking dates too protects bookings
    # whose UID was regenerated by the platform, and — crucially — catches
    # legacy rows imported before UID tracking (external_uid IS NULL), which
    # used to survive forever once their event vanished from the feed.
    cancelled = 0
    if live_uids or live_date_pairs:
        orphans = Reservation.query.filter(
            Reservation.source.in_(_source_variants(feed.source)),
            Reservation.status == 'confirmed',
            or_(Reservation.external_uid.is_(None), Reservation.external_uid.notin_(live_uids)),
        ).all()
    else:
        orphans = []  # empty feed → don't cancel everything (could be a fetch error)

    for r in orphans:
        if (r.check_in, r.check_out) in live_date_pairs:
            continue  # still present by dates → legitimately booked (UID may have changed)
        r.status = 'cancelled'
        cancelled += 1
        log.info(
            'iCal sync [%s]: cancelling %s (%s → %s) — no longer in feed',
            feed.source,
            r.external_uid or '(no uid)',
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
    """Hard-delete only KNOWN calendar blocks that ended before today.

    Booking.com rows are never blocks or deleted: the platform exports only
    booked dates, so every Booking row is a real reservation. Any legacy
    Booking row tagged is_block (from the old classifier) is repaired to a real
    reservation instead. Genuine blocks (Airbnb/VRBO 'Blocked', 'Not
    available') that are no longer imported — and any legacy ones — are removed
    to avoid clutter. Runs daily via APScheduler. Returns the number of rows
    cleaned up.
    """
    from app.models import QuesturaLog, Ross1000Log

    cutoff = date.today()
    stale_blocks = Reservation.query.filter(
        Reservation.source.in_(EXTERNAL_SOURCES),
        Reservation.is_block.is_(True),
        Reservation.check_out < cutoff,
    ).all()

    cleaned = 0
    for r in stale_blocks:
        # Booking.com reservations are real — repair the tag, never delete.
        if r.source in {'booking', 'booking_com'}:
            r.is_block = False
            log.info('cleanup: repaired past %s block #%s → real reservation (%s → %s)', r.source, r.id, r.check_in, r.check_out)
            cleaned += 1
            continue
        QuesturaLog.query.filter_by(reservation_id=r.id).delete()
        Ross1000Log.query.filter_by(reservation_id=r.id).delete()
        db.session.delete(r)
        log.info('cleanup: deleting past %s block #%s (%s → %s)', r.source, r.id, r.check_in, r.check_out)
        cleaned += 1

    if cleaned:
        db.session.commit()
    return cleaned

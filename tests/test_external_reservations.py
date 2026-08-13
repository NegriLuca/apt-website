from datetime import date, timedelta
from unittest.mock import patch

from app import db
from app.models import Apartment, ICalFeed, Reservation


def _make_reservation(**kwargs):
    defaults = dict(
        guest_name='External Guest',
        guest_email=None,
        check_in=date.today() + timedelta(days=20),
        check_out=date.today() + timedelta(days=24),
        num_guests=2,
        status='confirmed',
        source='airbnb',
        external_uid=None,
    )
    defaults.update(kwargs)
    res = Reservation(**defaults)
    db.session.add(res)
    db.session.commit()
    return res


class TestExternalReservationManagement:
    """Admin can edit/delete reservations coming from OTA (Airbnb/Booking/VRBO)."""

    def test_admin_edit_external_reservation(self, client, app):
        from tests.conftest import login_admin

        login_admin(client)
        with app.app_context():
            res = _make_reservation()
            new_check_in = date.today() + timedelta(days=30)
            new_check_out = new_check_in + timedelta(days=3)

            resp = client.post(
                f'/admin/reservations/{res.id}/edit',
                data={
                    'check_in': new_check_in.isoformat(),
                    'check_out': new_check_out.isoformat(),
                    'num_guests': 3,
                },
                follow_redirects=True,
            )
            assert resp.status_code == 200
            db.session.refresh(res)
            assert res.check_in == new_check_in
            assert res.check_out == new_check_out
            assert res.num_guests == 3

    def test_admin_cannot_edit_direct_reservation(self, client, app):
        from tests.conftest import login_admin

        login_admin(client)
        with app.app_context():
            res = _make_reservation(source='direct')

            resp = client.post(
                f'/admin/reservations/{res.id}/edit',
                data={
                    'check_in': (date.today() + timedelta(days=10)).isoformat(),
                    'check_out': (date.today() + timedelta(days=13)).isoformat(),
                    'num_guests': 2,
                },
                follow_redirects=True,
            )
            assert resp.status_code == 200
            db.session.refresh(res)
            assert res.check_in == date.today() + timedelta(days=20)

    def test_admin_edit_rejects_overlap(self, client, app):
        from tests.conftest import login_admin

        login_admin(client)
        with app.app_context():
            res = _make_reservation()  # today+20 → today+24
            _make_reservation(
                guest_name='Blocker',
                check_in=date.today() + timedelta(days=30),
                check_out=date.today() + timedelta(days=33),
                external_uid='UID-BLOCK',
            )

            resp = client.post(
                f'/admin/reservations/{res.id}/edit',
                data={
                    'check_in': (date.today() + timedelta(days=31)).isoformat(),
                    'check_out': (date.today() + timedelta(days=34)).isoformat(),
                    'num_guests': 2,
                },
                follow_redirects=True,
            )
            assert resp.status_code == 200
            db.session.refresh(res)
            assert res.check_in == date.today() + timedelta(days=20)

    def test_admin_delete_external_reservation(self, client, app):
        from tests.conftest import login_admin

        login_admin(client)
        with app.app_context():
            res = _make_reservation()
            res_id = res.id

            resp = client.post(
                f'/admin/reservations/{res_id}/delete',
                follow_redirects=True,
            )
            assert resp.status_code == 200
            assert Reservation.query.get(res_id) is None

    def test_admin_cannot_delete_direct_reservation(self, client, app):
        from tests.conftest import login_admin

        login_admin(client)
        with app.app_context():
            res = _make_reservation(source='direct')
            res_id = res.id

            resp = client.post(
                f'/admin/reservations/{res_id}/delete',
                follow_redirects=True,
            )
            assert resp.status_code == 200
            assert Reservation.query.get(res_id) is not None


class TestIcalOrphanCleanup:
    """iCal sync must cancel reservations whose UID vanished from a Booking feed."""

    def test_booking_source_orphan_cancelled(self, app):
        from app.services.ical_sync import sync_feed

        with app.app_context():
            feed = ICalFeed(source='booking', url='https://example.com/feed.ics', active=True)
            db.session.add(feed)
            orphan = _make_reservation(
                source='booking_com',
                external_uid='OLD-UID',
                check_in=date.today() + timedelta(days=10),
                check_out=date.today() + timedelta(days=13),
            )
            db.session.commit()

            class FakeResponse:
                content = (
                    b'BEGIN:VCALENDAR\n'
                    b'BEGIN:VEVENT\n'
                    b'UID:NEW-UID\n'
                    b'SUMMARY:Reservation Reserved - HMNEW1234\n'
                    b'DTSTART:20270101\n'
                    b'DTEND:20270103\n'
                    b'END:VEVENT\n'
                    b'END:VCALENDAR\n'
                )

                def raise_for_status(self):
                    return None

            with patch('app.services.ical_sync.requests.get', return_value=FakeResponse()):
                added, cancelled = sync_feed(feed)

            db.session.refresh(orphan)
            assert orphan.status == 'cancelled'
            assert cancelled == 1
            assert added == 1

    def test_booking_without_uid_cancelled_when_event_vanishes(self, app):
        """A booking_com row with external_uid=NULL must be cancelled once its
        event disappears from the feed (legacy rows imported pre-UID tracking)."""
        from app.services.ical_sync import sync_feed

        with app.app_context():
            feed = ICalFeed(source='booking', url='https://example.com/feed.ics', active=True)
            db.session.add(feed)
            stale = _make_reservation(
                source='booking_com',
                external_uid=None,
                check_in=date.today() + timedelta(days=30),
                check_out=date.today() + timedelta(days=33),
            )
            db.session.commit()

            class FakeResponse:
                content = (
                    b'BEGIN:VCALENDAR\n'
                    b'BEGIN:VEVENT\n'
                    b'UID:UNRELATED-UID\n'
                    b'SUMMARY:CLOSED - Not available\n'
                    b'DTSTART:20270101\n'
                    b'DTEND:20270103\n'
                    b'END:VEVENT\n'
                    b'END:VCALENDAR\n'
                )

                def raise_for_status(self):
                    return None

            with patch('app.services.ical_sync.requests.get', return_value=FakeResponse()):
                added, cancelled = sync_feed(feed)

            db.session.refresh(stale)
            assert stale.status == 'cancelled'
            assert cancelled == 1

    def test_booking_without_uid_kept_and_repaired_when_still_in_feed(self, app):
        """A booking_com row with external_uid=NULL whose dates are still in the
        feed stays confirmed and gets its UID attached."""
        from app.services.ical_sync import sync_feed

        with app.app_context():
            feed = ICalFeed(source='booking', url='https://example.com/feed.ics', active=True)
            db.session.add(feed)
            row = _make_reservation(
                source='booking_com',
                external_uid=None,
                check_in=date(2027, 1, 1),
                check_out=date(2027, 1, 3),
            )
            db.session.commit()

            class FakeResponse:
                content = (
                    b'BEGIN:VCALENDAR\n'
                    b'BEGIN:VEVENT\n'
                    b'UID:ACTIVE-UID\n'
                    b'SUMMARY:CLOSED - Not available\n'
                    b'DTSTART:20270101\n'
                    b'DTEND:20270103\n'
                    b'END:VEVENT\n'
                    b'END:VCALENDAR\n'
                )

                def raise_for_status(self):
                    return None

            with patch('app.services.ical_sync.requests.get', return_value=FakeResponse()):
                added, cancelled = sync_feed(feed)

            db.session.refresh(row)
            assert row.status == 'confirmed'
            assert row.external_uid == 'ACTIVE-UID'
            assert cancelled == 0

    def test_booking_with_old_uid_kept_when_dates_still_in_feed(self, app):
        """A booking_com row whose stored UID was regenerated by the platform is
        kept as long as its dates still match a live event."""
        from app.services.ical_sync import sync_feed

        with app.app_context():
            feed = ICalFeed(source='booking', url='https://example.com/feed.ics', active=True)
            db.session.add(feed)
            row = _make_reservation(
                source='booking_com',
                external_uid='OLD-UID',
                check_in=date(2027, 2, 1),
                check_out=date(2027, 2, 3),
            )
            db.session.commit()

            class FakeResponse:
                content = (
                    b'BEGIN:VCALENDAR\n'
                    b'BEGIN:VEVENT\n'
                    b'UID:NEW-UID\n'
                    b'SUMMARY:CLOSED - Not available\n'
                    b'DTSTART:20270201\n'
                    b'DTEND:20270203\n'
                    b'END:VEVENT\n'
                    b'END:VCALENDAR\n'
                )

                def raise_for_status(self):
                    return None

            with patch('app.services.ical_sync.requests.get', return_value=FakeResponse()):
                added, cancelled = sync_feed(feed)

            db.session.refresh(row)
            assert row.status == 'confirmed'
            assert cancelled == 0
            assert added == 0  # matched by dates, no duplicate created


class TestIcalClassification:
    """Distinguish real reservations from calendar blocks via the SUMMARY text."""

    def test_classify_event(self, app):
        from app.services.ical_sync import _classify_event

        with app.app_context():
            is_block, name = _classify_event('Reservation Reserved - HM1234567890')
            assert is_block is False
            assert name == 'External Guest'

            is_block, name = _classify_event('Blocked')
            assert is_block is True
            assert name == 'Blocked'

            is_block, _ = _classify_event('Not available')
            assert is_block is True

    def test_sync_classifies_and_skips_past(self, app):
        from app.services.ical_sync import sync_feed

        with app.app_context():
            feed = ICalFeed(source='airbnb', url='https://example.com/feed.ics', active=True)
            db.session.add(feed)
            db.session.commit()

            class FakeResponse:
                content = (
                    b'BEGIN:VCALENDAR\n'
                    b'BEGIN:VEVENT\n'
                    b'UID:RES-1\n'
                    b'SUMMARY:Reservation Reserved - HM1234567890\n'
                    b'DTSTART:20270201\n'
                    b'DTEND:20270203\n'
                    b'END:VEVENT\n'
                    b'BEGIN:VEVENT\n'
                    b'UID:BLOCK-1\n'
                    b'SUMMARY:Blocked\n'
                    b'DTSTART:20270301\n'
                    b'DTEND:20270302\n'
                    b'END:VEVENT\n'
                    b'BEGIN:VEVENT\n'
                    b'UID:NA-1\n'
                    b'SUMMARY:Airbnb (Not available)\n'
                    b'DTSTART:20270305\n'
                    b'DTEND:20270306\n'
                    b'END:VEVENT\n'
                    b'BEGIN:VEVENT\n'
                    b'UID:PAST-1\n'
                    b'SUMMARY:Reservation Reserved - HM0000000000\n'
                    b'DTSTART:20200101\n'
                    b'DTEND:20200103\n'
                    b'END:VEVENT\n'
                    b'END:VCALENDAR\n'
                )

                def raise_for_status(self):
                    return None

            with patch('app.services.ical_sync.requests.get', return_value=FakeResponse()):
                added, cancelled = sync_feed(feed)

            assert added == 1  # only the real reservation; closures + past skipped
            res = Reservation.query.filter_by(external_uid='RES-1').first()
            block = Reservation.query.filter_by(external_uid='BLOCK-1').first()
            na = Reservation.query.filter_by(external_uid='NA-1').first()
            past = Reservation.query.filter_by(external_uid='PAST-1').first()
            assert res is not None and res.is_block is False
            assert 'HM1234567890' in res.guest_name
            assert block is None
            assert na is None
            assert past is None

    def test_sync_booking_closed_not_available_is_real_reservation(self, app):
        """Booking.com marks real bookings as 'CLOSED - Not available' — import them."""
        from app.services.ical_sync import sync_feed

        with app.app_context():
            feed = ICalFeed(source='booking', url='https://example.com/feed.ics', active=True)
            db.session.add(feed)
            db.session.commit()

            class FakeResponse:
                content = (
                    b'BEGIN:VCALENDAR\n'
                    b'BEGIN:VEVENT\n'
                    b'UID:BOOKING-REAL-1\n'
                    b'SUMMARY:CLOSED - Not available\n'
                    b'DESCRIPTION:CLOSED - Not available\\n24 \\xe2\\x80\\x93 26 ottobre 2026\\nBooking\\nNon disponibile\n'
                    b'DTSTART:20261024\n'
                    b'DTEND:20261027\n'
                    b'END:VEVENT\n'
                    b'END:VCALENDAR\n'
                )

                def raise_for_status(self):
                    return None

            with patch('app.services.ical_sync.requests.get', return_value=FakeResponse()):
                added, cancelled = sync_feed(feed)

            assert added == 1
            res = Reservation.query.filter_by(external_uid='BOOKING-REAL-1').first()
            assert res is not None
            assert res.is_block is False
            assert res.source == 'booking_com'

    def test_sync_booking_plain_not_available_is_real_reservation(self, app):
        """Booking exports only booked dates, so even a bare 'Not available' event is a reservation."""
        from app.services.ical_sync import sync_feed

        with app.app_context():
            feed = ICalFeed(source='booking', url='https://example.com/feed.ics', active=True)
            db.session.add(feed)
            db.session.commit()

            class FakeResponse:
                content = (
                    b'BEGIN:VCALENDAR\n'
                    b'BEGIN:VEVENT\n'
                    b'UID:BOOKING-REAL-2\n'
                    b'SUMMARY:Not available\n'
                    b'DTSTART:20261201\n'
                    b'DTEND:20261204\n'
                    b'END:VEVENT\n'
                    b'END:VCALENDAR\n'
                )

                def raise_for_status(self):
                    return None

            with patch('app.services.ical_sync.requests.get', return_value=FakeResponse()):
                added, cancelled = sync_feed(feed)

            assert added == 1
            res = Reservation.query.filter_by(external_uid='BOOKING-REAL-2').first()
            assert res is not None
            assert res.is_block is False
            assert res.source == 'booking_com'
            assert 'BOOKING-REAL-2' in res.guest_name

    def test_guest_name_from_uid_fragment(self, app):
        """Booking reservations get a readable name derived from the UID."""
        from app.services.ical_sync import _guest_display_name

        assert _guest_display_name('booking_com', 'Not available', '', 'https://secure.booking.com/feed/8F3K2L') == 'Booking Guest (8F3K2L)'
        assert _guest_display_name('airbnb', 'Reservation Reserved - HM1234567890', '', 'whatever') == 'Airbnb Guest (HM1234567890)'
        assert _guest_display_name('airbnb', 'Airbnb', 'hmABC12345 in description', 'ignored') == 'Airbnb Guest (HMABC12345)'
        assert _guest_display_name('vrbo', 'X', '', '') == 'VRBO Guest'

    def test_sync_repairs_legacy_block_booking(self, app):
        """A pre-existing Booking.com row wrongly tagged is_block=True gets repaired on re-sync."""
        from app.services.ical_sync import sync_feed

        with app.app_context():
            feed = ICalFeed(source='booking', url='https://example.com/feed.ics', active=True)
            db.session.add(feed)
            _make_reservation(
                source='booking_com',
                external_uid='BOOKING-REAL-1',
                is_block=True,
                check_in=date(2026, 10, 24),
                check_out=date(2026, 10, 27),
            )
            db.session.commit()

            class FakeResponse:
                content = (
                    b'BEGIN:VCALENDAR\n'
                    b'BEGIN:VEVENT\n'
                    b'UID:BOOKING-REAL-1\n'
                    b'SUMMARY:CLOSED - Not available\n'
                    b'DESCRIPTION:CLOSED - Not available\\n24 \\xe2\\x80\\x93 26 ottobre 2026\\nBooking\\nNon disponibile\n'
                    b'DTSTART:20261024\n'
                    b'DTEND:20261027\n'
                    b'END:VEVENT\n'
                    b'END:VCALENDAR\n'
                )

                def raise_for_status(self):
                    return None

            with patch('app.services.ical_sync.requests.get', return_value=FakeResponse()):
                added, cancelled = sync_feed(feed)

            assert added == 0  # already exists, just repaired
            res = Reservation.query.filter_by(external_uid='BOOKING-REAL-1').first()
            assert res is not None
            assert res.is_block is False
            assert res.source == 'booking_com'

    def test_reservation_recognised_via_description(self, app):
        from app.services.ical_sync import _classify_event

        with app.app_context():
            desc = (
                'Reserved\n12 – 16 settembre 2026\n'
                'Reservation URL: https://www.airbnb.com/hosting/reservations/details/HMWPZHY9AA\n'
                'Phone Number (Last 4 Digits): 3551\nAirbnb'
            )
            is_block, name = _classify_event('Airbnb', desc)
            assert is_block is False
            assert name == 'External Guest'


class TestExpiredKeypadRevocation:
    """Scheduled job revokes Nuki keypad codes once the stay has ended."""

    def test_revokes_expired_and_keeps_active(self, app):
        from unittest.mock import Mock

        from app.models import Apartment
        from app.services.smart_lock import revoke_expired_keypad_codes

        with app.app_context():
            apt = Apartment.query.first()
            apt.nuki_enabled = True
            apt.nuki_smartlock_id = '22806585863'
            apt.nuki_web_token = 'test-token'
            db.session.commit()

            expired = _make_reservation(
                source='direct',
                check_in=date.today() - timedelta(days=5),
                check_out=date.today() - timedelta(days=1),
            )
            expired.keypad_code = '111222'
            expired.keypad_auth_id = 'auth-expired'
            active = _make_reservation(
                source='direct',
                check_in=date.today() + timedelta(days=1),
                check_out=date.today() + timedelta(days=4),
            )
            active.keypad_code = '333444'
            active.keypad_auth_id = 'auth-active'
            db.session.commit()

            class FakeNuki:
                def __init__(self, apt):
                    pass

                def is_configured(self):
                    return True

                def revoke_keypad_code(self, auth_id):
                    assert auth_id == 'auth-expired'

            with patch('app.services.smart_lock.get_nuki_service', return_value=FakeNuki(None)):
                revoked = revoke_expired_keypad_codes(apt)

            assert revoked == 1
            assert db.session.get(Reservation, expired.id).keypad_code is None
            assert db.session.get(Reservation, expired.id).keypad_auth_id is None
            assert db.session.get(Reservation, active.id).keypad_code == '333444'

    def test_skips_without_configured_nuki(self, app):
        from app.models import Apartment
        from app.services.smart_lock import revoke_expired_keypad_codes

        with app.app_context():
            apt = Apartment.query.first()
            apt.nuki_enabled = False
            db.session.commit()

            res = _make_reservation(
                source='direct',
                check_in=date.today() - timedelta(days=5),
                check_out=date.today() - timedelta(days=1),
            )
            res.keypad_code = '555666'
            res.keypad_auth_id = 'auth-past'
            db.session.commit()

            assert revoke_expired_keypad_codes(apt) == 0
            assert db.session.get(Reservation, res.id).keypad_code == '555666'


class TestGuestMessageKeypadAutoGen:
    """Opening the guest-message page auto-generates a Nuki keypad code."""

    def test_auto_generates_code_when_configured(self, app, client):
        from tests.conftest import login_admin

        from app.models import Apartment

        with app.app_context():
            apt = Apartment.query.first()
            apt.nuki_enabled = True
            apt.nuki_smartlock_id = '22806585863'
            apt.nuki_web_token = 'test-token'
            db.session.commit()

            res = _make_reservation(source='direct')
            db.session.add(res)
            db.session.commit()
            rid = res.id

        login_admin(client)

        class FakeNuki:
            def __init__(self, apt):
                pass

            def is_configured(self):
                return True

            def create_keypad_code(self, name, start_utc, end_utc):
                return '123456'

            def find_keypad_auth_id(self, code, attempts=6):
                return 'auth-1'

        with patch('app.services.smart_lock.get_nuki_service', return_value=FakeNuki(None)):
            resp = client.get(f'/admin/communication/guest-message/{rid}')

        assert resp.status_code == 200
        assert b'123456' in resp.data  # keypad code rendered in the page
        with app.app_context():
            res = db.session.get(Reservation, rid)
            assert res.keypad_code == '123456'
            assert res.keypad_auth_id == 'auth-1'

    def test_skips_when_not_configured(self, app, client):
        from tests.conftest import login_admin

        with app.app_context():
            res = _make_reservation(source='direct')
            db.session.add(res)
            db.session.commit()
            rid = res.id

        login_admin(client)

        resp = client.get(f'/admin/communication/guest-message/{rid}')

        assert resp.status_code == 200
        assert b'Reservation details' in resp.data

    def test_message_uses_reservation_code_and_links(self, app, client):
        from tests.conftest import login_admin

        with app.app_context():
            res = _make_reservation(source='airbnb', external_uid='AIRBNB-UID-1')
            res.guest_name = 'Airbnb Guest (HMB4R8NMCZ)'
            res.num_guests = 3
            db.session.add(res)
            db.session.commit()
            rid = res.id

        login_admin(client)

        resp = client.get(f'/admin/communication/guest-message/{rid}')

        assert resp.status_code == 200
        html = resp.data.decode()
        # Greeting uses the reservation code, not the full dashboard name
        assert 'Benvenuto a' in html
        assert 'HMB4R8NMCZ' in html
        assert 'food_recommendations' in html
        assert '/attractions' in html
        assert '/house-rules' in html
        assert 'Ospiti: 3' in html
        assert 'ATTRAZIONI' in html
        assert 'portal' not in html.lower()
        # The message textareas must not contain the full dashboard name
        msg_start = html.index('id="msg-it"')
        msg_end = html.index('id="msg-en"')
        it_area = html[msg_start:msg_end]
        assert 'Airbnb Guest' not in it_area

    def test_update_guest_details(self, app, client):
        from tests.conftest import login_admin

        with app.app_context():
            res = _make_reservation(source='booking_com')
            db.session.add(res)
            db.session.commit()
            rid = res.id

        login_admin(client)

        resp = client.post(
            f'/admin/communication/guest-message/{rid}/update',
            data={'guest_name': 'Giulia Bianchi', 'num_guests': 2},
            follow_redirects=True,
        )

        assert resp.status_code == 200
        with app.app_context():
            res = db.session.get(Reservation, rid)
            assert res.guest_name == 'Giulia Bianchi'
            assert res.num_guests == 2
            assert res.num_adults == 2

    def test_message_uses_edited_name_instead_of_code(self, app, client):
        from tests.conftest import login_admin

        with app.app_context():
            res = _make_reservation(source='airbnb', external_uid='AIRBNB-UID-1')
            res.guest_name = 'Giulia Bianchi'
            res.num_guests = 2
            db.session.add(res)
            db.session.commit()
            rid = res.id

        login_admin(client)

        resp = client.get(f'/admin/communication/guest-message/{rid}')

        assert resp.status_code == 200
        html = resp.data.decode()
        msg_start = html.index('id="msg-it"')
        msg_end = html.index('id="msg-en"')
        it_area = html[msg_start:msg_end]
        assert 'Giulia Bianchi' in it_area
        assert 'Guest (' not in it_area
        assert 'Benvenuto a' in it_area

    def test_message_falls_back_to_code_for_auto_name(self, app, client):
        from tests.conftest import login_admin

        with app.app_context():
            res = _make_reservation(source='airbnb', external_uid='AIRBNB-UID-1')
            res.guest_name = 'Booking Guest (BOOK1)'
            db.session.add(res)
            db.session.commit()
            rid = res.id

        login_admin(client)

        resp = client.get(f'/admin/communication/guest-message/{rid}')

        assert resp.status_code == 200
        html = resp.data.decode()
        msg_start = html.index('id="msg-it"')
        msg_end = html.index('id="msg-en"')
        it_area = html[msg_start:msg_end]
        assert 'BOOK1' in it_area
        assert 'Booking Guest' not in it_area


class TestGuestSelfCheckinCompanions:
    """The online check-in form collects data for all guests per num_guests."""

    def test_checkin_form_renders_blocks_for_num_guests(self, app, client):
        with app.app_context():
            res = _make_reservation(source='booking_com')
            res.num_guests = 3
            res.checkin_token = 'tok-checkin-form'
            db.session.add(res)
            db.session.commit()
            token = res.checkin_token

        resp = client.get(f'/checkin/{token}')

        assert resp.status_code == 200
        html = resp.data.decode()
        assert html.count('name="guest_0_surname"') == 1
        assert html.count('name="guest_1_surname"') == 1
        assert html.count('name="guest_2_surname"') == 1
        assert html.count('name="guest_3_surname"') == 0

    def test_checkin_submit_stores_main_and_companions(self, app, client):
        with app.app_context():
            res = _make_reservation(source='booking_com')
            res.num_guests = 3
            res.checkin_token = 'tok-checkin-submit'
            db.session.add(res)
            db.session.commit()
            rid = res.id
            token = res.checkin_token

        form = {
            'guest_0_surname': 'Rossi',
            'guest_0_first_name': 'Mario',
            'guest_0_birth_date': '1985-04-12',
            'guest_0_birth_place': 'Rome',
            'guest_0_nationality': 'Italian',
            'guest_0_gender': 'M',
            'guest_0_document_type': 'passport',
            'guest_0_document_number': 'AA123',
            'guest_0_document_expiry': '2030-01-01',
            'guest_0_document_country': 'ITA',
            'guest_1_surname': 'Bianchi',
            'guest_1_first_name': 'Anna',
            'guest_1_birth_date': '1990-06-20',
            'guest_1_birth_place': 'Milan',
            'guest_1_nationality': 'Italian',
            'guest_1_gender': 'F',
            'guest_1_document_type': 'id_card',
            'guest_1_document_number': 'BB456',
            'guest_1_document_expiry': '2031-01-01',
            'guest_1_document_country': 'ITA',
            'guest_2_surname': 'Rossi',
            'guest_2_first_name': 'Luca',
            'guest_2_birth_date': '2012-09-01',
            'guest_2_birth_place': 'Rome',
            'guest_2_nationality': 'Italian',
            'guest_2_gender': 'M',
            'guest_2_document_type': 'passport',
            'guest_2_document_number': 'CC789',
            'guest_2_document_expiry': '2032-01-01',
            'guest_2_document_country': 'ITA',
        }

        resp = client.post(f'/checkin/{token}', data=form, follow_redirects=True)

        assert resp.status_code == 200
        with app.app_context():
            res = db.session.get(Reservation, rid)
            assert res.guest_surname == 'Rossi'
            assert res.guest_first_name == 'Mario'
            assert res.guest_document_number == 'AA123'
            assert res.checkin_completed_at is not None
            assert res.checkin_token_used is True
            assert len(res.companions) == 2
            assert res.companions[0]['surname'] == 'Bianchi'
            assert res.companions[0]['first_name'] == 'Anna'
            assert res.companions[0]['birth_date'] == '1990-06-20'
            assert res.companions[1]['first_name'] == 'Luca'


class TestCheckinCityTax:
    """The check-in page offers a city tax payment link."""

    def test_checkin_page_shows_tax_when_unpaid(self, app, client):
        with app.app_context():
            res = _make_reservation(source='booking_com')
            res.num_guests = 2
            res.num_adults = 2
            res.check_in = date.today() + timedelta(days=5)
            res.check_out = date.today() + timedelta(days=8)
            res.checkin_token = 'tok-checkin-tax-page'
            res.guest_city_tax_enabled = True
            db.session.add(res)
            db.session.commit()
            token = res.checkin_token

        resp = client.get(f'/checkin/{token}')

        assert resp.status_code == 200
        html = resp.data.decode()
        assert 'City Tax' in html
        assert '36.00' in html  # 3 nights x 2 adults x €6
        assert 'Pay City Tax Online' in html

    def test_checkin_page_hides_tax_when_toggle_off(self, app, client):
        with app.app_context():
            res = _make_reservation(source='booking_com')
            res.num_guests = 2
            res.num_adults = 2
            res.check_in = date.today() + timedelta(days=5)
            res.check_out = date.today() + timedelta(days=8)
            res.checkin_token = 'tok-checkin-tax-hidden'
            db.session.add(res)
            db.session.commit()
            token = res.checkin_token

        resp = client.get(f'/checkin/{token}')

        assert resp.status_code == 200
        html = resp.data.decode()
        assert 'Pay City Tax Online' not in html

    def test_checkin_tax_link_creates_session(self, app, client):
        from unittest.mock import MagicMock

        with app.app_context():
            res = _make_reservation(source='booking_com')
            res.num_guests = 2
            res.num_adults = 2
            res.check_in = date.today() + timedelta(days=5)
            res.check_out = date.today() + timedelta(days=8)
            res.checkin_token = 'tok-checkin-tax-link'
            res.guest_city_tax_enabled = True
            db.session.add(res)
            db.session.commit()
            token = res.checkin_token

        fake_session = MagicMock()
        fake_session.url = 'https://checkout.stripe.com/c/guest-tax'

        with patch('app.routes.helpers.create_tourist_tax_payment_session', return_value=fake_session) as mock_create:
            resp = client.get(f'/checkin/{token}/tax-link')

        mock_create.assert_called_once()
        assert resp.status_code == 302
        assert 'checkout.stripe.com/c/guest-tax' in resp.location

    def test_admin_pay_tax_recomputes_amount_from_guests(self, app, client):
        from unittest.mock import MagicMock

        from tests.conftest import login_admin

        with app.app_context():
            res = _make_reservation(source='booking_com')
            res.num_guests = 2
            res.num_adults = 2
            res.check_in = date.today() + timedelta(days=5)
            res.check_out = date.today() + timedelta(days=8)
            res.tourist_tax_amount = None  # never set → old bug produced a failure
            db.session.add(res)
            db.session.commit()
            rid = res.id

        login_admin(client)

        fake_session = MagicMock()
        fake_session.url = 'https://checkout.stripe.com/c/recomputed'

        with patch('app.routes.admin.create_tourist_tax_payment_session', return_value=fake_session) as mock_create:
            resp = client.post(f'/admin/communication/guest-message/{rid}/pay-tax')

        mock_create.assert_called_once()
        assert resp.status_code == 302
        assert 'checkout.stripe.com/c/recomputed' in resp.location
        with app.app_context():
            res = db.session.get(Reservation, rid)
            assert res.tourist_tax_amount == 36.0


class TestGuestMessageCityTax:
    """The guest-message page can collect city tax via Stripe or mark it cash."""

    def test_page_shows_tax_amount_and_unpaid(self, app, client):
        from tests.conftest import login_admin

        with app.app_context():
            res = _make_reservation(source='booking_com')
            res.num_guests = 2
            res.num_adults = 2
            res.check_in = date.today() + timedelta(days=5)
            res.check_out = date.today() + timedelta(days=8)  # 3 nights
            db.session.add(res)
            db.session.commit()
            rid = res.id

        login_admin(client)

        resp = client.get(f'/admin/communication/guest-message/{rid}')

        assert resp.status_code == 200
        html = resp.data.decode()
        assert 'City Tax' in html
        assert '36.00' in html  # 3 nights x 2 adults x €6
        assert 'Unpaid' in html
        assert 'Mark as paid in cash' in html

    def test_mark_tax_cash(self, app, client):
        from tests.conftest import login_admin

        with app.app_context():
            res = _make_reservation(source='booking_com')
            db.session.add(res)
            db.session.commit()
            rid = res.id

        login_admin(client)

        resp = client.post(
            f'/admin/communication/guest-message/{rid}/tax-cash',
            follow_redirects=True,
        )

        assert resp.status_code == 200
        with app.app_context():
            res = db.session.get(Reservation, rid)
            assert res.tourist_tax_paid is True

    def test_message_includes_tax_link_when_enabled(self, app, client):
        from tests.conftest import login_admin

        with app.app_context():
            res = _make_reservation(source='booking_com')
            res.num_guests = 2
            res.num_adults = 2
            res.check_in = date.today() + timedelta(days=5)
            res.check_out = date.today() + timedelta(days=8)
            res.guest_city_tax_enabled = True
            db.session.add(res)
            db.session.commit()
            rid = res.id

        login_admin(client)

        resp = client.get(f'/admin/communication/guest-message/{rid}')

        assert resp.status_code == 200
        html = resp.data.decode()
        assert 'Tassa di soggiorno' in html
        assert 'tax-link' in html

    def test_message_omits_tax_link_when_disabled(self, app, client):
        from tests.conftest import login_admin

        with app.app_context():
            res = _make_reservation(source='booking_com')
            res.num_guests = 2
            res.num_adults = 2
            res.check_in = date.today() + timedelta(days=5)
            res.check_out = date.today() + timedelta(days=8)
            db.session.add(res)
            db.session.commit()
            rid = res.id

        login_admin(client)

        resp = client.get(f'/admin/communication/guest-message/{rid}')

        assert resp.status_code == 200
        html = resp.data.decode()
        assert 'Tassa di soggiorno' not in html

    def test_mark_tax_unpaid_toggle(self, app, client):
        from tests.conftest import login_admin

        with app.app_context():
            res = _make_reservation(source='booking_com')
            res.tourist_tax_paid = True
            db.session.add(res)
            db.session.commit()
            rid = res.id

        login_admin(client)

        resp = client.post(
            f'/admin/communication/guest-message/{rid}/tax-unpaid',
            follow_redirects=True,
        )

        assert resp.status_code == 200
        with app.app_context():
            res = db.session.get(Reservation, rid)
            assert res.tourist_tax_paid is False

    def test_pay_tax_creates_stripe_session(self, app, client):
        from unittest.mock import MagicMock

        from tests.conftest import login_admin

        with app.app_context():
            res = _make_reservation(source='booking_com')
            res.tourist_tax_amount = 36.0
            res.num_adults = 2
            db.session.add(res)
            db.session.commit()
            rid = res.id

        login_admin(client)

        fake_session = MagicMock()
        fake_session.url = 'https://checkout.stripe.com/c/test'

        with patch('app.routes.admin.create_tourist_tax_payment_session', return_value=fake_session) as mock_create:
            resp = client.post(f'/admin/communication/guest-message/{rid}/pay-tax')

        mock_create.assert_called_once()
        assert resp.status_code == 302
        assert 'checkout.stripe.com/c/test' in resp.location

    def test_create_tourist_tax_session_zero_returns_none(self, app):
        from app.routes.helpers import create_tourist_tax_payment_session

        with app.app_context():
            res = _make_reservation(source='booking_com')
            res.tourist_tax_amount = 0.0
            db.session.add(res)
            db.session.commit()
            assert create_tourist_tax_payment_session(res) is None

    def test_guest_message_update_toggles_city_tax_per_reservation(self, app, client):
        from tests.conftest import login_admin

        with app.app_context():
            res = _make_reservation(source='booking_com')
            res.num_guests = 2
            res.num_adults = 2
            res.check_in = date.today() + timedelta(days=5)
            res.check_out = date.today() + timedelta(days=8)
            res.checkin_token = 'tok-toggle-tax'
            res.guest_city_tax_enabled = False
            db.session.add(res)
            db.session.commit()
            rid = res.id
            token = res.checkin_token

        login_admin(client)

        resp = client.post(
            f'/admin/communication/guest-message/{rid}/update',
            data={'guest_name': 'New Name', 'num_guests': 2, 'guest_city_tax_enabled': 'on'},
            follow_redirects=True,
        )

        assert resp.status_code == 200
        with app.app_context():
            res = db.session.get(Reservation, rid)
            assert res.guest_city_tax_enabled is True
            assert res.guest_name == 'New Name'

        html = client.get(f'/checkin/{token}').data.decode()
        assert 'Pay City Tax Online' in html

        # Turn it back off for this reservation
        resp = client.post(
            f'/admin/communication/guest-message/{rid}/update',
            data={'guest_name': 'New Name', 'num_guests': 2},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        with app.app_context():
            res = db.session.get(Reservation, rid)
            assert res.guest_city_tax_enabled is False
        html = client.get(f'/checkin/{token}').data.decode()
        assert 'Pay City Tax Online' not in html


class TestPastExternalCleanup:
    """Daily job removes only KNOWN blocks, never real reservations."""

    def test_cleanup_deletes_only_past_blocks(self, app):
        from app.services.ical_sync import cleanup_past_external_reservations

        with app.app_context():
            past_block = _make_reservation(
                source='airbnb',
                external_uid='PAST-BLOCK',
                is_block=True,
                check_in=date.today() - timedelta(days=3),
                check_out=date.today() - timedelta(days=1),
            )
            past_booking = _make_reservation(
                source='booking_com',
                external_uid='PAST-BOOKING',
                is_block=True,
                check_in=date.today() - timedelta(days=3),
                check_out=date.today() - timedelta(days=1),
            )
            past_res = _make_reservation(
                source='airbnb',
                external_uid='PAST-RES',
                is_block=False,
                check_in=date.today() - timedelta(days=3),
                check_out=date.today() - timedelta(days=1),
            )
            future_block = _make_reservation(
                source='airbnb',
                external_uid='FUT-BLOCK',
                is_block=True,
            )
            past_direct = _make_reservation(
                source='direct',
                check_in=date.today() - timedelta(days=3),
                check_out=date.today() - timedelta(days=1),
            )

            count = cleanup_past_external_reservations()

            assert count == 2
            assert Reservation.query.get(past_block.id) is None
            assert Reservation.query.get(past_booking.id) is not None
            assert Reservation.query.get(past_booking.id).is_block is False
            assert Reservation.query.get(past_res.id) is not None
            assert Reservation.query.get(future_block.id) is not None
            assert Reservation.query.get(past_direct.id) is not None

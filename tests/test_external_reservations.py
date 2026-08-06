from datetime import date, timedelta
from unittest.mock import patch

from app import db
from app.models import ICalFeed, Reservation


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
        data = resp.get_json()
        assert data['keypad_status'] == 'created'
        assert data['keypad_code'] == '123456'
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
        data = resp.get_json()
        assert data['keypad_status'] == 'not_configured'
        assert data['keypad_code'] is None


class TestPastExternalCleanup:
    """Daily job removes only KNOWN blocks, never real reservations."""

    def test_cleanup_deletes_only_past_blocks(self, app):
        from app.services.ical_sync import cleanup_past_external_reservations

        with app.app_context():
            past_block = _make_reservation(
                source='booking_com',
                external_uid='PAST-BLOCK',
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

            assert count == 1
            assert Reservation.query.get(past_block.id) is None
            assert Reservation.query.get(past_res.id) is not None
            assert Reservation.query.get(future_block.id) is not None
            assert Reservation.query.get(past_direct.id) is not None

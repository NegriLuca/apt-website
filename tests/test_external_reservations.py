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

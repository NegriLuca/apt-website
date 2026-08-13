from datetime import date, timedelta
from unittest.mock import patch

from app import db
from app.models import Apartment, Reservation


def _make_cancellable(**kwargs):
    defaults = dict(
        guest_name='Cancel Guest',
        guest_email='cancel@example.com',
        check_in=date.today() + timedelta(days=20),
        check_out=date.today() + timedelta(days=23),
        num_guests=2,
        status='confirmed',
        source='direct',
    )
    defaults.update(kwargs)
    res = Reservation(**defaults)
    db.session.add(res)
    db.session.commit()
    return res


class FakeNuki:
    def __init__(self, apt):
        self.revoked = []

    def is_configured(self):
        return True

    def revoke_keypad_code(self, auth_id):
        self.revoked.append(auth_id)


class TestDashboardHidesCancelled:
    def test_cancelled_absent_from_ledger(self, app, client):
        from tests.conftest import login_admin

        with app.app_context():
            _make_cancellable(guest_name='Active Guest A')
            _make_cancellable(guest_name='Active Guest B')
            _make_cancellable(guest_name='Hidden Cancel Guest', status='cancelled')
        login_admin(client)

        resp = client.get('/admin')
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert 'Active Guest A' in body
        assert 'Active Guest B' in body
        assert 'Hidden Cancel Guest' not in body


class TestCancelRevokesKeypad:
    def test_admin_cancel_revokes_keypad_and_clears_fields(self, app, client):
        from tests.conftest import login_admin

        with app.app_context():
            apt = Apartment.query.first()
            apt.nuki_enabled = True
            res = _make_cancellable(keypad_code='111222', keypad_auth_id='auth-1')
            db.session.commit()
            rid = res.id

        login_admin(client)
        fake = FakeNuki(None)
        with patch('app.services.smart_lock.get_nuki_service', return_value=fake):
            resp = client.post(f'/admin/reservations/{rid}/cancel', follow_redirects=True)

        assert resp.status_code == 200
        assert fake.revoked == ['auth-1']
        with app.app_context():
            res = db.session.get(Reservation, rid)
            assert res.status == 'cancelled'
            assert res.keypad_code is None
            assert res.keypad_auth_id is None

    def test_admin_cancel_clears_fields_even_without_nuki(self, app, client):
        from tests.conftest import login_admin

        with app.app_context():
            res = _make_cancellable(keypad_code='333444', keypad_auth_id='auth-2')
            db.session.commit()
            rid = res.id

        login_admin(client)
        resp = client.post(f'/admin/reservations/{rid}/cancel', follow_redirects=True)

        assert resp.status_code == 200
        with app.app_context():
            res = db.session.get(Reservation, rid)
            assert res.status == 'cancelled'
            assert res.keypad_code is None
            assert res.keypad_auth_id is None

    def test_guest_cancel_revokes_keypad(self, app, client):
        with app.app_context():
            apt = Apartment.query.first()
            apt.nuki_enabled = True
            res = _make_cancellable(cancel_token='cancel-tok-1', keypad_code='555666', keypad_auth_id='auth-3')
            db.session.commit()
            rid = res.id

        fake = FakeNuki(None)
        with patch('app.services.smart_lock.get_nuki_service', return_value=fake):
            with patch('app.routes.booking.send_cancellation_emails', return_value=True):
                resp = client.get('/cancel/cancel-tok-1', follow_redirects=True)

        assert resp.status_code == 200
        assert fake.revoked == ['auth-3']
        with app.app_context():
            res = db.session.get(Reservation, rid)
            assert res.status == 'cancelled'
            assert res.keypad_code is None
            assert res.keypad_auth_id is None


class TestCancelledGuestPagesDenied:
    def _make_cancelled_with_tokens(self):
        res = _make_cancellable(
            guest_name='Denied Guest',
            status='cancelled',
            access_token='access-tok-cancel',
            checkin_token='checkin-tok-cancel',
        )
        db.session.commit()
        return res

    def test_access_page_denied(self, app, client):
        with app.app_context():
            self._make_cancelled_with_tokens()
        assert client.get('/access/access-tok-cancel').status_code == 404

    def test_checkin_guide_denied(self, app, client):
        with app.app_context():
            self._make_cancelled_with_tokens()
        assert client.get('/checkin-guide/access-tok-cancel').status_code == 404

    def test_self_checkin_denied(self, app, client):
        with app.app_context():
            self._make_cancelled_with_tokens()
        assert client.get('/checkin/checkin-tok-cancel').status_code == 404

    def test_portal_denied(self, app, client):
        with app.app_context():
            self._make_cancelled_with_tokens()
        assert client.get('/portal/checkin-tok-cancel').status_code == 404

    def test_active_reservation_still_serves_pages(self, app, client):
        with app.app_context():
            _make_cancellable(
                guest_name='Active Guest',
                access_token='access-tok-active',
                checkin_token='checkin-tok-active',
            )
        # checkin-guide is the page served before the stay window; active → 200
        assert client.get('/checkin-guide/access-tok-active').status_code == 200
        assert client.get('/checkin/checkin-tok-active').status_code == 200
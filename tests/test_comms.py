"""Comms layer: Brevo HTTPS email + Slack webhook — never raise, never hang."""

from datetime import date

import pytest

from app import db
from app.models import Reservation
from app.routes.helpers import _send_brevo_email, send_pending_payment_email
from app.services.email_service import send_email
from app.services.slack import notify


class _FakeResp:
    def __init__(self, status_code=201, text='ok'):
        self.status_code = status_code
        self.text = text

    def json(self):
        return {'messageId': 'test'}


def _boom(*args, **kwargs):
    raise ConnectionError('network down (simulates Railway SMTP block)')


@pytest.fixture
def live_comms(app):
    """Real send paths: suppression off so HTTP is actually attempted."""
    app.config.update(
        TESTING=False, MAIL_SUPPRESS_SEND=False, BREVO_API_KEY='test-key', SLACK_WEBHOOK_URL='https://hooks.test/xxx'
    )
    yield app
    app.config.update(TESTING=True, MAIL_SUPPRESS_SEND=True)


def test_brevo_send_success(app, live_comms, monkeypatch):
    calls = {}

    def _ok(url, json=None, headers=None, timeout=None):
        calls.update(url=url, json=json, headers=headers, timeout=timeout)
        return _FakeResp(201)

    monkeypatch.setattr('requests.post', _ok)
    with app.app_context():
        assert send_email(['guest@example.com'], 'Subject', '<p>Hi</p>') is True
    assert calls['url'] == 'https://api.brevo.com/v3/smtp/email'
    assert calls['headers']['api-key'] == 'test-key'
    assert calls['json']['to'] == [{'email': 'guest@example.com'}]
    assert calls['json']['subject'] == 'Subject'
    assert calls['timeout'] == 10


def test_brevo_send_rejected(app, live_comms, monkeypatch):
    monkeypatch.setattr('requests.post', lambda *a, **k: _FakeResp(401, 'unauthorized'))
    with app.app_context():
        assert send_email(['guest@example.com'], 'Subject', '<p>Hi</p>') is False


def test_brevo_send_never_raises(app, live_comms, monkeypatch):
    monkeypatch.setattr('requests.post', _boom)
    with app.app_context():
        assert send_email(['guest@example.com'], 'Subject', '<p>Hi</p>') is False


def test_brevo_no_key_no_http(app, live_comms, monkeypatch):
    app.config.update(BREVO_API_KEY='')
    called = []
    monkeypatch.setattr('requests.post', lambda *a, **k: called.append(1) or _FakeResp(201))
    with app.app_context():
        assert send_email(['guest@example.com'], 'Subject', '<p>Hi</p>') is False
    assert called == []


def test_helpers_sender_contract(app, live_comms, monkeypatch):
    monkeypatch.setattr('requests.post', lambda *a, **k: _FakeResp(201))
    payload = {
        'sender': {'name': 'Lotto235 Garbatella', 'email': 'lotto235roma@gmail.com'},
        'to': [{'email': 'guest@example.com'}],
        'subject': 'Booking received',
        'htmlContent': '<p>ok</p>',
        'replyTo': {'email': 'back@example.com'},
    }
    with app.app_context():
        resp = _send_brevo_email(payload)
    assert resp.status_code in (200, 201, 202)


def test_booking_email_never_breaks_flow(app, live_comms, monkeypatch):
    """Wire-transfer booking email must return False (not raise) when net is down."""
    monkeypatch.setattr('requests.post', _boom)
    with app.app_context():
        res = Reservation(
            guest_name='Comms Guest',
            guest_email='comms@example.com',
            check_in=date(2026, 8, 10),
            check_out=date(2026, 8, 12),
            num_guests=2,
            status='pending',
            source='direct',
            total_price=200.0,
            payment_status='unpaid',
            payment_method='wire_transfer',
            cancel_token='tok-comms-123',
        )
        db.session.add(res)
        db.session.commit()
        assert send_pending_payment_email(res) is False


def test_contact_form_uses_inbox_and_slack_not_email(app, client, monkeypatch):
    """Contact messages land in the admin inbox + Slack; Brevo is never touched."""
    from app.models import Notification

    calls = []

    def _record(url, **k):
        calls.append(url)
        return _FakeResp(200, 'ok')

    monkeypatch.setattr('requests.post', _record)

    resp = client.post(
        '/contact',
        data={'name': 'Tester', 'email': 'tester@example.com', 'message': 'Hello, is this available in August?'},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert calls == []  # TESTING suppresses Slack before HTTP; Brevo must never be called
    with app.app_context():
        note = Notification.query.filter_by(category='contact').order_by(Notification.id.desc()).first()
        assert note is not None
        assert 'Tester' in note.title and 'tester@example.com' in note.title


def test_booking_admin_alerts_are_slack_only(app, live_comms, monkeypatch):
    """Guest flows ping Slack; Brevo is called exactly once (guest email)."""
    from app.routes.helpers import send_cancellation_emails, send_payment_verified_email

    urls = []
    monkeypatch.setattr('requests.post', lambda url, **k: urls.append(url) or _FakeResp(200, 'ok'))
    with app.app_context():
        res = Reservation(
            guest_name='Comms Guest',
            guest_email='comms@example.com',
            check_in=date(2026, 8, 10),
            check_out=date(2026, 8, 12),
            num_guests=2,
            status='confirmed',
            source='direct',
            total_price=200.0,
            payment_status='paid',
            payment_method='stripe',
            cancel_token='tok-comms-456',
        )
        db.session.add(res)
        db.session.commit()

        assert send_payment_verified_email(res) is True
        assert urls == ['https://api.brevo.com/v3/smtp/email', 'https://hooks.test/xxx']

        urls.clear()
        assert send_cancellation_emails(res) is True
        assert urls == ['https://api.brevo.com/v3/smtp/email', 'https://hooks.test/xxx']


def test_admin_checkin_notification_is_slack_only(app, live_comms, monkeypatch):
    from app.services.email_service import send_admin_checkin_notification

    urls = []
    monkeypatch.setattr('requests.post', lambda url, **k: urls.append(url) or _FakeResp(200, 'ok'))
    with app.app_context():
        res = Reservation(
            guest_name='Checkin Guest',
            guest_email='checkin@example.com',
            check_in=date(2026, 8, 10),
            check_out=date(2026, 8, 12),
            num_guests=1,
            status='confirmed',
            source='direct',
            total_price=100.0,
        )
        db.session.add(res)
        db.session.commit()
        assert send_admin_checkin_notification(res) is True
    assert urls == ['https://hooks.test/xxx']


def test_slack_success(app, live_comms, monkeypatch):
    monkeypatch.setattr('requests.post', lambda *a, **k: _FakeResp(200, 'ok'))
    with app.app_context():
        assert notify('hello') is True


def test_slack_never_raises(app, live_comms, monkeypatch):
    monkeypatch.setattr('requests.post', _boom)
    with app.app_context():
        assert notify('hello') is False


def test_slack_no_url_no_http(app, live_comms, monkeypatch):
    app.config.update(SLACK_WEBHOOK_URL='')
    called = []
    monkeypatch.setattr('requests.post', lambda *a, **k: called.append(1) or _FakeResp(200, 'ok'))
    with app.app_context():
        assert notify('hello') is False
    assert called == []

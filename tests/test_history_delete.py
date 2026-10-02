from datetime import date, timedelta

from app import db
from app.models import Earning, Receipt, Reservation
from tests.conftest import login_admin


def _past_reservation(source='airbnb', status='confirmed', days_ago=30, length=4):
    check_out = date.today() - timedelta(days=days_ago)
    r = Reservation(
        guest_name='History Guest',
        guest_email='hist@example.com',
        check_in=check_out - timedelta(days=length),
        check_out=check_out,
        num_guests=2,
        status=status,
        source=source,
        total_price=200.0,
        payment_status='paid',
        payment_method='cash',
    )
    db.session.add(r)
    db.session.commit()
    return r


def test_requires_login(app, client):
    with app.app_context():
        res = Reservation(
            guest_name='G',
            check_in=date(2026, 1, 1),
            check_out=date(2026, 1, 3),
            num_guests=1,
            status='confirmed',
            source='airbnb',
            total_price=100.0,
        )
        db.session.add(res)
        db.session.commit()
        rid = res.id
    resp = client.post(f'/admin/history/delete/{rid}')
    assert resp.status_code in (302, 401, 403)


def test_delete_past_external(app, client):
    login_admin(client)
    with app.app_context():
        r = _past_reservation(source='airbnb')
        rid = r.id
    resp = client.post(f'/admin/history/delete/{rid}', follow_redirects=True)
    assert resp.status_code == 200
    assert b'permanently deleted' in resp.data
    with app.app_context():
        assert Reservation.query.get(rid) is None


def test_delete_past_direct_allowed(app, client):
    """Unlike the dashboard delete, History cleanup also removes direct/test rows."""
    login_admin(client)
    with app.app_context():
        r = _past_reservation(source='direct')
        rid = r.id
    resp = client.post(f'/admin/history/delete/{rid}', follow_redirects=True)
    assert resp.status_code == 200
    with app.app_context():
        assert Reservation.query.get(rid) is None


def test_earning_kept_but_unlinked(app, client):
    login_admin(client)
    with app.app_context():
        r = _past_reservation(source='airbnb')
        rid = r.id
        e = Earning(
            platform='airbnb',
            confirmation_code='DELTEST1',
            guest_name='History Guest',
            start_date=r.check_in,
            end_date=r.check_out,
            nights=4,
            amount=150.0,
            gross_earnings=180.0,
            withholding=-30.0,
            net=120.0,
            reservation_id=rid,
        )
        db.session.add(e)
        db.session.commit()
    client.post(f'/admin/history/delete/{rid}', follow_redirects=True)
    with app.app_context():
        assert Reservation.query.get(rid) is None
        kept = Earning.query.filter_by(confirmation_code='DELTEST1').first()
        assert kept is not None
        assert kept.reservation_id is None


def test_future_stay_blocked(app, client):
    login_admin(client)
    with app.app_context():
        r = Reservation(
            guest_name='Future Guest',
            check_in=date.today() + timedelta(days=5),
            check_out=date.today() + timedelta(days=8),
            num_guests=2,
            status='confirmed',
            source='airbnb',
            total_price=300.0,
        )
        db.session.add(r)
        db.session.commit()
        rid = r.id
    resp = client.post(f'/admin/history/delete/{rid}', follow_redirects=True)
    assert resp.status_code == 200
    assert b'Only past stays' in resp.data
    with app.app_context():
        assert Reservation.query.get(rid) is not None


def test_receipt_blocks_delete(app, client):
    login_admin(client)
    with app.app_context():
        r = _past_reservation(source='direct')
        rid = r.id
        db.session.add(
            Receipt(
                reservation_id=rid,
                year=2026,
                sequence=99,
                receipt_number='99/2026',
                stay_amount=200.0,
                total_amount=200.0,
            )
        )
        db.session.commit()
    resp = client.post(f'/admin/history/delete/{rid}', follow_redirects=True)
    assert resp.status_code == 200
    assert b'fiscal receipt' in resp.data
    with app.app_context():
        assert Reservation.query.get(rid) is not None

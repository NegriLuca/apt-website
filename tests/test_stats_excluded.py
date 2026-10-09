"""Reservation.stats_excluded — friends/family stays kept as rows, out of stats."""
from datetime import date, timedelta

from app import db
from app.models import Earning, Reservation
from app.routes.admin import _occupancy_rate
from app.services.finance import compute_finance
from tests.conftest import login_admin


def _past(days_ago_start: int, nights: int) -> tuple[date, date]:
    start = date.today() - timedelta(days=days_ago_start)
    return start, start + timedelta(days=nights)


def _make_res(**kwargs) -> Reservation:
    defaults = dict(
        guest_name='Guest',
        num_guests=2,
        status='confirmed',
        source='direct',
        is_block=False,
        total_price=0.0,
        payment_status='paid',
        payment_method='n/a',
    )
    defaults.update(kwargs)
    r = Reservation(**defaults)
    db.session.add(r)
    return r


def test_occupancy_removes_excluded_from_both_sides():
    start = date(2026, 1, 1)
    end = date(2026, 1, 11)  # 10-night window
    a = Reservation(guest_name='A', check_in=start, check_out=start + timedelta(days=4), num_guests=2, status='confirmed')
    b = Reservation(
        guest_name='Friends',
        check_in=start + timedelta(days=5),
        check_out=start + timedelta(days=8),
        num_guests=2,
        status='confirmed',
        stats_excluded=True,
    )
    assert _occupancy_rate([a, b], start, end) == 57.1  # 4 occupied / 7 available
    b.stats_excluded = False
    assert _occupancy_rate([a, b], start, end) == 70.0  # 7 / 10


def test_history_toggle_endpoint_flips_flag(client, app):
    with app.app_context():
        ci, co = _past(20, 3)
        r = _make_res(guest_name='Amici', check_in=ci, check_out=co, total_price=0.0)
        db.session.commit()
        rid = r.id
        assert r.stats_excluded in (False, None)

    login_admin(client)
    resp = client.post(f'/admin/history/toggle-stats/{rid}?status=all', follow_redirects=True)
    assert resp.status_code == 200
    with app.app_context():
        assert Reservation.query.get(rid).stats_excluded is True
    resp = client.post(f'/admin/history/toggle-stats/{rid}?status=all', follow_redirects=True)
    assert resp.status_code == 200
    with app.app_context():
        assert Reservation.query.get(rid).stats_excluded is False


def test_history_and_finance_exclude_flagged(client, app):
    with app.app_context():
        ci1, co1 = _past(30, 3)
        ci2, co2 = _past(20, 3)
        _make_res(guest_name='Paying', check_in=ci1, check_out=co1, total_price=300.0)
        friends = _make_res(guest_name='Amici', check_in=ci2, check_out=co2, total_price=0.0, stats_excluded=True)
        db.session.commit()
        earn = Earning(
            platform='booking', confirmation_code='FRIENDS1', guest_name='Amici',
            start_date=ci2, end_date=co2, payout_date=ci2, nights=3, currency='EUR',
            amount=200.0, service_fee=40.0, gross_earnings=240.0, withholding=-50.0, net=150.0,
            reservation_id=friends.id,
        )
        db.session.add(earn)
        db.session.commit()

    login_admin(client)
    resp = client.get('/admin/history')
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    # excluded row still listed, with badge
    assert 'Amici' in html
    assert 'no-stats' in html
    # avg/night over the paying stay only: 300 / 3 = 100 (not 300 / 6 = 50)
    assert '€100.00' in html

    with app.app_context():
        fin = compute_finance(ci1.year, ci1.month)
        assert fin['direct']['count'] == 1
        assert fin['direct']['gross'] == 300.0
        assert fin['ota']['count'] == 0  # linked payout excluded with the stay

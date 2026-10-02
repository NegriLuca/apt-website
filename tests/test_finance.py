from datetime import date

from app import db
from app.models import Earning, Reservation, RunningCost
from app.services.finance import compute_finance, sankey_data
from tests.conftest import login_admin


def _add_earning(code='TEST1', start=date(2026, 8, 20), end=date(2026, 8, 24)):
    e = Earning(
        platform='airbnb',
        confirmation_code=code,
        guest_name='Test Guest',
        start_date=start,
        end_date=end,
        payout_date=date(2026, 8, 24),
        nights=4,
        currency='EUR',
        amount=265.97,
        service_fee=62.02,
        cleaning_fee=40.00,
        gross_earnings=316.81,
        withholding=-68.88,
        net=197.09,
    )
    db.session.add(e)
    db.session.commit()
    return e


def _add_direct(total=300.0, check_in=date(2026, 8, 10), check_out=date(2026, 8, 12), method='cash'):
    r = Reservation(
        guest_name='Direct Guest',
        guest_email='direct@example.com',
        check_in=check_in,
        check_out=check_out,
        num_guests=2,
        status='confirmed',
        source='direct',
        total_price=total,
        payment_status='paid',
        payment_method=method,
    )
    db.session.add(r)
    db.session.commit()
    return r


def _add_cost(category='internet', amount=29.99, cost_date=date(2026, 8, 5), note='fiber'):
    c = RunningCost(cost_date=cost_date, category=category, amount=amount, note=note)
    db.session.add(c)
    db.session.commit()
    return c


def test_compute_monthly_balances(app):
    with app.app_context():
        _add_earning()
        _add_direct()
        _add_cost('internet', 29.99, date(2026, 8, 5))
        _add_cost('cleaning', 50.0, date(2026, 8, 21))
        _add_cost('electricity', 60.0, date(2026, 8, 25))
        _add_cost('imu', 500.0, date(2026, 8, 1))  # yearly-only: excluded from monthly

        fin = compute_finance(2026, 8)
        assert fin['gross'] == round(316.81 + 300.0, 2)
        assert fin['ota']['fees'] == round(316.81 - 265.97, 2)
        assert fin['cedolare_ota'] == 68.88
        assert fin['cedolare_direct'] == round(300.0 * 0.21, 2)
        # monthly excludes imu
        assert fin['imu'] == 0.0
        assert fin['running_total'] == round(29.99 + 50.0 + 60.0, 2)
        # conservation: payout - cedolare - running == net
        assert fin['net'] == round(fin['after_tax'] - fin['running_total'], 2)
        assert fin['payout'] == round(fin['gross'] - fin['fees'], 2)


def test_compute_yearly_includes_imu(app):
    with app.app_context():
        _add_earning()
        _add_cost('imu', 500.0, date(2026, 6, 16))
        _add_cost('internet', 30.0, date(2026, 3, 1))

        fin = compute_finance(2026, None)
        assert fin['imu'] == 500.0
        assert fin['running_total'] == 530.0
        assert fin['net'] == round(fin['after_tax'] - 530.0, 2)


def test_sankey_balances(app):
    with app.app_context():
        _add_earning()
        _add_direct()
        _add_cost('cleaning', 50.0, date(2026, 8, 21))

        fin = compute_finance(2026, 8)
        sk = sankey_data(fin)
        assert sk['labels'][0] == 'Gross revenue'
        assert 'Net profit' in sk['labels']
        # Gross splits into fees + payout
        gross_out = round(sum(v for s, v in zip(sk['sources'], sk['values']) if s == 0), 2)
        assert gross_out == round(fin['fees'] + fin['payout'], 2) == fin['gross']
        # Payout splits into cedolare + after_tax
        payout_idx = sk['labels'].index('Payout')
        payout_out = round(sum(v for s, v in zip(sk['sources'], sk['values']) if s == payout_idx), 2)
        assert payout_out == round(fin['cedolare'] + fin['after_tax'], 2) == fin['payout']


def test_finance_page_requires_admin(app, client):
    resp = client.get('/admin/finance')
    assert resp.status_code in (302, 401, 403)


def test_finance_crud(app, client):
    login_admin(client)
    # add
    resp = client.post(
        '/admin/finance?year=2026&month=8',
        data={'action': 'add', 'cost_date': '2026-08-05', 'category': 'internet', 'amount': '29.99', 'note': 'fiber'},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b'29.99' in resp.data
    with app.app_context():
        cost = RunningCost.query.filter_by(category='internet').first()
        assert cost is not None
        cid = cost.id
    # invalid amount rejected
    resp = client.post(
        '/admin/finance?year=2026&month=8',
        data={'action': 'add', 'cost_date': '2026-08-05', 'category': 'internet', 'amount': '-5', 'note': ''},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    # delete
    resp = client.post(
        '/admin/finance?year=2026&month=8',
        data={'action': 'delete', 'cost_id': str(cid)},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    with app.app_context():
        assert RunningCost.query.get(cid) is None

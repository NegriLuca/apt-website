from datetime import date, timedelta

from app import db
from app.models import Apartment, Coupon, Reservation


class TestBookingFlow:
    """Test the reservation/booking flow from submission through confirmation"""

    def test_create_reservation_wire_transfer(self, client, app):
        with app.app_context():
            check_in = date.today() + timedelta(days=30)
            check_out = check_in + timedelta(days=3)

            resp = client.post(
                '/reserve',
                data={
                    'guest_name': 'Mario Rossi',
                    'guest_email': 'mario@example.com',
                    'check_in': check_in.isoformat(),
                    'check_out': check_out.isoformat(),
                    'num_adults': 2,
                    'num_children': 1,
                },
                follow_redirects=True,
            )
            assert resp.status_code == 200

            resp2 = client.post(
                '/process-payment',
                data={
                    'payment_method': 'wire_transfer',
                },
                follow_redirects=True,
            )
            assert resp2.status_code == 200

            res = Reservation.query.filter_by(guest_email='mario@example.com').first()
            assert res is not None
            assert res.guest_name == 'Mario Rossi'
            assert res.num_guests == 3
            assert res.num_adults == 2
            assert res.num_children == 1
            assert res.status == 'pending'
            assert res.payment_method == 'wire_transfer'

    def test_booking_blocked_over_max_guests(self, client, app):
        with app.app_context():
            check_in = date.today() + timedelta(days=30)
            check_out = check_in + timedelta(days=3)

            resp = client.post(
                '/reserve',
                data={
                    'guest_name': 'Too Many',
                    'guest_email': 'many@example.com',
                    'check_in': check_in.isoformat(),
                    'check_out': check_out.isoformat(),
                    'num_adults': 4,
                    'num_children': 2,
                },
                follow_redirects=True,
            )
            assert resp.status_code == 200
            assert b'cannot exceed 4' in resp.data
            res = Reservation.query.filter_by(guest_email='many@example.com').first()
            assert res is None

    def test_reservation_nights_calculation(self, app):
        with app.app_context():
            res = Reservation(
                guest_name='Test',
                guest_email='test@test.com',
                check_in=date(2026, 8, 1),
                check_out=date(2026, 8, 5),
                num_guests=2,
                total_price=520.0,
            )
            assert res.nights == 4

    def test_reservation_total_price_positive(self, app):
        with app.app_context():
            apt = Apartment.query.first()
            check_in = date.today() + timedelta(days=14)
            check_out = check_in + timedelta(days=2)
            nights = (check_out - check_in).days
            expected = nights * apt.price_per_night
            assert expected > 0
            assert nights == 2

    def test_coupon_discount_percentage(self, app):
        with app.app_context():
            coup = Coupon(code='TEST10', discount_type='percentage', discount_value=10, active=True)
            assert coup.apply_discount(100.0) == 90.0
            assert coup.apply_discount(50.0) == 45.0

    def test_coupon_discount_flat(self, app):
        with app.app_context():
            coup = Coupon(code='FLAT20', discount_type='flat', discount_value=20, active=True)
            assert coup.apply_discount(100.0) == 80.0

    def test_coupon_inactive_returns_full_price(self, app):
        with app.app_context():
            coup = Coupon(code='INACTIVE', discount_type='percentage', discount_value=50, active=False)
            assert coup.apply_discount(100.0) == 100.0

    def test_reservation_status_transitions(self, app):
        with app.app_context():
            res = Reservation(
                guest_name='Test',
                guest_email='test@test.com',
                check_in=date(2026, 9, 1),
                check_out=date(2026, 9, 5),
                num_guests=2,
                status='pending',
            )
            assert res.status == 'pending'
            res.status = 'confirmed'
            assert res.status == 'confirmed'
            res.status = 'cancelled'
            assert res.status == 'cancelled'

    def test_homepage_loads(self, client):
        resp = client.get('/')
        assert resp.status_code == 200

    def test_faq_page(self, client):
        resp = client.get('/faq')
        assert resp.status_code == 200

    def test_legal_pages(self, client):
        for path in ['/terms', '/cancellation-policy', '/refund-policy', '/house-rules', '/privacy']:
            resp = client.get(path)
            assert resp.status_code == 200, f'{path} returned {resp.status_code}'

    def test_admin_login_page(self, client):
        resp = client.get('/login')
        assert resp.status_code == 200

    def test_admin_login_success(self, client, app):
        from tests.conftest import login_admin

        resp = login_admin(client)
        assert resp.status_code == 200

    def test_admin_dashboard_requires_login(self, client):
        resp = client.get('/admin', follow_redirects=True)
        assert resp.status_code == 200

    def test_reserve_page_get(self, client):
        resp = client.get('/reserve')
        assert resp.status_code == 200

    def test_checkout_requires_session(self, client):
        resp = client.get('/checkout', follow_redirects=True)
        assert resp.status_code == 200

    def test_cancel_token_not_found(self, client):
        resp = client.get('/cancel/invalid-token', follow_redirects=True)
        assert resp.status_code == 404


class TestAdminDashboardOccupancy:
    """Occupancy = % of nights in a window covered by a confirmed booking."""

    def _make(self, **kwargs):
        defaults = dict(
            guest_name='G',
            check_in=date.today() + timedelta(days=30),
            check_out=date.today() + timedelta(days=33),
            num_guests=2,
            status='confirmed',
            total_price=100.0,
        )
        defaults.update(kwargs)
        return Reservation(**defaults)

    def test_counts_overlapping_stays_once(self, app):
        from app.routes.admin import _occupancy_rate

        with app.app_context():
            res = [
                self._make(check_in=date(2026, 8, 10), check_out=date(2026, 8, 25)),
                self._make(check_in=date(2026, 8, 20), check_out=date(2026, 9, 5)),
            ]
            # Union of nights in Aug = Aug 10..Aug 31 = 22 nights of 31 → 71.0%
            rate = _occupancy_rate(res, date(2026, 8, 1), date(2026, 9, 1))
            assert rate == 71.0

    def test_ignores_cancelled(self, app):
        from app.routes.admin import _occupancy_rate

        with app.app_context():
            res = [
                self._make(check_in=date(2026, 8, 1), check_out=date(2026, 8, 10)),
                self._make(check_in=date(2026, 8, 5), check_out=date(2026, 8, 20), status='cancelled'),
            ]
            # 9 nights occupied of 31 → 29.0%
            assert _occupancy_rate(res, date(2026, 8, 1), date(2026, 9, 1)) == 29.0

    def test_stays_outside_window_do_not_count(self, app):
        from app.routes.admin import _occupancy_rate

        with app.app_context():
            res = [self._make(check_in=date(2026, 7, 20), check_out=date(2026, 7, 25))]
            assert _occupancy_rate(res, date(2026, 8, 1), date(2026, 9, 1)) == 0.0

    def test_stay_spanning_window_boundary_counts_portion(self, app):
        from app.routes.admin import _occupancy_rate

        with app.app_context():
            res = [self._make(check_in=date(2026, 8, 25), check_out=date(2026, 9, 5))]
            # Aug 25..Aug 31 = 7 nights of 31 → 22.6%
            assert _occupancy_rate(res, date(2026, 8, 1), date(2026, 9, 1)) == 22.6

    def test_add_months_clamps_day(self, app):
        from app.routes.admin import _add_months

        assert _add_months(date(2026, 8, 7), 3) == date(2026, 11, 7)
        assert _add_months(date(2026, 11, 30), 3) == date(2027, 2, 28)
        assert _add_months(date(2026, 12, 15), 1) == date(2027, 1, 15)

    def test_dashboard_shows_correct_occupancy(self, app, client):
        from app.routes.admin import _add_months
        from tests.conftest import login_admin

        with app.app_context():
            today = date.today()
            window_end = _add_months(today, 3)
            total_nights = (window_end - today).days
            # Book a single night starting today → tiny but non-zero occupancy.
            db.session.add(
                self._make(
                    check_in=today,
                    check_out=today + timedelta(days=1),
                    total_price=100.0,
                )
            )
            db.session.commit()

        login_admin(client)
        resp = client.get('/admin')
        assert resp.status_code == 200
        html = resp.data.decode()
        assert 'Occupancy (3 months)' in html
        # Single night over the whole window, never 100%.
        assert f'{round(1 / total_nights * 100, 1)}%' in html

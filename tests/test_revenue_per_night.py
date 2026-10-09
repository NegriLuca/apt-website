"""Reservation.revenue_per_night + €/night column in Dashboard/History tables."""
from datetime import date, timedelta

from app import db
from app.models import Reservation
from tests.conftest import login_admin


def test_revenue_per_night_property():
    r = Reservation(
        guest_name='G', check_in=date(2026, 5, 1), check_out=date(2026, 5, 4),
        num_guests=2, total_price=300.0,
    )
    assert r.nights == 3
    assert r.revenue_per_night == 100.0
    r.total_price = 0.0
    assert r.revenue_per_night == 0.0


def test_per_night_column_renders(client, app):
    with app.app_context():
        today = date.today()
        upcoming = Reservation(
            guest_name='Future', check_in=today + timedelta(days=2), check_out=today + timedelta(days=4),
            num_guests=2, status='confirmed', source='direct', total_price=250.0, payment_status='paid',
        )
        past = Reservation(
            guest_name='Past', check_in=today - timedelta(days=10), check_out=today - timedelta(days=7),
            num_guests=2, status='confirmed', source='direct', total_price=300.0, payment_status='paid',
        )
        db.session.add_all([upcoming, past])
        db.session.commit()

    login_admin(client)
    dash = client.get('/admin')
    assert dash.status_code == 200
    assert '€/night' in dash.get_data(as_text=True)
    assert '€125.00' in dash.get_data(as_text=True)  # 250 / 2 nights
    hist = client.get('/admin/history')
    assert hist.status_code == 200
    assert '€/night' in hist.get_data(as_text=True)
    assert '€100.00' in hist.get_data(as_text=True)  # 300 / 3 nights

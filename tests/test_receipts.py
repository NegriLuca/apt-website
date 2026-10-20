"""Ricevute — quietanza di pagamento, intestazione azienda vs persona fisica."""

from datetime import date, timedelta

import pytest

from app import db as _db
from app.models import Receipt, Reservation
from tests.conftest import login_admin


def _make_reservation(app, with_address=False):
    check_in = date.today() + timedelta(days=10)
    res = Reservation(
        guest_name='Mario Rossi',
        guest_email='mario@corp.it',
        check_in=check_in,
        check_out=check_in + timedelta(days=3),
        num_guests=1,
        total_price=150.0,
        tourist_tax_amount=18.0,
        source='direct',
        payment_method='wire_transfer',
        payment_status='paid',
        status='confirmed',
    )
    if with_address:
        res.guest_residence_address = 'Via Vecchia 1'
        res.guest_residence_city = 'Roma'
        res.guest_residence_country = 'IT'
    _db.session.add(res)
    _db.session.commit()
    return res.id


class TestCompanyReceipt:
    """Quietanza intestata a società/ente: non serve l'indirizzo del soggiornante."""

    def test_company_receipt_without_guest_address(self, app, client):
        res_id = _make_reservation(app)
        login_admin(client)
        resp = client.post(
            f'/admin/receipts/create/{res_id}',
            data={
                'entity_company': '1',
                'company_name': 'Rossi S.r.l.',
                'company_vat': '01234567890',
                'company_address': 'Via Milano 10, 00100 Roma',
                'guest_full_name': 'Mario Rossi',
                'guest_residence_address': '',
                'guest_residence_city': '',
                'guest_residence_country': '',
            },
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert 'Rossi S.r.l.' in resp.get_data(as_text=True)

        with app.app_context():
            receipt = Receipt.query.filter_by(reservation_id=res_id).first()
            assert receipt is not None
            assert receipt.entity_type == 'company'
            assert receipt.company_name == 'Rossi S.r.l.'
            assert receipt.company_vat == '01234567890'
            assert receipt.company_address == 'Via Milano 10, 00100 Roma'
            # il soggiornante resta indicato
            assert receipt.guest_full_name == 'Mario Rossi'

    def test_company_mode_does_not_write_guest_address_on_reservation(self, app, client):
        res_id = _make_reservation(app)
        login_admin(client)
        client.post(
            f'/admin/receipts/create/{res_id}',
            data={
                'entity_company': '1',
                'company_name': 'Rossi S.r.l.',
                'company_vat': '01234567890',
                'company_address': 'Via Milano 10, 00100 Roma',
                'guest_full_name': 'Mario Rossi',
            },
            follow_redirects=True,
        )
        with app.app_context():
            res = Reservation.query.get(res_id)
            assert not res.guest_residence_address
            assert not res.guest_residence_city
            assert not res.guest_residence_country

    def test_company_mode_preserves_existing_guest_address(self, app, client):
        res_id = _make_reservation(app, with_address=True)
        login_admin(client)
        client.post(
            f'/admin/receipts/create/{res_id}',
            data={
                'entity_company': '1',
                'company_name': 'Rossi S.r.l.',
                'company_vat': '01234567890',
                'company_address': 'Via Milano 10, 00100 Roma',
                'guest_full_name': 'Mario Rossi',
            },
            follow_redirects=True,
        )
        with app.app_context():
            res = Reservation.query.get(res_id)
            assert res.guest_residence_address == 'Via Vecchia 1'

    @pytest.mark.parametrize(
        ('company_name', 'company_vat', 'company_address', 'expected'),
        [
            ('', '01234567890', 'Via Milano 10', 'denominazione'),
            ('Rossi S.r.l.', '123', 'Via Milano 10', '11 cifre'),
            ('Rossi S.r.l.', '01234567890', '', 'sede legale'),
        ],
    )
    def test_company_data_validation(self, app, client, company_name, company_vat, company_address, expected):
        res_id = _make_reservation(app)
        login_admin(client)
        resp = client.post(
            f'/admin/receipts/create/{res_id}',
            data={
                'entity_company': '1',
                'company_name': company_name,
                'company_vat': company_vat,
                'company_address': company_address,
                'guest_full_name': 'Mario Rossi',
            },
            follow_redirects=True,
        )
        body = resp.get_data(as_text=True)
        assert expected.lower() in body.lower()
        with app.app_context():
            assert Receipt.query.filter_by(reservation_id=res_id).first() is None

    def test_company_pdf_is_generated(self, app, client):
        res_id = _make_reservation(app)
        login_admin(client)
        client.post(
            f'/admin/receipts/create/{res_id}',
            data={
                'entity_company': '1',
                'company_name': 'Rossi S.r.l.',
                'company_vat': '01234567890',
                'company_address': 'Via Milano 10, 00100 Roma',
                'guest_full_name': 'Mario Rossi',
            },
            follow_redirects=True,
        )
        with app.app_context():
            receipt = Receipt.query.filter_by(reservation_id=res_id).first()
            from app.services.receipts import generate_receipt_pdf_bytes

            pdf = generate_receipt_pdf_bytes(receipt)
            assert pdf[:4] == b'%PDF'


class TestReceiptForm:
    def test_form_exposes_both_intestatari(self, app, client):
        _make_reservation(app)
        login_admin(client)
        body = client.get('/admin/receipts').get_data(as_text=True)
        assert 'name="guest_residence_address"' in body
        assert 'name="entity_company"' in body
        assert 'name="company_vat"' in body

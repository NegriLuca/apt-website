from datetime import date

from app import db
from app.models import Apartment, Reservation
from app.services.tourist_tax import TouristTaxService, generate_monthly_tax_report, get_tax_service


class TestTouristTaxCalculation:
    """Test tourist tax (Tassa di Soggiorno) calculation logic"""

    def test_calculate_tax_basic(self, app):
        with app.app_context():
            apt = Apartment.query.first()
            res = Reservation(
                guest_name='Test Guest',
                guest_email='test@test.com',
                check_in=date(2026, 8, 1),
                check_out=date(2026, 8, 5),
                num_guests=2,
                status='confirmed',
            )
            service = TouristTaxService(apt)
            tax = service.calculate_tax(res)
            expected = 4 * 2 * 6.00
            assert tax == expected

    def test_calculate_tax_uses_num_adults(self, app):
        with app.app_context():
            apt = Apartment.query.first()
            res = Reservation(
                guest_name='Family Guest',
                check_in=date(2026, 8, 1),
                check_out=date(2026, 8, 5),
                num_guests=4,
                num_adults=2,
                num_children=2,
                status='confirmed',
            )
            service = TouristTaxService(apt)
            tax = service.calculate_tax(res)
            assert tax == 4 * 2 * 6.00

    def test_calculate_tax_with_child_exempt(self, app):
        with app.app_context():
            apt = Apartment.query.first()
            res = Reservation(
                guest_name='Family Guest',
                guest_email='family@test.com',
                check_in=date(2026, 8, 1),
                check_out=date(2026, 8, 5),
                num_guests=4,
                status='confirmed',
            )
            service = TouristTaxService(apt)
            tax = service.calculate_tax(res, guest_ages=[35, 33, 7, 5])
            expected = 4 * 2 * 6.00
            assert tax == expected

    def test_calculate_tax_all_children_exempt(self, app):
        with app.app_context():
            apt = Apartment.query.first()
            res = Reservation(
                guest_name='Kids Only',
                check_in=date(2026, 8, 1),
                check_out=date(2026, 8, 3),
                num_guests=2,
                status='confirmed',
            )
            service = TouristTaxService(apt)
            tax = service.calculate_tax(res, guest_ages=[5, 8])
            assert tax == 0.0

    def test_calculate_tax_max_nights_capped(self, app):
        with app.app_context():
            apt = Apartment.query.first()
            res = Reservation(
                guest_name='Long Stay',
                check_in=date(2026, 8, 1),
                check_out=date(2026, 8, 20),
                num_guests=1,
                status='confirmed',
            )
            service = TouristTaxService(apt)
            tax = service.calculate_tax(res)
            expected = 10 * 1 * 6.00
            assert tax == expected

    def test_calculate_tax_pending_reservation(self, app):
        with app.app_context():
            apt = Apartment.query.first()
            res = Reservation(
                guest_name='Pending',
                check_in=date(2026, 8, 1),
                check_out=date(2026, 8, 5),
                num_guests=2,
                status='pending',
            )
            service = TouristTaxService(apt)
            tax = service.calculate_tax(res)
            assert tax == 0.0

    def test_calculate_tax_cancelled_reservation(self, app):
        with app.app_context():
            apt = Apartment.query.first()
            res = Reservation(
                guest_name='Cancelled',
                check_in=date(2026, 8, 1),
                check_out=date(2026, 8, 5),
                num_guests=2,
                status='cancelled',
            )
            service = TouristTaxService(apt)
            tax = service.calculate_tax(res)
            assert tax == 0.0

    def test_tax_row_generation(self, app):
        with app.app_context():
            apt = Apartment.query.first()
            res = Reservation(
                guest_name='Row Test',
                guest_email='row@test.com',
                check_in=date(2026, 8, 1),
                check_out=date(2026, 8, 4),
                num_guests=2,
                status='confirmed',
            )
            db.session.add(res)
            db.session.commit()

            service = TouristTaxService(apt)
            row = service.calculate_for_reservation(res)
            assert row.guest_name == 'Row Test'
            assert row.nights == 3
            assert row.total_tax == 3 * 2 * 6.00

    def test_get_tax_service_factory(self, app):
        with app.app_context():
            apt = Apartment.query.first()
            service = get_tax_service(apt)
            assert isinstance(service, TouristTaxService)
            assert service.rate == 6.00

    def test_export_monthly_csv_has_headers(self, app):
        with app.app_context():
            apt = Apartment.query.first()
            service = TouristTaxService(apt)
            csv_data = service.export_monthly_csv(2026, 8)
            assert 'ID Prenotazione' in csv_data
            assert 'Totale tassa' in csv_data

    def test_export_monthly_csv_with_reservation(self, app):
        with app.app_context():
            apt = Apartment.query.first()
            res = Reservation(
                guest_name='CSV Export',
                guest_email='csv@test.com',
                check_in=date(2026, 8, 5),
                check_out=date(2026, 8, 8),
                num_guests=2,
                status='confirmed',
            )
            db.session.add(res)
            db.session.commit()

            service = TouristTaxService(apt)
            csv_data = service.export_monthly_csv(2026, 8)
            assert 'CSV Export' in csv_data
            assert res.guest_name in csv_data

    def test_excluded_reservation_omitted_from_report(self, app):
        with app.app_context():
            apt = Apartment.query.first()
            res = Reservation(
                guest_name='Excluded Guest',
                guest_email='excluded@test.com',
                check_in=date(2026, 8, 10),
                check_out=date(2026, 8, 12),
                num_guests=2,
                status='confirmed',
                tourist_tax_excluded=True,
            )
            db.session.add(res)
            db.session.commit()

            service = TouristTaxService(apt)
            csv_data = service.export_monthly_csv(2026, 8)
            assert 'Excluded Guest' not in csv_data

    def test_generate_detailed_report_structure(self, app):
        with app.app_context():
            apt = Apartment.query.first()
            res = Reservation(
                guest_name='Report Test',
                guest_email='report@test.com',
                check_in=date(2026, 9, 1),
                check_out=date(2026, 9, 5),
                num_guests=1,
                status='confirmed',
            )
            db.session.add(res)
            db.session.commit()

            service = TouristTaxService(apt)
            report = service.generate_detailed_report(2026, 9)
            assert report['period'] == '09/2026'
            assert report['total_tax'] == 4 * 1 * 6.00
            assert len(report['reservations']) == 1

    def test_generate_monthly_tax_report_function(self, app):
        with app.app_context():
            res = Reservation(
                guest_name='Monthly Test',
                guest_email='monthly@test.com',
                check_in=date(2026, 7, 1),
                check_out=date(2026, 7, 5),
                num_guests=1,
                status='confirmed',
            )
            db.session.add(res)
            db.session.commit()

            report = generate_monthly_tax_report(year=2026, month=7)
            assert report['total_tax'] == 4 * 1 * 6.00
            assert len(report['reservations']) >= 1

    def test_custom_rate_from_apartment(self, app):
        with app.app_context():
            apt = Apartment.query.first()
            apt.tourist_tax_rate = 7.50
            db.session.commit()

            service = TouristTaxService(apt)
            assert service.rate == 7.50
            res = Reservation(
                guest_name='Custom Rate',
                check_in=date(2026, 10, 1),
                check_out=date(2026, 10, 4),
                num_guests=2,
                status='confirmed',
            )
            tax = service.calculate_tax(res)
            assert tax == 3 * 2 * 7.50

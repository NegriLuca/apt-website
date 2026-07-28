from datetime import date, timedelta

from app import db
from app.models import QuesturaLog, Reservation


class TestQuesturaModel:
    """Test Questura-related model logic"""

    def test_questura_ready_all_fields(self, app):
        with app.app_context():
            res = Reservation(
                guest_name='Mario Rossi',
                guest_email='mario@test.com',
                check_in=date.today() + timedelta(days=10),
                check_out=date.today() + timedelta(days=13),
                num_guests=2,
                guest_surname='Rossi',
                guest_first_name='Mario',
                guest_birth_date=date(1990, 1, 1),
                guest_birth_place='Roma',
                guest_nationality='ITA',
                guest_document_type='passport',
                guest_document_number='AB123456',
                guest_document_expiry=date(2030, 1, 1),
                guest_document_country='ITA',
                guest_gender='M',
            )
            assert res.questura_ready() is True

    def test_questura_ready_missing_fields(self, app):
        with app.app_context():
            res = Reservation(
                guest_name='Mario Rossi',
                guest_email='mario@test.com',
                check_in=date.today() + timedelta(days=10),
                check_out=date.today() + timedelta(days=13),
                num_guests=2,
            )
            assert res.questura_ready() is False

    def test_questura_ready_partial_fields(self, app):
        with app.app_context():
            res = Reservation(
                guest_name='Mario Rossi',
                guest_email='mario@test.com',
                check_in=date.today() + timedelta(days=10),
                check_out=date.today() + timedelta(days=13),
                num_guests=2,
                guest_surname='Rossi',
                guest_first_name='Mario',
                guest_nationality='ITA',
            )
            assert res.questura_ready() is False

    def test_guest_full_name_property(self, app):
        with app.app_context():
            res = Reservation(
                guest_name='Mario Rossi',
                guest_email='mario@test.com',
                check_in=date.today(),
                check_out=date.today() + timedelta(days=1),
                num_guests=1,
                guest_surname='Rossi',
                guest_first_name='Mario',
            )
            assert res.guest_full_name == 'Rossi Mario'

    def test_guest_full_name_fallback(self, app):
        with app.app_context():
            res = Reservation(
                guest_name='Mario Rossi',
                guest_email='mario@test.com',
                check_in=date.today(),
                check_out=date.today() + timedelta(days=1),
                num_guests=1,
            )
            assert res.guest_full_name == 'Mario Rossi'

    def test_questura_log_creation(self, app):
        with app.app_context():
            res = Reservation(
                guest_name='Test Guest',
                guest_email='test@test.com',
                check_in=date.today(),
                check_out=date.today() + timedelta(days=2),
                num_guests=1,
                status='confirmed',
            )
            db.session.add(res)
            db.session.commit()

            log = QuesturaLog(
                reservation_id=res.id,
                action='submit',
                status='success',
                request_xml='<test/>',
                response_xml='<response/>',
            )
            db.session.add(log)
            db.session.commit()

            assert log.id is not None
            assert log.reservation_id == res.id
            assert log.action == 'submit'
            assert log.status == 'success'

    def test_questura_log_relationship(self, app):
        with app.app_context():
            res = Reservation(
                guest_name='Test Guest',
                guest_email='test@test.com',
                check_in=date.today(),
                check_out=date.today() + timedelta(days=2),
                num_guests=1,
                status='confirmed',
            )
            db.session.add(res)
            db.session.commit()

            for action in ['submit', 'retry', 'manual']:
                log = QuesturaLog(reservation_id=res.id, action=action, status='success')
                db.session.add(log)
            db.session.commit()

            assert res.questura_logs.count() == 3

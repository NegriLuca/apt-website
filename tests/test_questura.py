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

    def test_submit_reservation_builds_main_and_companions(self, app):
        from unittest.mock import patch

        from app.services.questura import get_questura_service

        with app.app_context():
            res = Reservation(
                guest_name='Main Guest',
                guest_email='main@test.com',
                check_in=date.today(),
                check_out=date.today() + timedelta(days=1),
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
                companions=[
                    {
                        'surname': 'Bianchi',
                        'first_name': 'Anna',
                        'birth_date': '1992-05-05',
                        'birth_place': 'Milano',
                        'nationality': 'ITA',
                        'document_type': 'id_card',
                        'document_number': 'CD654321',
                        'document_expiry': '2031-05-05',
                        'document_country': 'ITA',
                        'gender': 'F',
                    }
                ],
            )
            db.session.add(res)
            db.session.commit()
            rid = res.id

            svc = get_questura_service(test_mode=True)

            with patch.object(svc, 'is_configured', return_value=True):
                result = svc.submit_reservation(res)

            assert result['success'] is True
            # 2 guests submitted: main + companion
            with app.app_context():
                log = QuesturaLog.query.filter_by(reservation_id=rid, action='submit').first()
                assert log is not None
                xml = log.request_xml
                assert xml.count('Rossi') >= 1
                assert xml.count('Bianchi') >= 1
                # Tabella 1 records are fixed 168 chars, CRLF-separated.
                records = xml.split('\r\n')
                assert all(len(r) == 168 for r in records)

    def test_submit_reservation_requires_guest_data(self, app):
        from app.services.questura import get_questura_service

        with app.app_context():
            res = Reservation(
                guest_name='No Data Guest',
                guest_email='nodata@test.com',
                check_in=date.today(),
                check_out=date.today() + timedelta(days=1),
                num_guests=1,
            )
            db.session.add(res)
            db.session.commit()

            svc = get_questura_service(test_mode=True)
            result = svc.submit_reservation(res)

            assert result['success'] is False
            assert result.get('requires_guest_data') is True

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

    def test_build_record_single_italian_guest(self, app):
        from datetime import date as d

        from app.services.questura import QuesturaGuest, get_questura_service

        with app.app_context():
            svc = get_questura_service(test_mode=True)
            guest = QuesturaGuest(
                surname='Rossi', first_name='Mario', birth_date=d(1990, 1, 1),
                birth_place='Roma', birth_country='ITA', nationality='ITA',
                document_type='id_card', document_number='AB123456',
                document_expiry=d(2030, 1, 1), document_country='ITA',
                gender='M', check_in=d(2026, 8, 13), check_out=d(2026, 8, 15),
                reservation_id=1,
            )
            schedine = svc.build_schedine([guest])
            record = schedine[0]
            assert len(record) == 168
            # tipo alloggiato (0:2), data arrivo (2:12), giorni (12:14)
            assert record[0:2] == '16'
            assert record[2:12] == '13/08/2026'
            assert record[12:14] == ' 2'
            # cognome/nome padded
            assert record[14:64].strip() == 'Rossi'
            assert record[64:94].strip() == 'Mario'
            # sesso M=1, data nascita, Italia 100000100 in stato nascita e cittadinanza
            assert record[94] == '1'
            assert record[95:105] == '01/01/1990'
            assert record[116:125] == '100000100'
            assert record[125:134] == '100000100'
            # documento: IDENT + numero + luogo (comune code not resolved in test mode)
            assert record[134:139].strip() == 'IDENT'
            assert record[139:159].strip() == 'AB123456'

    def test_build_record_family_group(self, app):
        from datetime import date as d

        from app.services.questura import QuesturaGuest, get_questura_service

        with app.app_context():
            svc = get_questura_service(test_mode=True)
            main = QuesturaGuest(
                surname='Rossi', first_name='Mario', birth_date=d(1990, 1, 1),
                birth_place='Roma', birth_country='ITA', nationality='ITA',
                document_type='passport', document_number='AB123456',
                document_expiry=d(2030, 1, 1), document_country='ITA',
                gender='M', check_in=d(2026, 8, 13), check_out=d(2026, 8, 15),
                reservation_id=1,
            )
            child = QuesturaGuest(
                surname='Rossi', first_name='Anna', birth_date=d(2018, 5, 5),
                birth_place='Roma', birth_country='ITA', nationality='ITA',
                document_type='', document_number='',
                document_expiry=d(2030, 1, 1), document_country='ITA',
                gender='F', check_in=d(2026, 8, 13), check_out=d(2026, 8, 15),
                reservation_id=1,
            )
            records = svc.build_schedine([main, child])
            assert len(records) == 2
            assert all(len(r) == 168 for r in records)
            # capo famiglia (17) then familiare (19)
            assert records[0][0:2] == '17'
            assert records[1][0:2] == '19'
            # familiare document fields are blank
            assert records[1][134:168] == ' ' * 34
            # gender F=2
            assert records[1][94] == '2'

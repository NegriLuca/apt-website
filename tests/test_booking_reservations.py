"""Booking arrivals report (.xls/.csv) parser — net = Prezzo − commissione."""
import io
from datetime import date

from app import db
from app.models import Earning, Reservation
from app.services.booking_reservations import parse_reservations_report
from app.services.finance import compute_finance
from tests.conftest import login_admin


def _read_downloads(name: str) -> bytes:
    with open(f'/home/lucanegri/Downloads/{name}', 'rb') as f:
        return f.read()

SAMPLE_CSV = (
    'N° di prenotazione,Prenotato da,Nome ospite(i),Arrivo,Partenza,Data di prenotazione,Stato,'
    'Camere/unità,Persone,Adulti,Bambini,Età dei bambini,Prezzo,% commissione,Importo commissione,'
    'Stato del pagamento,Dispositivo,Durata (notti),Data di cancellazione\n'
    '5619010403,"Tiranti, Francesco",Francesco Tiranti,2026-10-06,2026-10-08,2026-09-25 12:19:41,ok,'
    '1,2,2,0,,208 EUR,18.0,37.44 EUR,Pagamento tramite Booking.com,Computer,2,\n'
    '5519483624,"Banasakis, Georgios",Georgios Banasakis,2026-10-08,2026-10-12,2026-08-06 16:52:40,'
    'cancelled_by_guest,1,4,4,0,,518.4 EUR,18.0,,Mobile,4,2026-09-16 14:44:40\n'
    '5791136725,"Sponsale, Sara",Sara Sponsale,2026-10-23,2026-10-25,2026-10-06 22:23:47,ok,'
    '1,4,4,0,,352 EUR,18.0,63.36 EUR,Pagamento tramite Booking.com,Mobile,2,\n'
)


def test_parse_arrivals_csv_ok_and_cancelled():
    res = parse_reservations_report(SAMPLE_CSV, 'arrivi.csv')
    assert res['totals']['count'] == 2
    assert res['totals']['cancelled'] == 1
    assert res['totals']['gross'] == 560.0
    assert res['totals']['commission'] == 100.80
    assert res['totals']['transaction'] == 8.40  # 1.5% of gross
    assert res['totals']['vat'] == 24.02  # 22% of (commission + transaction)
    assert res['totals']['withholding'] == -117.60  # cedolare 21% est
    assert res['totals']['payout'] == 426.78  # bank before withholding
    assert res['totals']['net'] == 309.18  # == Finance Netto
    assert res['totals']['nights'] == 4
    # cedolare estimated 21% of gross, city tax 6€/adult/night
    assert res['totals']['cedolare_est'] == round(560.0 * 0.21, 2)
    assert res['totals']['city_tax_est'] == (2 * 2 * 6) + (4 * 2 * 6)
    by_code = {r['code']: r for r in res['reservations']}
    assert by_code['5619010403']['payout_est'] == 158.52
    assert by_code['5619010403']['net'] == 114.84
    assert by_code['5791136725']['payout_est'] == 268.26
    assert by_code['5791136725']['net'] == 194.34
    assert [c['code'] for c in res['cancelled']] == ['5519483624']


def test_parse_arrivals_xls_october_file():
    try:
        raw = _read_downloads('Arrivo_ 2026-10-01 - 2026-10-31.xls')
    except OSError:
        import pytest

        pytest.skip('October .xls fixture not available')
    res = parse_reservations_report(raw, 'Arrivo.xls')
    assert res['totals']['count'] == 5
    assert res['totals']['cancelled'] == 3
    assert res['totals']['gross'] == 1308.0
    assert res['totals']['commission'] == 235.44
    assert res['totals']['transaction'] == 19.62
    assert res['totals']['vat'] == 56.10
    assert res['totals']['payout'] == 996.84
    assert res['totals']['withholding'] == -274.68
    assert res['totals']['net'] == 722.16
    assert res['totals']['nights'] == 11


def test_finance_csv_reconciles_and_matches_arrivals_estimate():
    """G0Vg64ch7JuIisvn.csv (Bosotti): Netto == sum of parts, and arrivals
    estimates match the Finance row to the cent."""
    from app.services.booking_earnings import parse_earnings_csv

    try:
        raw = _read_downloads('G0Vg64ch7JuIisvn.csv')
    except OSError:
        import pytest

        pytest.skip('Finance CSV fixture not available')
    fin = parse_earnings_csv(raw)
    assert fin['errors'] == []
    assert fin['totals']['count'] == 1
    row = fin['per_code'][0]
    assert row['gross'] == 135.85
    assert row['commission'] == -24.45
    assert row['withholding'] == -28.53
    assert row['vat'] == -5.83
    assert row['transaction'] == -2.04
    assert row['net'] == 75.00

    arr = parse_reservations_report(_read_downloads('Arrivo_ 2026-09-01 - 2026-09-30.xls'), 'Arrivo.xls')
    est = {r['code']: r for r in arr['reservations']}['6738995981']
    assert est['gross'] == row['gross']
    assert est['commission'] == abs(row['commission'])
    assert est['vat_est'] == abs(row['vat'])
    assert est['transaction_est'] == abs(row['transaction'])
    assert est['cedolare_est'] == abs(row['withholding'])
    assert est['net'] == row['net']


def test_arrivals_upload_updates_reservations_and_finance(client, app):
    """September .xls: ok booking syncs Reservation+Earning, cancelled booking
    is removed from Finance (Earning deleted) and its Reservation cancelled."""
    with app.app_context():
        r_ok = Reservation(
            guest_name='Booking Guest (abc123)', check_in=date(2026, 9, 23), check_out=date(2026, 9, 24),
            num_guests=1, status='confirmed', source='booking_com', external_uid='ical-uid-bosotti',
            is_block=False, total_price=0.0, payment_status='n/a', payment_method='automatic',
        )
        r_canc = Reservation(
            guest_name='Booking Guest (def456)', check_in=date(2026, 9, 8), check_out=date(2026, 9, 12),
            num_guests=1, status='confirmed', source='booking_com', external_uid='ical-uid-bonifaci',
            is_block=False, total_price=0.0, payment_status='n/a', payment_method='automatic',
        )
        e_stale = Earning(
            platform='booking', confirmation_code='5060277056', guest_name='bonifaci, nazario',
            start_date=date(2026, 9, 8), end_date=date(2026, 9, 12), payout_date=date(2026, 9, 8),
            nights=4, currency='EUR', amount=393.6, service_fee=86.4, gross_earnings=480.0,
            withholding=0, net=393.6,
            raw_json={'report': 'reservations', 'cedolare_est': 100.80, 'city_tax_est': 48.0},
        )
        db.session.add_all([r_ok, r_canc, e_stale])
        db.session.commit()
        ok_id, canc_id = r_ok.id, r_canc.id

    login_admin(client)
    raw = _read_downloads('Arrivo_ 2026-09-01 - 2026-09-30.xls')
    resp = client.post(
        '/admin/earnings',
        data={'csv_file': (io.BytesIO(raw), 'Arrivo_sett.xls')},
        content_type='multipart/form-data',
        follow_redirects=True,
    )
    assert resp.status_code == 200

    with app.app_context():
        # ok booking: full estimates stored (== Finance Netto), linked + reservation updated
        e = Earning.query.filter_by(platform='booking', confirmation_code='6738995981').first()
        assert e is not None
        assert e.gross_earnings == 135.85
        assert e.service_fee == 32.32  # 24.45 comm + 5.83 VAT + 2.04 trans
        assert e.amount == 103.53  # bank before withholding
        assert e.withholding == -28.53
        assert e.net == 75.00  # == Finance Netto
        assert e.nights == 1
        r = Reservation.query.get(ok_id)
        assert e.reservation_id == ok_id
        assert r.guest_name == 'Filippo Bosotti'
        assert r.total_price == 75.00
        assert r.amount_paid == 75.00
        assert r.payment_status == 'paid'
        assert r.num_adults == 1
        assert r.num_guests == 1
        assert r.status == 'confirmed'
        # cancelled booking: Earning deleted (out of Finance), reservation cancelled
        assert Earning.query.filter_by(platform='booking', confirmation_code='5060277056').first() is None
        rc = Reservation.query.get(canc_id)
        assert rc.status == 'cancelled'
        # Finance September OTA counts only the ok booking, fees reconciled
        fin = compute_finance(2026, 9)
        assert fin['ota']['gross'] == 135.85
        assert fin['ota']['amount'] == 103.53
        assert fin['ota']['fees'] == 32.32
        assert fin['ota']['withholding'] == -28.53
        assert fin['payout'] == 103.53
        assert fin['cedolare'] == 28.53
        assert fin['after_tax'] == 75.00
        assert fin['ota']['count'] == 1


def test_cancelled_keeps_finance_payout_penalty(client, app):
    """Cancelled without free cancellation (penalty collected): the Finance
    Earning is real payout evidence and must be kept; reservation is cancelled."""
    with app.app_context():
        r = Reservation(
            guest_name='Georgios Banasakis', check_in=date(2026, 10, 8), check_out=date(2026, 10, 12),
            num_guests=4, num_adults=4, status='confirmed', source='booking_com',
            external_uid='5519483624', is_block=False, total_price=425.09,
            payment_status='paid', payment_method='automatic',
        )
        db.session.add(r)
        db.session.commit()
        e_fin = Earning(
            platform='booking', confirmation_code='5519483624', guest_name='Georgios Banasakis',
            start_date=date(2026, 10, 8), end_date=date(2026, 10, 12), payout_date=date(2026, 10, 12),
            nights=4, currency='EUR', amount=518.4, service_fee=93.31, gross_earnings=518.4,
            withholding=-108.86, net=100.0, reservation_id=r.id,
            raw_json={'report': 'earnings', 'payment_id': 'PAY-123', 'withholding': -108.86, 'net': 100.0},
        )
        db.session.add(e_fin)
        db.session.commit()
        res_id = r.id

    login_admin(client)
    resp = client.post(
        '/admin/earnings',
        data={'csv_text': SAMPLE_CSV},
        follow_redirects=True,
    )
    assert resp.status_code == 200

    with app.app_context():
        # real Finance payout kept untouched, reservation cancelled (stay didn't happen)
        kept = Earning.query.filter_by(platform='booking', confirmation_code='5519483624').first()
        assert kept is not None
        assert kept.net == 100.0
        assert kept.withholding == -108.86
        rc = Reservation.query.get(res_id)
        assert rc.status == 'cancelled'
        # Finance October still counts the penalty payout
        fin = compute_finance(2026, 10)
        assert fin['ota']['count'] >= 1
        assert any(
            e.confirmation_code == '5519483624'
            for e in Earning.query.filter_by(platform='booking').all()
            if e.start_date and e.start_date.year == 2026 and e.start_date.month == 10
        )

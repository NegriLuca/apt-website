"""
Booking.com Reservations report parser ("Arrivi" .xls / .csv) — Lotto 235.

This is NOT the Finance → Earnings export (Tipologia,Numero prenotazione,
Importo,Commissione,Ritenuta,Netto... handled by booking_earnings.py).
It is the extranet Reservations report:

N° di prenotazione,Prenotato da,Nome ospite(i),Arrivo,Partenza,
Data di prenotazione,Stato,Camere/unità,Persone,Adulti,Bambini,
Età dei bambini,Prezzo,% commissione,Importo commissione,
Stato del pagamento,Metodo di pagamento,...,Durata (notti),
Data di cancellazione,...

Money semantics (verified on Ott 2026 file):
- Prezzo = gross paid by guest (e.g. "208 EUR"), city tax NOT included.
- Importo commissione = Booking commission (e.g. "37.44 EUR", 18%).
- Ritenuta cedolare 21%, VAT 22%, transaction fee are NOT in this file —
  estimated from the Sep 2026 Finance export (1 row: Bosotti 6738995981,
  Importo 135.85, Commissione -24.45, Ritenuta -28.53, VAT -5.83,
  Transazione -2.04, Netto 75.00 — reconciles to the cent):
  - Commissione = % commissione x Prezzo (file value, rounded to cents)
  - Ritenuta = 21% x Prezzo (cedolare secca, withheld by Booking)
  - Transazione = 1.5% x Prezzo  (single-sample fit — confirm on more rows)
  - VAT = 22% x (Commissione + Transazione)  (single-sample fit)
  - Payout (bank before withholding) = Prezzo - Commissione - VAT - Transazione
  - Netto full (== Finance Netto, bank) = Payout - Ritenuta
  Authoritative values only in the Finance Earnings export, which overwrites
  these estimates on upload (upsert on platform+code).
- Tassa di soggiorno Roma (6 EUR/adult/night, max 10 nights) is NOT included
  in Prezzo — estimated from Adulti x notti for the compliance report.
Cancelled rows (Stato != 'ok') are returned separately and excluded from totals.
"""
import csv
import io
from collections import defaultdict
from datetime import date, datetime
from typing import Any

CEDOLARE_RATE = 0.21
VAT_RATE = 0.22
TRANSACTION_RATE = 0.015
CITY_TAX_RATE = 6.00
CITY_TAX_MAX_NIGHTS = 10

_OK_STATES = {'ok'}


def _fnum(s: Any) -> float:
    if s is None:
        return 0.0
    s = str(s).strip().replace('\xa0', '').replace('€', '').replace('EUR', '').strip()
    if not s or s == '-':
        return 0.0
    if ',' in s and '.' in s:
        if s.rfind(',') > s.rfind('.'):
            s = s.replace('.', '').replace(',', '.')
        else:
            s = s.replace(',', '')
    elif ',' in s and '.' not in s:
        s = s.replace(',', '.')
    try:
        return float(s)
    except ValueError:
        return 0.0


def _inum(s: Any) -> int:
    try:
        return int(float(str(s).strip() or 0))
    except (ValueError, TypeError):
        return 0


def _parse_date(s: Any) -> date | None:
    if s is None:
        return None
    s = str(s).strip()
    if not s:
        return None
    # "2026-10-06" or "2026-09-25 12:19:41"
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d', '%d/%m/%Y', '%d/%m/%y', '%d-%m-%Y', '%Y/%m/%d'):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _rows_from_xls(file_bytes: bytes) -> list[dict]:
    """Read first sheet of a BIFF .xls via xlrd (optional dep)."""
    try:
        import xlrd
    except ImportError as exc:
        raise ValueError('Foglio .xls: installa xlrd (pip install xlrd) oppure riesporta in .csv') from exc
    wb = xlrd.open_workbook(file_contents=file_bytes)
    sh = wb.sheet_by_index(0)
    if sh.nrows < 2:
        return []
    headers = [str(sh.cell_value(0, c)).strip() for c in range(sh.ncols)]
    rows = []
    for r in range(1, sh.nrows):
        row = {}
        for c, h in enumerate(headers):
            v = sh.cell_value(r, c)
            # xlrd returns floats for numbers ("5619010403.0") — normalise
            if isinstance(v, float) and v.is_integer() and 'prenotazione' in h.lower():
                v = str(int(v))
            row[h] = v
        rows.append(row)
    return rows


def _rows_from_csv(text: str) -> list[dict]:
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames:
        reader.fieldnames = [h.strip() for h in reader.fieldnames]
    return [{k.strip(): v for k, v in row.items()} for row in reader]


def parse_reservations_report(file_bytes: bytes | str, filename: str = '') -> dict:
    """Parse Booking arrivals .xls/.csv bytes and return per-booking net + totals."""
    if isinstance(file_bytes, bytes):
        if filename.lower().endswith(('.xls', '.xlsx')) or file_bytes[:4] == b'\xd0\xcf\x11\xe0':
            rows = _rows_from_xls(file_bytes)
        else:
            for enc in ('utf-8-sig', 'utf-8', 'cp1252', 'iso-8859-1'):
                try:
                    text = file_bytes.decode(enc)
                    break
                except UnicodeDecodeError:
                    continue
            else:
                text = file_bytes.decode('utf-8', errors='ignore')
            rows = _rows_from_csv(text.lstrip('\ufeff'))
    else:
        rows = _rows_from_csv(file_bytes.lstrip('\ufeff'))

    reservations: list[dict] = []
    cancelled: list[dict] = []
    per_code: dict[str, dict] = {}
    errors: list[str] = []

    for idx, r in enumerate(rows, start=2):
        code = str(r.get('N° di prenotazione') or r.get('Numero prenotazione') or '').strip()
        # xlrd float artefact: "5619010403.0"
        if code.endswith('.0'):
            code = code[:-2]
        if not code:
            continue
        stato = str(r.get('Stato') or '').strip().lower()
        guest = str(r.get('Nome ospite(i)') or r.get('Prenotato da') or '').strip()
        start = _parse_date(r.get('Arrivo'))
        end = _parse_date(r.get('Partenza'))
        booking_date = _parse_date(r.get('Data di prenotazione'))
        cancelled_at = _parse_date(r.get('Data di cancellazione'))
        adults = _inum(r.get('Adulti') or 0)
        children = _inum(r.get('Bambini') or 0)
        guests = _inum(r.get('Persone') or 0) or (adults + children) or 1
        nights = _inum(r.get('Durata (notti)') or 0)
        if not nights and start and end:
            nights = (end - start).days
        gross = _fnum(r.get('Prezzo'))
        # Booking sometimes writes 3 decimals (135.85 x 18% = 24.453) — bank
        # payout rounds to cents, so round here for Finance consistency.
        commission = round(_fnum(r.get('Importo commissione')), 2)
        if not commission:
            pct = _fnum(r.get('% commissione'))
            commission = round(gross * pct / 100.0, 2)
        transaction_est = round(gross * TRANSACTION_RATE, 2)
        vat_est = round((commission + transaction_est) * VAT_RATE, 2)
        payout_est = round(gross - commission - vat_est - transaction_est, 2)
        cedolare_est = round(gross * CEDOLARE_RATE, 2)
        net_full_est = round(payout_est - cedolare_est, 2)  # == Finance Netto
        city_tax_est = round(min(nights, CITY_TAX_MAX_NIGHTS) * adults * CITY_TAX_RATE, 2)

        entry = {
            'row': idx,
            'code': code,
            'guest': guest,
            'booked_by': str(r.get('Prenotato da') or '').strip(),
            'start': start,
            'end': end,
            'booking_date': booking_date,
            'status': stato,
            'is_cancelled': stato not in _OK_STATES,
            'cancelled_at': cancelled_at,
            'adults': adults,
            'children': children,
            'guests': guests,
            'nights': nights,
            'currency': 'EUR',
            'gross': gross,
            'commission': commission,
            'commission_pct': round(commission / gross * 100, 2) if gross else 0.0,
            'vat_est': vat_est,
            'transaction_est': transaction_est,
            'payout_est': payout_est,  # bank before withholding
            'cedolare_est': cedolare_est,  # 21% of gross, withheld by Booking
            'net': net_full_est,  # == Finance Netto (bank) — comparable across reports
            'after_tax_est': net_full_est,
            'city_tax_est': city_tax_est,  # NOT included in Prezzo
            'payment_status': str(r.get('Stato del pagamento') or '').strip(),
            'booker_country': str(r.get('Booker country') or '').strip(),
            'comments': str(r.get('Commenti') or '').strip(),
            # aliases so the earnings template (Finance CSV shape) renders as-is
            'service': commission,
            'cleaning': 0.0,
            'vat': vat_est,
            'transaction': transaction_est,
            'withholding': -cedolare_est,
            'amount': payout_est,
        }
        if entry['is_cancelled']:
            cancelled.append(entry)
        else:
            reservations.append(entry)
        per_code[code] = entry

    total_gross = round(sum(r['gross'] for r in reservations), 2)
    total_commission = round(sum(r['commission'] for r in reservations), 2)
    total_vat = round(sum(r['vat_est'] for r in reservations), 2)
    total_transaction = round(sum(r['transaction_est'] for r in reservations), 2)
    total_payout = round(sum(r['payout_est'] for r in reservations), 2)
    total_net = round(sum(r['net'] for r in reservations), 2)
    total_cedolare = round(sum(r['cedolare_est'] for r in reservations), 2)
    total_city_tax = round(sum(r['city_tax_est'] for r in reservations), 2)
    total_nights = sum(r['nights'] for r in reservations)

    monthly_map: dict[str, dict] = defaultdict(
        lambda: {'gross': 0.0, 'commission': 0.0, 'vat': 0.0, 'transaction': 0.0, 'payout': 0.0, 'net': 0.0, 'withholding': 0.0, 'cedolare_est': 0.0, 'city_tax_est': 0.0, 'nights': 0, 'count': 0}
    )
    for r in reservations:
        if not r['start']:
            continue
        key = r['start'].strftime('%Y-%m')
        m = monthly_map[key]
        m['gross'] += r['gross']
        m['commission'] += r['commission']
        m['vat'] += r['vat_est']
        m['transaction'] += r['transaction_est']
        m['payout'] += r['payout_est']
        m['net'] += r['net']
        m['withholding'] += r['withholding']
        m['cedolare_est'] += r['cedolare_est']
        m['city_tax_est'] += r['city_tax_est']
        m['nights'] += r['nights']
        m['count'] += 1
    monthly = []
    for k in sorted(monthly_map):
        v = monthly_map[k]
        v['month'] = k
        try:
            v['label'] = datetime.strptime(k, '%Y-%m').strftime('%b %Y')
        except ValueError:
            v['label'] = k
        v['avg_gross_night'] = round(v['gross'] / v['nights'], 2) if v['nights'] else 0
        v['avg_net_night'] = round(v['net'] / v['nights'], 2) if v['nights'] else 0
        monthly.append(v)

    per_code_list = sorted(per_code.values(), key=lambda x: (x.get('start') or date.min))

    totals = {
        'count': len(reservations),
        'cancelled': len(cancelled),
        'reservations': len(reservations),
        'nights': total_nights,
        'gross': total_gross,
        'commission': total_commission,
        'service': total_commission,
        'cleaning': 0.0,
        'vat': total_vat,
        'transaction': total_transaction,
        'payout': total_payout,  # bank before withholding
        'withholding': -total_cedolare,  # estimated cedolare 21%
        'net': total_net,  # == Finance Netto (bank)
        'cedolare_est': total_cedolare,
        'after_tax_est': total_net,
        'city_tax_est': total_city_tax,  # to collect separately, not in Prezzo
        'avg_gross_night': round(total_gross / total_nights, 2) if total_nights else 0,
        'avg_net_night': round(total_net / total_nights, 2) if total_nights else 0,
        'withholding_rate': round(total_cedolare / total_gross * 100, 2) if total_gross else 0.0,
    }

    return {
        'reservations': reservations,
        'cancelled': cancelled,
        'per_code': per_code_list,
        'totals': totals,
        'monthly': monthly,
        'errors': errors,
        'raw_rows': len(rows),
    }

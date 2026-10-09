"""
Booking.com Earnings CSV parser — Lotto 235

Handles the Booking.com "Earnings" export (Italian locale):
Tipologia,"Numero prenotazione",Check-in,Checkout,"Nome dell'ospite","Fornitore di servizi di pagamento","Stato della prenotazione",Valuta,"Stato del pagamento",Importo,Commissione,"Ritenuta sugli affitti a breve termine","VAT for online platform services","Costo di transazione",Netto,"Data del pagamento","ID pagamento"

Columns are stable but we are defensive: missing cols, empty strings, different
date formats (DD MMM YYYY Italian), EUR amounts with comma/dot.
"""
import csv
import io
from collections import defaultdict
from datetime import date, datetime
from typing import Any

ITALIAN_MONTHS = {
    'gen': 1, 'feb': 2, 'mar': 3, 'apr': 4, 'mag': 5, 'giu': 6,
    'lug': 7, 'ago': 8, 'set': 9, 'ott': 10, 'nov': 11, 'dic': 12,
}


def _fnum(s: Any) -> float:
    if s is None:
        return 0.0
    s = str(s).strip().replace('\u00a0', '').replace('€', '').strip()
    if not s or s == '-':
        return 0.0
    # handle both 1.234,56 and 1,234.56 -> normalize
    # Booking export usually uses dot for thousands, comma for decimal
    # e.g. "1.234,56" -> 1234.56, or "135.85" -> 135.85
    if ',' in s and '.' in s:
        # assume comma is decimal if dot is thousands
        s = s.replace('.', '').replace(',', '.') if s.rfind(',') > s.rfind('.') else s.replace(',', '')
    elif ',' in s and '.' not in s:
        # comma as decimal
        s = s.replace(',', '.')
    try:
        return float(s)
    except ValueError:
        return 0.0


def _parse_date(s: str) -> date | None:
    if not s:
        return None
    s = str(s).strip()
    # Try Italian format first: "23 set 2026" or "1 ott 2026"
    parts = s.split()
    if len(parts) == 3:
        try:
            day = int(parts[0])
            month_str = parts[1][:3].lower()
            year = int(parts[2])
            month = ITALIAN_MONTHS.get(month_str)
            if month:
                return date(year, month, day)
        except Exception:
            pass
    # Fallback to standard formats
    for fmt in ('%d/%m/%Y', '%d/%m/%y', '%Y-%m-%d', '%Y/%m/%d', '%m/%d/%Y', '%m/%d/%y'):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def parse_earnings_csv(file_bytes: bytes | str) -> dict:
    """
    Parse raw CSV bytes/str and return a dict with:
      reservations: list[dict]
      per_code: dict[code -> {reservation, gross, commission, withholding, vat, transaction, net, ...}]
      totals: {count, nights, gross, commission, withholding, vat, transaction, net, avg_gross_night, avg_net_night}
      monthly: list[{month, gross, commission, withholding, vat, transaction, net, nights, count, avg_night}]
      errors: list[str]
    """
    if isinstance(file_bytes, bytes):
        for enc in ('utf-8-sig', 'utf-8', 'cp1252', 'iso-8859-1'):
            try:
                text = file_bytes.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        else:
            text = file_bytes.decode('utf-8', errors='ignore')
    else:
        text = file_bytes

    if text.startswith('\ufeff'):
        text = text.lstrip('\ufeff')

    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames:
        reader.fieldnames = [h.strip() for h in reader.fieldnames]

    reservations = []
    per_code: dict[str, dict] = {}
    errors = []

    for idx, row in enumerate(reader, start=2):
        r = {k.strip(): (v.strip() if isinstance(v, str) else v) for k, v in row.items()}

        typ = (r.get('Tipologia') or r.get('Tipo') or '').strip()
        code = (r.get('Numero prenotazione') or r.get('Booking number') or '').strip()
        if not code:
            continue

        # parse numeric fields
        nights = 0
        checkin = _parse_date(r.get('Check-in') or r.get('Checkin') or '')
        checkout = _parse_date(r.get('Checkout') or r.get('Check-out') or '')
        if checkin and checkout:
            nights = (checkout - checkin).days

        gross = _fnum(r.get('Importo') or r.get('Amount') or 0)
        commission = _fnum(r.get('Commissione') or r.get('Commission') or 0)
        withholding = _fnum(r.get('Ritenuta sugli affitti a breve termine') or r.get('Withholding') or 0)
        vat = _fnum(r.get('VAT for online platform services') or r.get('VAT') or 0)
        transaction = _fnum(r.get('Costo di transazione') or r.get('Transaction cost') or 0)
        net = _fnum(r.get('Netto') or r.get('Net') or 0)
        # Netto must reconcile: Importo + Commissione + Ritenuta + VAT + Transazione
        # (fees negative in the file). Warn on drift so formula changes get noticed.
        _reconciled = round(gross + commission + withholding + vat + transaction, 2)
        if net and abs(_reconciled - net) > 0.02:
            errors.append(f'Riga {idx} {code}: Netto {net:.2f} != somma parti {_reconciled:.2f} — controlla le formule')

        start = checkin
        end = checkout
        payout_date = _parse_date(r.get('Data del pagamento') or r.get('Payment date') or '')
        booking_date = None  # Not provided in Booking.com export

        entry = {
            'row': idx,
            'type': typ,
            'code': code,
            'guest': (r.get('Nome dell\'ospite') or r.get('Guest name') or '').strip(),
            'payment_provider': (r.get('Fornitore di servizi di pagamento') or '').strip(),
            'booking_status': (r.get('Stato della prenotazione') or '').strip(),
            'currency': (r.get('Valuta') or r.get('Currency') or 'EUR').strip(),
            'payment_status': (r.get('Stato del pagamento') or '').strip(),
            'start': start,
            'end': end,
            'nights': nights,
            'gross': gross,
            'commission': commission,
            'withholding': withholding,
            'vat': vat,
            'transaction': transaction,
            'net': net,
            'payout_date': payout_date,
            'booking_date': booking_date,
            'payment_id': (r.get('ID pagamento') or r.get('Payment ID') or '').strip(),
            'raw': r,
        }

        reservations.append(entry)
        per_code[code] = entry

    # totals
    total_gross = sum(r.get('gross', 0) for r in reservations)
    total_commission = sum(r.get('commission', 0) for r in reservations)
    total_withholding = sum(r.get('withholding', 0) for r in reservations)
    total_vat = sum(r.get('vat', 0) for r in reservations)
    total_transaction = sum(r.get('transaction', 0) for r in reservations)
    total_net = sum(r.get('net', 0) for r in reservations)
    total_nights = sum(r.get('nights', 0) for r in reservations)

    # monthly by start date
    monthly_map = defaultdict(lambda: {'gross': 0.0, 'commission': 0.0, 'withholding': 0.0, 'vat': 0.0, 'transaction': 0.0, 'net': 0.0, 'nights': 0, 'count': 0})
    for code, d in per_code.items():
        start = d.get('start')
        if not start:
            continue
        key = start.strftime('%Y-%m')
        monthly_map[key]['gross'] += d.get('gross', 0)
        monthly_map[key]['commission'] += d.get('commission', 0)
        monthly_map[key]['withholding'] += d.get('withholding', 0)
        monthly_map[key]['vat'] += d.get('vat', 0)
        monthly_map[key]['transaction'] += d.get('transaction', 0)
        monthly_map[key]['net'] += d.get('net', 0)
        monthly_map[key]['nights'] += d.get('nights', 0)
        monthly_map[key]['count'] += 1

    monthly = []
    for k in sorted(monthly_map.keys()):
        v = monthly_map[k]
        v['month'] = k
        try:
            dt = datetime.strptime(k, '%Y-%m')
            v['label'] = dt.strftime('%b %Y')
        except Exception:
            v['label'] = k
        v['avg_gross_night'] = (v['gross'] / v['nights']) if v['nights'] else 0
        v['avg_net_night'] = (v['net'] / v['nights']) if v['nights'] else 0
        monthly.append(v)

    totals = {
        'count': len(per_code),
        'reservations': len(reservations),
        'nights': total_nights,
        'gross': total_gross,
        'commission': total_commission,
        'withholding': total_withholding,
        'vat': total_vat,
        'transaction': total_transaction,
        'net': total_net,
        'avg_gross_night': (total_gross / total_nights) if total_nights else 0,
        'avg_net_night': (total_net / total_nights) if total_nights else 0,
        'withholding_rate': (-total_withholding / total_gross * 100) if total_gross else 0,
    }

    per_code_list = []
    for code, d in per_code.items():
        per_code_list.append({'code': code, **d})
    per_code_list.sort(key=lambda x: (x.get('start') or date.min))

    return {
        'reservations': reservations,
        'per_code': per_code_list,
        'totals': totals,
        'monthly': monthly,
        'errors': errors,
        'raw_rows': len(reservations),
    }


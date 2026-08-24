"""
Airbnb Earnings CSV parser — Lotto 235

Handles the Airbnb "Earnings" export:
Date,Type,Confirmation code,Booking date,Start date,End date,Nights,Guest,Listing,...
Columns are stable but we are defensive: missing cols, empty strings, different
date formats (MM/DD/YYYY or DD/MM/YYYY), EUR amounts with comma/dot.
"""
import csv
import io
from collections import defaultdict
from datetime import datetime, date
from typing import Any


def _fnum(s: Any) -> float:
    if s is None:
        return 0.0
    s = str(s).strip().replace('\u00a0', '').replace('€', '').strip()
    if not s or s == '-':
        return 0.0
    # handle both 1.234,56 and 1,234.56 -> normalize
    # Airbnb export is usually 123.45 with dot, but be safe
    if ',' in s and '.' in s:
        # assume comma is thousand sep if dot is decimal
        if s.rfind(',') > s.rfind('.'):
            s = s.replace('.', '').replace(',', '.')
        else:
            s = s.replace(',', '')
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
    for fmt in ('%m/%d/%Y', '%m/%d/%y', '%d/%m/%Y', '%d/%m/%y', '%Y-%m-%d', '%Y/%m/%d'):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def parse_earnings_csv(file_bytes: bytes | str) -> dict:
    """
    Parse raw CSV bytes/str and return a dict with:
      reservations: list[dict]
      withholdings: list[dict]
      per_code: dict[code -> {reservation, withholding, gross, withholding_amt, net, ...}]
      totals: {count, nights, gross, amount, service, cleaning, airbnb_tax, withholding, net, avg_gross_night, avg_net_night}
      monthly: list[{month, gross, withholding, net, nights, count, avg_night}]
      errors: list[str]
    """
    if isinstance(file_bytes, bytes):
        # try utf-8, fallback to utf-8-sig, cp1252
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

    # strip BOM if present
    if text.startswith('\ufeff'):
        text = text.lstrip('\ufeff')

    reader = csv.DictReader(io.StringIO(text))
    # normalize header names (strip)
    if reader.fieldnames:
        reader.fieldnames = [h.strip() for h in reader.fieldnames]

    reservations = []
    withholdings = []
    per_code: dict[str, dict] = {}
    errors = []

    for idx, row in enumerate(reader, start=2):
        # normalize keys
        r = {k.strip(): (v.strip() if isinstance(v, str) else v) for k, v in row.items()}
        typ = (r.get('Type') or '').strip()
        code = (r.get('Confirmation code') or '').strip()
        if not code:
            continue
        # parse numeric
        nights = 0
        try:
            nights = int(float(r.get('Nights') or 0))
        except Exception:
            nights = 0
        gross = _fnum(r.get('Gross earnings'))
        amount = _fnum(r.get('Amount'))
        service = _fnum(r.get('Service fee'))
        cleaning = _fnum(r.get('Cleaning fee'))
        airbnb_tax = _fnum(r.get('Airbnb remitted tax'))
        start = _parse_date(r.get('Start date') or '')
        end = _parse_date(r.get('End date') or '')
        payout_date = _parse_date(r.get('Date') or '')

        entry = {
            'row': idx,
            'type': typ,
            'code': code,
            'guest': (r.get('Guest') or '').strip(),
            'listing': (r.get('Listing') or '').strip(),
            'start': start,
            'end': end,
            'nights': nights,
            'amount': amount,
            'service': service,
            'cleaning': cleaning,
            'gross': gross,
            'airbnb_tax': airbnb_tax,
            'payout_date': payout_date,
            'currency': (r.get('Currency') or 'EUR').strip(),
            'raw': r,
        }

        if typ == 'Reservation':
            reservations.append(entry)
            per_code.setdefault(code, {})['reservation'] = entry
        elif 'Tax Withholding' in typ:
            withholdings.append(entry)
            per_code.setdefault(code, {})['withholding'] = entry
        else:
            # other types (e.g. adjustments) — treat as reservation if gross present
            if gross:
                reservations.append(entry)
                per_code.setdefault(code, {})['reservation'] = entry
            else:
                withholdings.append(entry)

    # build per-code net — net is what lands in your bank: Amount + withholding
    # Amount is already after Airbnb commission and includes cleaning (e.g. Karen: (360-72)+40 -62.02 commission = 265.97 Amount; 265.97-68.88 withholding =197.09 net)
    # Gross is shown for reference but not used for net (Gross ≈ Amount+Service but off by fees).
    for code, d in per_code.items():
        res = d.get('reservation') or {}
        wh = d.get('withholding') or {}
        gross = res.get('gross', 0) or _fnum(res.get('Amount', 0)) if res else 0
        # if gross missing but we have amount+cleaning+service, fallback
        if not gross and res:
            gross = _fnum(res.get('Amount', 0)) + _fnum(res.get('Cleaning fee', 0))  # approx
        wh_amt = wh.get('amount', 0) if wh else 0
        service = res.get('service', 0) if res else 0
        amount = res.get('amount', 0) if res else 0
        # Withholding is negative, Amount already net of Airbnb service fee
        net = amount + wh_amt
        d['gross'] = gross
        d['withholding'] = wh_amt
        d['service'] = service
        d['amount'] = amount
        d['net'] = net
        d['nights'] = res.get('nights', 0) if res else (wh.get('nights', 0) if wh else 0)
        d['guest'] = res.get('guest') or wh.get('guest') or ''
        d['start'] = res.get('start') or wh.get('start')
        d['end'] = res.get('end') or wh.get('end')
        d['cleaning'] = res.get('cleaning', 0) if res else 0
        d['airbnb_tax'] = res.get('airbnb_tax', 0) if res else 0

    total_gross = sum(r.get('gross', 0) for r in reservations if r.get('gross'))
    # fallback if gross column empty for some: use amount
    if total_gross == 0:
        total_gross = sum(r.get('amount', 0) for r in reservations)
    total_amount = sum(r.get('amount', 0) for r in reservations)
    total_service = sum(r.get('service', 0) for r in reservations)
    total_cleaning = sum(r.get('cleaning', 0) for r in reservations)
    total_airbnb_tax = sum(r.get('airbnb_tax', 0) for r in reservations)
    total_withholding = sum(w.get('amount', 0) for w in withholdings)  # negative
    total_nights = sum(r.get('nights', 0) for r in reservations)
    # Net = Amount + Withholding — Amount already includes (room - discount + cleaning - Airbnb commission)
    # Example Karen: 265.97 Amount -68.88 =197.09 net; Gross 316.81 shown for reference only.
    net = total_amount + total_withholding

    # monthly by start date (fallback to payout date)
    monthly_map = defaultdict(lambda: {'gross': 0.0, 'withholding': 0.0, 'net': 0.0, 'nights': 0, 'count': 0, 'service': 0.0, 'cleaning': 0.0})
    for code, d in per_code.items():
        start = d.get('start')
        if not start:
            # try payout date from reservation
            start = (d.get('reservation') or {}).get('payout_date')
        if not start:
            continue
        key = start.strftime('%Y-%m')
        monthly_map[key]['gross'] += d.get('gross', 0)
        monthly_map[key]['withholding'] += d.get('withholding', 0)
        monthly_map[key]['net'] += d.get('net', 0)
        monthly_map[key]['nights'] += d.get('nights', 0)
        monthly_map[key]['count'] += 1
        monthly_map[key]['service'] += d.get('service', 0)
        monthly_map[key]['cleaning'] += d.get('cleaning', 0)

    monthly = []
    for k in sorted(monthly_map.keys()):
        v = monthly_map[k]
        v['month'] = k
        # label like 2026-08
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
        'withholdings': len(withholdings),
        'nights': total_nights,
        'gross': total_gross,
        'amount': total_amount,
        'service': total_service,
        'cleaning': total_cleaning,
        'airbnb_tax': total_airbnb_tax,
        'withholding': total_withholding,
        'net': net,
        'avg_gross_night': (total_gross / total_nights) if total_nights else 0,
        'avg_net_night': (net / total_nights) if total_nights else 0,
        'withholding_rate': (-total_withholding / total_gross * 100) if total_gross else 0,
    }

    # sort per_code list by start date
    per_code_list = []
    for code, d in per_code.items():
        per_code_list.append({
            'code': code,
            **d,
        })
    per_code_list.sort(key=lambda x: (x.get('start') or date.min))

    return {
        'reservations': reservations,
        'withholdings': withholdings,
        'per_code': per_code_list,
        'totals': totals,
        'monthly': monthly,
        'errors': errors,
        'raw_rows': len(reservations) + len(withholdings),
    }

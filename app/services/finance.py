"""Finance aggregation: Gross → Net Sankey data — Lotto 235.

Sources
-------
- OTA revenue: ``Earning`` rows (authoritative payout data from Airbnb /
  Booking.com CSV imports). Dated by ``start_date`` (stay month), fallback
  to ``payout_date``.
- Direct revenue: ``Reservation`` rows with ``source in ('direct', 'stripe')``,
  ``status == 'confirmed'``, not a calendar block. Dated by ``check_in``.
  OTA reservations are NOT taken from ``Reservation`` to avoid double
  counting with ``Earning``.
- Running costs: ``RunningCost`` rows. ``category == 'imu'`` counts only in
  the yearly view (yearly tax, not a monthly operating cost).

Flow (balances by construction)
-------------------------------
Gross (= ota_gross + direct_gross)
  ├─ ota_fees (= ota_gross - ota_amount) + stripe_fees → Payout
  └─ Payout (= ota_amount + direct_net_of_stripe)
       ├─ cedolare (= |ota_withholding| + 21% of direct_gross) → AfterTax
       └─ AfterTax (= Payout - cedolare)
            ├─ running costs (internet / cleaning / electricity / other [+ imu yearly])
            └─ Net
"""

from __future__ import annotations

from datetime import date
from typing import Any

from app.models import Earning, Reservation, RunningCost

CEDOLARE_RATE = 0.21
STRIPE_PCT = 0.015
STRIPE_FIXED = 0.25
DIRECT_SOURCES = ('direct', 'stripe')


def _earning_period(e: Earning) -> date | None:
    return e.start_date or e.payout_date


def _in_month(d: date | None, year: int, month: int | None) -> bool:
    if d is None:
        return False
    if month is None:
        return d.year == year
    return d.year == year and d.month == month


def compute_finance(year: int, month: int | None = None) -> dict[str, Any]:
    """Aggregate revenue + costs for a month (``month`` set) or a year."""
    earnings = Earning.query.all()
    ota_gross = ota_amount = ota_withholding = 0.0
    ota_nights = ota_count = 0
    for e in earnings:
        if not _in_month(_earning_period(e), year, month):
            continue
        ota_gross += e.gross_earnings or 0.0
        ota_amount += e.amount or 0.0
        ota_withholding += e.withholding or 0.0  # negative
        ota_nights += e.nights or 0
        ota_count += 1
    ota_gross = round(ota_gross, 2)
    ota_amount = round(ota_amount, 2)
    ota_withholding = round(ota_withholding, 2)
    ota_fees = round(max(0.0, ota_gross - ota_amount), 2)

    reservations = Reservation.query.filter(
        Reservation.status == 'confirmed',
        Reservation.is_block.is_(False),
        Reservation.source.in_(DIRECT_SOURCES),
    ).all()
    direct_gross = 0.0
    direct_stripe_fees = 0.0
    direct_nights = direct_count = 0
    for r in reservations:
        if not _in_month(r.check_in, year, month):
            continue
        total = r.total_price or 0.0
        direct_gross += total
        if (r.payment_method or '') == 'stripe' and total > 0:
            direct_stripe_fees += round(total * STRIPE_PCT + STRIPE_FIXED, 2)
        direct_nights += r.nights or 0
        direct_count += 1
    direct_gross = round(direct_gross, 2)
    direct_stripe_fees = round(direct_stripe_fees, 2)

    gross = round(ota_gross + direct_gross, 2)
    fees = round(ota_fees + direct_stripe_fees, 2)
    payout = round(gross - fees, 2)  # == ota_amount + (direct_gross - stripe_fees)

    cedolare_ota = round(abs(ota_withholding), 2)
    cedolare_direct = round(direct_gross * CEDOLARE_RATE, 2) if direct_gross else 0.0
    cedolare = round(cedolare_ota + cedolare_direct, 2)
    after_tax = round(payout - cedolare, 2)

    year_costs = RunningCost.query.filter_by(year=year).all()
    # Monthly view: only entries booked to that month (yearly rows have month NULL).
    costs = year_costs if month is None else [c for c in year_costs if c.month == month]
    by_category: dict[str, float] = {'internet': 0.0, 'cleaning': 0.0, 'electricity': 0.0, 'imu': 0.0, 'other': 0.0}
    for c in costs:
        cat = (c.category or 'other') if (c.category or 'other') in by_category else 'other'
        by_category[cat] += c.amount or 0.0
    by_category = {k: round(v, 2) for k, v in by_category.items()}

    if month is None:
        running_total = round(sum(by_category.values()), 2)
        imu = by_category['imu']
        operating = round(running_total - imu, 2)
    else:
        # Monthly view excludes IMU (yearly tax shown in yearly view only).
        imu = 0.0
        operating = round(sum(v for k, v in by_category.items() if k != 'imu'), 2)
        running_total = operating

    net = round(after_tax - running_total, 2)

    return {
        'year': year,
        'month': month,
        'ota': {
            'gross': ota_gross,
            'amount': ota_amount,
            'fees': ota_fees,
            'withholding': ota_withholding,
            'nights': ota_nights,
            'count': ota_count,
        },
        'direct': {
            'gross': direct_gross,
            'stripe_fees': direct_stripe_fees,
            'cedolare_estimate': cedolare_direct,
            'nights': direct_nights,
            'count': direct_count,
        },
        'gross': gross,
        'fees': fees,
        'payout': payout,
        'cedolare_ota': cedolare_ota,
        'cedolare_direct': cedolare_direct,
        'cedolare': cedolare,
        'after_tax': after_tax,
        'costs': by_category,
        'imu': imu,
        'operating': operating,
        'running_total': running_total,
        'net': net,
    }


def sankey_data(fin: dict[str, Any]) -> dict[str, Any]:
    """Build Plotly Sankey ``{labels, sources, targets, values}`` from ``compute_finance``.

    Nodes: Gross → Fees, Payout → Cedolare, AfterTax → each cost → Net.
    Zero-value links are dropped so empty periods render cleanly.
    """
    labels = [
        'Gross revenue',
        'OTA + Stripe fees',
        'Payout',
        'Cedolare secca 21%',
        'After tax',
        'Internet',
        'Cleaning',
        'Electricity',
        'Other costs',
    ]
    if fin['imu']:
        labels.append('IMU')
    labels.append('Net profit')

    idx = {label: i for i, label in enumerate(labels)}

    links: list[tuple[str, str, float]] = [
        ('Gross revenue', 'OTA + Stripe fees', fin['fees']),
        ('Gross revenue', 'Payout', fin['payout']),
        ('Payout', 'Cedolare secca 21%', fin['cedolare']),
        ('Payout', 'After tax', fin['after_tax']),
        ('After tax', 'Internet', fin['costs']['internet']),
        ('After tax', 'Cleaning', fin['costs']['cleaning']),
        ('After tax', 'Electricity', fin['costs']['electricity']),
        ('After tax', 'Other costs', fin['costs']['other']),
    ]
    if fin['imu']:
        links.append(('After tax', 'IMU', fin['imu']))
    after_costs = fin['running_total']
    links.append(('After tax', 'Net profit', max(0.0, round(fin['after_tax'] - after_costs, 2))))

    sources, targets, values = [], [], []
    for s, t, v in links:
        if v and v > 0:
            sources.append(idx[s])
            targets.append(idx[t])
            values.append(round(v, 2))

    return {'labels': labels, 'sources': sources, 'targets': targets, 'values': values}


def available_years() -> list[int]:
    """Years present in any finance source (for the year selector)."""
    years: set[int] = set()
    for e in Earning.query.all():
        d = _earning_period(e)
        if d:
            years.add(d.year)
    for r in Reservation.query.filter(
        Reservation.status == 'confirmed',
        Reservation.is_block.is_(False),
        Reservation.source.in_(DIRECT_SOURCES),
    ).all():
        if r.check_in:
            years.add(r.check_in.year)
    for c in RunningCost.query.all():
        if c.year:
            years.add(c.year)
    if not years:
        years.add(date.today().year)
    return sorted(years, reverse=True)

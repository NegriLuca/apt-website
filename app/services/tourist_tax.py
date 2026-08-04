"""
Tourist tax (Tassa di Soggiorno) calculation and export for Roma Capitale.
"""

import csv
import io
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from app import db
from app.models import Apartment, Reservation


@dataclass
class TaxRow:
    """Single row for tourist tax CSV export"""

    reservation_id: int
    guest_name: str
    check_in: date
    check_out: date
    nights: int
    guests: int
    taxable_guests: int
    rate_per_night: float
    total_tax: float
    apartment_name: str
    cin_code: str
    status: str


class TouristTaxService:
    """Calculate and export tourist tax for Roma Capitale"""

    # Roma 2024 rates by category (euro per night per person)
    DEFAULT_RATES = {
        'CAV': 6.00,  # Case per vacanze / vacation rentals
        'BB': 6.00,  # Bed & Breakfast
        'HOTEL_1': 6.00,
        'HOTEL_2': 6.00,
        'HOTEL_3': 6.00,
        'HOTEL_4': 6.00,
        'HOTEL_5': 6.00,
        'OSTELLO': 6.00,
        'CAMPEGGIO': 6.00,
    }

    # Exemptions
    EXEMPT_AGE = 10  # Children under 10 exempt
    MAX_TAXABLE_NIGHTS = 10  # Max 10 nights taxable per stay

    def __init__(self, apartment: Apartment = None):
        self.apartment = apartment
        if apartment:
            rate_val = apartment.tourist_tax_rate or self.DEFAULT_RATES.get(apartment.tourist_tax_category, 6.00)
            self.rate = float(rate_val)
            self.category = apartment.tourist_tax_category or 'CAV'
            self.cin = apartment.cin_code
        else:
            self.rate = 6.00
            self.category = 'CAV'
            self.cin = None

    def _taxable_guests(self, reservation: Reservation) -> int:
        """Number of guests subject to the tax (adults; children 3-9 exempt).

        Falls back to the total guest count for legacy bookings created before
        adults/children breakdown was collected.
        """
        if reservation.num_adults:
            return reservation.num_adults
        return reservation.num_guests or 1

    def calculate_tax(self, reservation: Reservation, guest_ages: list[int] = None) -> float:
        """Calculate tourist tax for a reservation

        Args:
            reservation: The reservation
            guest_ages: List of guest ages (for exemption calculation)
                        If None, uses num_adults (children 3-9 exempt)
        """
        if reservation.status != 'confirmed':
            return 0.0

        nights = min(reservation.nights, self.MAX_TAXABLE_NIGHTS)

        # Calculate taxable guests (exclude children under 10)
        if guest_ages:
            taxable_guests = sum(1 for age in guest_ages if age >= self.EXEMPT_AGE)
        else:
            taxable_guests = self._taxable_guests(reservation)

        if taxable_guests <= 0:
            return 0.0

        total = nights * taxable_guests * self.rate
        return round(total, 2)

    def calculate_for_reservation(self, reservation: Reservation) -> TaxRow:
        """Generate tax row for CSV export"""
        total_tax = self.calculate_tax(reservation)

        return TaxRow(
            reservation_id=reservation.id,
            guest_name=reservation.guest_name,
            check_in=reservation.check_in,
            check_out=reservation.check_out,
            nights=min(reservation.nights, self.MAX_TAXABLE_NIGHTS),
            guests=reservation.num_guests,
            taxable_guests=self._taxable_guests(reservation),
            rate_per_night=self.rate,
            total_tax=total_tax,
            apartment_name=self.apartment.name if self.apartment else 'Unknown',
            cin_code=self.cin or '',
            status=reservation.status,
        )

    def export_monthly_csv(self, year: int, month: int) -> str:
        """Generate CSV for Roma Capitale monthly declaration"""
        # Date range for the month
        start_date = date(year, month, 1)
        if month == 12:
            end_date = date(year + 1, 1, 1) - timedelta(days=1)
        else:
            end_date = date(year, month + 1, 1) - timedelta(days=1)

        # Query confirmed reservations with check-in in this month (exclude flagged ones)
        reservations = (
            Reservation.query.filter(
                Reservation.status == 'confirmed',
                Reservation.tourist_tax_excluded != True,
                Reservation.check_in >= start_date,
                Reservation.check_in <= end_date,
            )
            .order_by(Reservation.check_in)
            .all()
        )

        if not reservations:
            output = io.StringIO()
            writer = csv.writer(output, delimiter=';')
            writer.writerow(self._get_csv_headers())
            return output.getvalue()

        rows = []
        total_tax = 0.0
        for res in reservations:
            row = self.calculate_for_reservation(res)
            rows.append(row)
            total_tax += row.total_tax

            res.tourist_tax_amount = row.total_tax
            db.session.add(res)
        db.session.commit()

        output = io.StringIO()
        writer = csv.writer(output, delimiter=';', quoting=csv.QUOTE_MINIMAL)
        writer.writerow(self._get_csv_headers())

        for row in rows:
            writer.writerow(self._row_to_csv(row))

        writer.writerow(['', '', '', '', '', '', '', f'{total_tax:.2f}', '', '', ''])

        return output.getvalue()

    def _get_csv_headers(self) -> list[str]:
        """CSV headers matching Roma Capitale format"""
        return [
            'ID Prenotazione',
            'Ospite',
            'Check-in',
            'Check-out',
            'Notti tassabili',
            'Ospiti totali',
            'Ospiti tassabili',
            'Tariffa/notte/ospite (€)',
            'Totale tassa (€)',
            'Appartamento',
            'CIN',
            'Stato',
        ]

    def _row_to_csv(self, row: TaxRow) -> list[str]:
        return [
            str(row.reservation_id),
            row.guest_name,
            row.check_in.strftime('%d/%m/%Y'),
            row.check_out.strftime('%d/%m/%Y'),
            str(row.nights),
            str(row.guests),
            str(row.taxable_guests),
            f'{row.rate_per_night:.2f}',
            f'{row.total_tax:.2f}',
            row.apartment_name,
            row.cin_code,
            row.status,
        ]

    def generate_detailed_report(self, year: int, month: int) -> dict[str, Any]:
        """Generate detailed tax report for admin review"""
        csv_data = self.export_monthly_csv(year, month)
        csv_data.strip().split('\n')

        reservations = Reservation.query.filter(
            Reservation.status == 'confirmed',
            Reservation.tourist_tax_excluded != True,
            Reservation.check_in >= date(year, month, 1),
            Reservation.check_in
            <= date(year, month + 1 if month < 12 else year + 1, 1 if month < 12 else 1) - timedelta(days=1),
        ).all()

        return {
            'period': f'{month:02d}/{year}',
            'category': self.category,
            'rate': self.rate,
            'total_reservations': len(reservations),
            'total_nights': sum(r.nights for r in reservations),
            'total_guests': sum(r.num_guests for r in reservations),
            'total_tax': sum(r.tourist_tax_amount or 0 for r in reservations),
            'csv_data': csv_data,
            'reservations': [
                {
                    'id': r.id,
                    'guest': r.guest_name,
                    'check_in': r.check_in.isoformat(),
                    'check_out': r.check_out.isoformat(),
                    'nights': r.nights,
                    'guests': r.num_guests,
                    'tax': r.tourist_tax_amount or 0,
                    'tax_paid': r.tourist_tax_paid,
                    'excluded': r.tourist_tax_excluded or False,
                }
                for r in reservations
            ],
        }


def get_tax_service(apartment: Apartment = None) -> TouristTaxService:
    """Factory function"""
    return TouristTaxService(apartment)


# Scheduled task for monthly report
def generate_monthly_tax_report(year: int = None, month: int = None) -> dict[str, Any]:
    """Generate monthly tax report for previous month"""
    today = date.today()
    if year is None:
        year = today.year if today.month > 1 else today.year - 1
    if month is None:
        month = today.month - 1 if today.month > 1 else 12

    apt = Apartment.query.first()
    service = get_tax_service(apt)
    return service.generate_detailed_report(year, month)

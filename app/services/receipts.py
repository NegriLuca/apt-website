"""
Ricevuta fiscale italiana — servizio per generazione numerazione e PDF.

Valida per locazioni brevi non-imprenditoriali:
- numerazione progressiva annuale 01/2026, reset 01/01
- solo prenotazioni direct/stripe (sito)
- bollo 2€ se imponibile soggiorno > 77.47
- indirizzo guest preso da Stripe customer_details se disponibile
"""
from datetime import date, datetime
from typing import Optional

from flask import current_app

from app import db
from app.models import Apartment, Receipt, Reservation

BOLLO_THRESHOLD = 77.47
BOLLO_AMOUNT = 2.00
IVA_EXEMPTION_TEXT = "Operazione fuori campo di applicazione dell'IVA ai sensi dell'art. 1, comma 2, D.P.R. 633/1972"


def is_direct_reservation(reservation: Reservation) -> bool:
    return (reservation.source or 'direct') in ('direct', 'stripe')


def _format_receipt_number(sequence: int, year: int) -> str:
    return f"{sequence:02d}/{year}"


def get_next_sequence(year: int) -> int:
    max_seq = db.session.query(db.func.max(Receipt.sequence)).filter(Receipt.year == year).scalar()
    return (max_seq or 0) + 1


def _snapshot_host(apartment: Optional[Apartment]) -> dict:
    if not apartment:
        return dict(
            host_full_name=current_app.config.get('HOST_FULL_NAME', '') or '',
            host_codice_fiscale=current_app.config.get('HOST_CODICE_FISCALE', '') or '',
            host_address=current_app.config.get('HOST_ADDRESS', 'Via Lotto 235, 00153 Roma'),
            cin_code=current_app.config.get('CIN_CODE', ''),
            cir_code=current_app.config.get('CIR_CODE', ''),
        )
    return dict(
        host_full_name=apartment.host_full_name or current_app.config.get('HOST_FULL_NAME', '') or '',
        host_codice_fiscale=apartment.host_codice_fiscale or current_app.config.get('HOST_CODICE_FISCALE', '') or '',
        host_address=apartment.host_address or current_app.config.get('HOST_ADDRESS', 'Via Lotto 235, 00153 Roma'),
        cin_code=apartment.cin_code or current_app.config.get('CIN_CODE', ''),
        cir_code=apartment.cir_code or current_app.config.get('CIR_CODE', ''),
    )


def _guest_address_snapshot(res: Reservation) -> dict:
    # Prefer Stripe billing address if present, else manual residence fields
    line1 = res.guest_billing_address_line1 or res.guest_residence_address or ''
    city = res.guest_billing_city or res.guest_residence_city or ''
    zipc = res.guest_billing_postal_code or res.guest_residence_zip or ''
    country = res.guest_billing_country or res.guest_residence_country or res.guest_nationality or ''
    # Build single line for PDF
    parts = [p for p in [line1, res.guest_billing_address_line2, city, zipc, country] if p]
    # For manual fallback include city if billing empty
    if not parts and res.guest_residence_address:
        parts = [res.guest_residence_address, res.guest_residence_city or '', res.guest_residence_country or '']
    return dict(
        guest_residence_address=line1,
        guest_residence_city=city,
        guest_residence_zip=zipc,
        guest_residence_country=country,
    )


def create_or_get_receipt(reservation: Reservation, issue_date: Optional[date] = None, bollo_id: Optional[str] = None) -> Receipt:
    """Create receipt for direct reservation if not exists, else return existing.

    Raises ValueError if not direct or missing host data.
    """
    if not is_direct_reservation(reservation):
        raise ValueError("Ricevuta solo per prenotazioni dirette dal sito (direct/stripe)")

    existing = Receipt.query.filter_by(reservation_id=reservation.id).first()
    if existing:
        # allow updating bollo_id if supplied
        if bollo_id and not existing.bollo_id:
            # validate 14 digits if provided
            digits = ''.join(c for c in bollo_id if c.isdigit())
            if len(digits) == 14:
                existing.bollo_id = digits
                db.session.commit()
        return existing

    year = (issue_date or date.today()).year
    sequence = get_next_sequence(year)
    receipt_number = _format_receipt_number(sequence, year)

    stay_amount = float(reservation.total_price or 0.0)
    tourist_tax = float(reservation.tourist_tax_amount or 0.0)
    total = round(stay_amount + tourist_tax, 2)

    bollo_required = stay_amount > BOLLO_THRESHOLD
    bollo_amount = BOLLO_AMOUNT if bollo_required else 0.0

    apartment = Apartment.query.first()
    host = _snapshot_host(apartment)
    guest_snap = _guest_address_snapshot(reservation)

    guest_full = reservation.guest_full_name if hasattr(reservation, 'guest_full_name') else reservation.guest_name
    # Prefer guest_codice_fiscale, fallback to document number for foreign
    guest_cf = reservation.guest_codice_fiscale or ''

    receipt = Receipt(
        reservation_id=reservation.id,
        year=year,
        sequence=sequence,
        receipt_number=receipt_number,
        issue_date=issue_date or date.today(),
        stay_amount=round(stay_amount, 2),
        tourist_tax_amount=round(tourist_tax, 2),
        total_amount=total,
        payment_method=reservation.payment_method or 'stripe',
        stripe_payment_intent_id=reservation.stripe_payment_intent_id,
        stripe_charge_id=reservation.stripe_charge_id,
        stripe_receipt_url=reservation.stripe_receipt_url,
        bollo_required=bollo_required,
        bollo_amount=bollo_amount,
        bollo_id=''.join(c for c in (bollo_id or '') if c.isdigit()) if bollo_id and len(''.join(c for c in bollo_id if c.isdigit())) == 14 else None,
        host_full_name=host['host_full_name'],
        host_codice_fiscale=host['host_codice_fiscale'],
        host_address=host['host_address'],
        cin_code=host['cin_code'],
        cir_code=host['cir_code'],
        guest_full_name=guest_full,
        guest_email=reservation.guest_email,
        guest_residence_address=guest_snap['guest_residence_address'],
        guest_residence_city=guest_snap['guest_residence_city'],
        guest_residence_zip=guest_snap['guest_residence_zip'],
        guest_residence_country=guest_snap['guest_residence_country'],
        guest_codice_fiscale=guest_cf,
        guest_document_type=reservation.guest_document_type,
        guest_document_number=reservation.guest_document_number,
        check_in=reservation.check_in,
        check_out=reservation.check_out,
        nights=reservation.nights,
        num_guests=reservation.num_guests,
    )
    db.session.add(receipt)
    db.session.commit()
    return receipt


def update_receipt_bollo(receipt: Receipt, bollo_id: str) -> Receipt:
    digits = ''.join(c for c in (bollo_id or '') if c.isdigit())
    if digits and len(digits) != 14:
        raise ValueError("Il codice marca da bollo deve contenere 14 cifre")
    receipt.bollo_id = digits or None
    db.session.commit()
    return receipt


def enrich_reservation_from_stripe_session(reservation: Reservation, session_obj: dict) -> bool:
    """Extract billing address + charge/receipt URL from Stripe checkout session dict.

    Works with dict from stripe.checkout.Session.retrieve(expand=['payment_intent.charges']).
    Returns True if any field updated.
    """
    updated = False
    try:
        customer_details = session_obj.get('customer_details') or {}
        address = customer_details.get('address') or {}
        if address:
            line1 = address.get('line1')
            line2 = address.get('line2')
            city = address.get('city')
            postal = address.get('postal_code')
            country = address.get('country')
            state = address.get('state')
            if line1 and not reservation.guest_billing_address_line1:
                reservation.guest_billing_address_line1 = line1
                updated = True
            if line2:
                reservation.guest_billing_address_line2 = line2
                updated = True
            if city and not reservation.guest_billing_city:
                reservation.guest_billing_city = city
                updated = True
            if postal and not reservation.guest_billing_postal_code:
                reservation.guest_billing_postal_code = postal
                updated = True
            if country and not reservation.guest_billing_country:
                reservation.guest_billing_country = country
                updated = True
            if state and not reservation.guest_billing_state:
                reservation.guest_billing_state = state
                updated = True
            # also fill generic residence fields if empty
            if line1 and not reservation.guest_residence_address:
                reservation.guest_residence_address = line1
                updated = True
            if city and not reservation.guest_residence_city:
                reservation.guest_residence_city = city
                updated = True
            if postal and not reservation.guest_residence_zip:
                reservation.guest_residence_zip = postal
                updated = True
            if country and not reservation.guest_residence_country:
                reservation.guest_residence_country = country
                updated = True

        # payment_intent + charges
        pi = session_obj.get('payment_intent')
        # when expanded, pi is dict
        if isinstance(pi, dict):
            charges = pi.get('charges', {}).get('data', []) if isinstance(pi.get('charges'), dict) else []
            if charges:
                ch = charges[0]
                if ch.get('id') and not reservation.stripe_charge_id:
                    reservation.stripe_charge_id = ch.get('id')
                    updated = True
                if ch.get('receipt_url') and not reservation.stripe_receipt_url:
                    reservation.stripe_receipt_url = ch.get('receipt_url')
                    updated = True
        elif isinstance(pi, str):
            if not reservation.stripe_payment_intent_id:
                reservation.stripe_payment_intent_id = pi
                updated = True

        # fallback: session payment_intent string already stored elsewhere
        if updated:
            db.session.commit()
    except Exception as e:
        current_app.logger.warning(f"Stripe enrich failed for res#{reservation.id}: {e}")
    return updated


# ── PDF generation ────────────────────────────────────────────────────────────

def generate_receipt_pdf_bytes(receipt: Receipt) -> bytes:
    """Generate PDF bytes for a receipt using fpdf2."""
    from fpdf import FPDF

    pdf = FPDF(format='A4')
    pdf.set_auto_page_break(auto=True, margin=18)
    pdf.add_page()

    def _sanitize(t: str) -> str:
        # Helvetica core font = latin-1 only — replace unsupported unicode
        return (
            t.replace('—', '-')
            .replace('–', '-')
            .replace('≤', '<=')
            .replace('≥', '>=')
            .replace('€', 'EUR ')
            .replace('’', "'")
            .replace('‘', "'")
            .replace('“', '"')
            .replace('”', '"')
            .replace('•', '-')
        )

    # patch core-font cells to auto-sanitize
    _orig_cell = pdf.cell
    def _cell(w, h=0, text='', **kw):
        if isinstance(text, str):
            text = _sanitize(text)
        return _orig_cell(w, h, text, **kw)
    pdf.cell = _cell  # type: ignore
    _orig_multi = pdf.multi_cell
    def _multi(w, h=0, text='', **kw):
        if isinstance(text, str):
            text = _sanitize(text)
        return _orig_multi(w, h, text, **kw)
    pdf.multi_cell = _multi  # type: ignore

    # Use Helvetica (core font) — no embedding issues
    def _money(v):
        return f"EUR {v:,.2f}".replace(',', 'X').replace('.', ',').replace('X', '.')

    # Header
    pdf.set_font('Helvetica', 'B', 14)
    pdf.cell(0, 8, 'RICEVUTA DI LOCAZIONE BREVE', align='C', new_x='LMARGIN', new_y='NEXT')
    pdf.set_font('Helvetica', '', 8)
    pdf.set_text_color(90, 90, 90)
    pdf.cell(0, 4, 'Documento non-fattura — Operazione fuori campo IVA art. 1 c.2 DPR 633/1972', align='C', new_x='LMARGIN', new_y='NEXT')
    pdf.ln(4)
    pdf.set_draw_color(200, 200, 200)
    pdf.line(10, pdf.get_y(), 200, pdf.get_y())
    pdf.ln(4)

    # Receipt number + date
    pdf.set_text_color(0, 0, 0)
    pdf.set_font('Helvetica', 'B', 11)
    pdf.cell(95, 6, f"Ricevuta N. {receipt.receipt_number}", new_x='RIGHT', new_y='TOP')
    pdf.set_font('Helvetica', '', 9)
    pdf.cell(95, 6, f"Data emissione: {receipt.issue_date.strftime('%d/%m/%Y')}", align='R', new_x='LMARGIN', new_y='NEXT')
    pdf.set_font('Helvetica', '', 7)
    pdf.set_text_color(100, 100, 100)
    pdf.cell(0, 4, f"ID prenotazione #{receipt.reservation_id}  |  Emessa il {receipt.created_at.strftime('%d/%m/%Y %H:%M') if receipt.created_at else ''}", new_x='LMARGIN', new_y='NEXT')
    pdf.ln(3)

    # Emittente
    pdf.set_text_color(0, 0, 0)
    pdf.set_font('Helvetica', 'B', 9)
    pdf.set_fill_color(245, 245, 245)
    pdf.cell(0, 6, '  DATI EMITTENTE (Locatore)', fill=True, new_x='LMARGIN', new_y='NEXT')
    pdf.set_font('Helvetica', '', 8)
    pdf.cell(0, 4, f"Nome/Cognome: {receipt.host_full_name or '—'}", new_x='LMARGIN', new_y='NEXT')
    pdf.cell(0, 4, f"Codice Fiscale: {receipt.host_codice_fiscale or '—'}", new_x='LMARGIN', new_y='NEXT')
    pdf.cell(0, 4, f"Indirizzo struttura: {receipt.host_address or '—'}", new_x='LMARGIN', new_y='NEXT')
    pdf.cell(0, 4, f"CIN: {receipt.cin_code or '—'}   |   CIR: {receipt.cir_code or '—'}", new_x='LMARGIN', new_y='NEXT')
    pdf.ln(3)

    # Cliente
    pdf.set_font('Helvetica', 'B', 9)
    pdf.cell(0, 6, '  DATI OSPITE (Intestatario prenotazione)', fill=True, new_x='LMARGIN', new_y='NEXT')
    pdf.set_font('Helvetica', '', 8)
    pdf.cell(0, 4, f"Nome/Cognome: {receipt.guest_full_name or '—'}", new_x='LMARGIN', new_y='NEXT')
    # Indirizzo: prefer billing snapshot
    addr_parts = [p for p in [receipt.guest_residence_address, receipt.guest_residence_city, receipt.guest_residence_zip, receipt.guest_residence_country] if p]
    addr_line = ', '.join(addr_parts) if addr_parts else '— (indirizzo non fornito — integrare da Stripe/check-in)'
    pdf.cell(0, 4, f"Residenza: {addr_line}", new_x='LMARGIN', new_y='NEXT')
    pdf.cell(0, 4, f"Email: {receipt.guest_email or '—'}", new_x='LMARGIN', new_y='NEXT')
    if receipt.guest_codice_fiscale:
        pdf.cell(0, 4, f"Codice Fiscale: {receipt.guest_codice_fiscale}", new_x='LMARGIN', new_y='NEXT')
    else:
        doc_line = f"{receipt.guest_document_type or ''} {receipt.guest_document_number or ''}".strip()
        pdf.cell(0, 4, f"Documento: {doc_line or '— (CF/Passaporto da completare)'}", new_x='LMARGIN', new_y='NEXT')
    pdf.ln(3)

    # Soggiorno
    pdf.set_font('Helvetica', 'B', 9)
    pdf.cell(0, 6, '  DETTAGLIO SOGGIORNO', fill=True, new_x='LMARGIN', new_y='NEXT')
    pdf.set_font('Helvetica', '', 8)
    pdf.cell(0, 4, f"Check-in: {receipt.check_in.strftime('%d/%m/%Y') if receipt.check_in else '—'}   Check-out: {receipt.check_out.strftime('%d/%m/%Y') if receipt.check_out else '—'}   Notti: {receipt.nights or '—'}   Ospiti: {receipt.num_guests or '—'}", new_x='LMARGIN', new_y='NEXT')
    pdf.cell(0, 4, f"Indirizzo alloggio: {receipt.host_address or '—'}", new_x='LMARGIN', new_y='NEXT')
    pdf.ln(2)

    # Corrispettivi — tabella
    pdf.set_font('Helvetica', 'B', 8)
    col_w = [110, 30, 25, 25]
    headers = ['Descrizione', 'Q.tà', 'Unitario', 'Importo']
    for i, h in enumerate(headers):
        pdf.cell(col_w[i], 6, h, border=1, align='C', fill=True)
    pdf.ln()
    pdf.set_font('Helvetica', '', 8)
    # Riga soggiorno
    nightly = receipt.stay_amount / receipt.nights if receipt.nights else receipt.stay_amount
    pdf.cell(col_w[0], 6, 'Soggiorno (locazione breve)', border=1)
    pdf.cell(col_w[1], 6, f"{receipt.nights} notti x {receipt.num_guests} osp.", border=1, align='C')
    pdf.cell(col_w[2], 6, _money(nightly) if receipt.nights else '—', border=1, align='R')
    pdf.cell(col_w[3], 6, _money(receipt.stay_amount), border=1, align='R')
    pdf.ln()
    # Riga tassa soggiorno
    pdf.cell(col_w[0], 6, 'Contributo di Soggiorno Roma Capitale (incassato per conto del Comune)', border=1)
    pdf.cell(col_w[1], 6, f"{receipt.nights} x {receipt.num_guests}", border=1, align='C')
    # unit tax
    unit_tax = receipt.tourist_tax_amount / (receipt.nights * receipt.num_guests) if receipt.nights and receipt.num_guests and receipt.tourist_tax_amount else 0
    pdf.cell(col_w[2], 6, _money(unit_tax) if unit_tax else '—', border=1, align='R')
    pdf.cell(col_w[3], 6, _money(receipt.tourist_tax_amount), border=1, align='R')
    pdf.ln()
    # Totale
    pdf.set_font('Helvetica', 'B', 8)
    pdf.cell(col_w[0] + col_w[1] + col_w[2], 7, 'TOTALE CORRISPETTIVO (soggiorno + tassa)', border=1, align='R')
    pdf.cell(col_w[3], 7, _money(receipt.total_amount), border=1, align='R')
    pdf.ln(8)

    # Pagamento + Stripe refs
    pdf.set_font('Helvetica', '', 8)
    pay_label = {'stripe': 'Carta di Credito / Stripe', 'wire_transfer': 'Bonifico', 'cash': 'Contanti', 'n/a': '—'}.get(receipt.payment_method or '', receipt.payment_method or '—')
    pdf.cell(0, 4, f"Modalità pagamento: {pay_label} — {_money(receipt.total_amount)}", new_x='LMARGIN', new_y='NEXT')
    if receipt.stripe_payment_intent_id:
        pdf.set_font('Helvetica', '', 7)
        pdf.set_text_color(80, 80, 80)
        pdf.cell(0, 4, f"Stripe PaymentIntent: {receipt.stripe_payment_intent_id}", new_x='LMARGIN', new_y='NEXT')
    if receipt.stripe_charge_id:
        pdf.cell(0, 4, f"Stripe Charge: {receipt.stripe_charge_id}", new_x='LMARGIN', new_y='NEXT')
    if receipt.stripe_receipt_url:
        pdf.set_text_color(0, 70, 180)
        pdf.cell(0, 4, f"Ricevuta Stripe: {receipt.stripe_receipt_url}", link=receipt.stripe_receipt_url, new_x='LMARGIN', new_y='NEXT')
        pdf.set_text_color(0, 0, 0)
    pdf.ln(2)

    # Bollo
    pdf.set_text_color(0, 0, 0)
    pdf.set_font('Helvetica', 'B', 8)
    pdf.cell(0, 5, 'IMPOSTA DI BOLLO', new_x='LMARGIN', new_y='NEXT')
    pdf.set_font('Helvetica', '', 8)
    if receipt.bollo_required:
        pdf.cell(0, 4, f"Marca da bollo da EUR 2,00 obbligatoria (imponibile soggiorno { _money(receipt.stay_amount)} > EUR 77,47).", new_x='LMARGIN', new_y='NEXT')
        if receipt.bollo_id:
            pdf.cell(0, 4, f"Contrassegno telematico applicato sull'originale: {receipt.bollo_id} (14 cifre)", new_x='LMARGIN', new_y='NEXT')
        else:
            pdf.set_text_color(180, 0, 0)
            pdf.cell(0, 4, "Contrassegno: DA APPLICARE sull'originale cartaceo e riportare qui il codice a 14 cifre (art. 13 DPR 642/1972).", new_x='LMARGIN', new_y='NEXT')
            pdf.set_text_color(0, 0, 0)
        pdf.cell(0, 4, "Assolta in modo virtuale ove autorizzato; altrimenti marca fisica su copia conservata dal locatore.", new_x='LMARGIN', new_y='NEXT')
    else:
        pdf.cell(0, 4, f"Non dovuta (imponibile soggiorno { _money(receipt.stay_amount)} \u2264 EUR 77,47).", new_x='LMARGIN', new_y='NEXT')
    pdf.ln(3)

    # IVA exemption
    pdf.set_draw_color(200, 200, 200)
    pdf.line(10, pdf.get_y(), 200, pdf.get_y())
    pdf.ln(2)
    pdf.set_font('Helvetica', 'I', 7)
    pdf.multi_cell(0, 3.5, IVA_EXEMPTION_TEXT + "\nLocazione breve non imprenditoriale — fuori campo IVA. Ricevuta valida ai fini fiscali (Agenzia Entrate) e amministrativi (Roma Capitale). La tassa di soggiorno è incassata per conto del Comune e non costituisce reddito del locatore.", align='C')
    pdf.ln(4)

    # Footer
    pdf.set_font('Helvetica', '', 6)
    pdf.set_text_color(120, 120, 120)
    pdf.cell(0, 3, f"Documento generato automaticamente da lotto235garbatella.it  |  CIN {receipt.cin_code or ''}  CIR {receipt.cir_code or ''}  |  Conservare per 10 anni ai fini fiscali.", align='C', new_x='LMARGIN', new_y='NEXT')

    out = pdf.output(dest='S')
    if isinstance(out, (bytes, bytearray)):
        return bytes(out)
    return out.encode('latin-1')


def get_receipt_or_none(reservation_id: int) -> Optional[Receipt]:
    return Receipt.query.filter_by(reservation_id=reservation_id).first()

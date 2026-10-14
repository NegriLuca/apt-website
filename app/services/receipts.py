"""
Ricevuta fiscale italiana — servizio per generazione numerazione e PDF.

Valida per locazioni brevi non-imprenditoriali:
- numerazione progressiva annuale 01/2026, reset 01/01
- solo prenotazioni direct/stripe (sito)
- bollo 2€ se imponibile soggiorno > 77.47
- indirizzo guest preso da Stripe customer_details se disponibile
"""
import os
from datetime import date, datetime
from typing import Optional

from flask import current_app

from app import db
from app.models import Apartment, Receipt, Reservation

BOLLO_THRESHOLD = 77.47
BOLLO_AMOUNT = 2.00
# Percorso immagine marca da bollo — template globale
MARCA_BOLLO_IMAGE = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "static", "images", "marca_da_bollo.png"))
# Cartella per immagini marca per-ricevuta (sovrascrivono il template)
BOLLO_UPLOAD_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "static", "images", "bolli"))
IVA_EXEMPTION_TEXT = "Operazione fuori campo di applicazione dell'IVA ai sensi dell'art. 1, comma 2, D.P.R. 633/1972"


def get_bollo_image_path(receipt: "Receipt") -> Optional[str]:
    """Ritorna path assoluto immagine marca per questa ricevuta: per-ricevuta > template globale > None."""
    # per-ricevuta (DB)
    p = getattr(receipt, 'bollo_image_path', None)
    if p:
        # p è salvato come path relativo tipo static/images/bolli/bollo_12.png o assoluto
        abs_p = p if os.path.isabs(p) else os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", p))
        # anche se salvato relativo a project root
        if os.path.exists(abs_p):
            return abs_p
        # prova relativo a app root
        alt = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", p.replace("app/", "").replace("static/", "static/")))
        if os.path.exists(alt):
            return alt
        # fallback: cerca direttamente in BOLLO_UPLOAD_DIR per convenzione
        cand = os.path.join(BOLLO_UPLOAD_DIR, os.path.basename(p))
        if os.path.exists(cand):
            return cand
    # convention-based per id anche senza DB (retrocompat)
    if getattr(receipt, 'id', None):
        for ext in (".png", ".jpg", ".jpeg", ".webp"):
            cand = os.path.join(BOLLO_UPLOAD_DIR, f"bollo_{receipt.id}{ext}")
            if os.path.exists(cand):
                return cand
    if os.path.exists(MARCA_BOLLO_IMAGE):
        return MARCA_BOLLO_IMAGE
    return None


def save_bollo_image(file_storage, receipt: "Receipt") -> Optional[str]:
    """Salva upload marca (PNG/JPG/WEBP) per questa ricevuta, ritorna path relativo per DB."""
    if not file_storage or not getattr(file_storage, 'filename', ''):
        return None
    from werkzeug.utils import secure_filename
    filename = secure_filename(file_storage.filename)
    ext = os.path.splitext(filename)[1].lower()
    if ext not in (".png", ".jpg", ".jpeg", ".webp"):
        raise ValueError("Formato immagine non supportato — usa PNG/JPG/WEBP")
    # validazione leggera con Pillow
    try:
        from PIL import Image
        file_storage.stream.seek(0)
        im = Image.open(file_storage.stream)
        im.verify()
        file_storage.stream.seek(0)
    except Exception:
        # se Pillow non riesce, lascia passare — fpdf proverà comunque
        file_storage.stream.seek(0)
    os.makedirs(BOLLO_UPLOAD_DIR, exist_ok=True)
    # nome deterministico per id (sovrascrive precedente)
    dest_name = f"bollo_{receipt.id}{ext}"
    dest_abs = os.path.join(BOLLO_UPLOAD_DIR, dest_name)
    file_storage.save(dest_abs)
    # pulisci eventuali vecchi estensioni diverse
    for old_ext in (".png", ".jpg", ".jpeg", ".webp"):
        if old_ext != ext:
            old = os.path.join(BOLLO_UPLOAD_DIR, f"bollo_{receipt.id}{old_ext}")
            if os.path.exists(old):
                try:
                    os.remove(old)
                except Exception:
                    pass
    # path relativo rispetto a project root per DB (usato anche da template)
    rel = os.path.relpath(dest_abs, os.path.join(os.path.dirname(__file__), "..", ".."))
    # normalizza a forward slashes per url_for
    rel = rel.replace(os.sep, "/")
    # salva in DB
    receipt.bollo_image_path = rel
    db.session.commit()
    return rel


def is_direct_reservation(reservation: Reservation) -> bool:
    return (reservation.source or 'direct') in ('direct', 'stripe')


OTA_SOURCES = {'airbnb', 'booking', 'booking_com', 'vrbo'}

PLATFORM_LABELS = {
    'direct': 'Prenotazione diretta (sito)',
    'stripe': 'Prenotazione diretta (sito)',
    'airbnb': 'Airbnb',
    'booking': 'Booking.com',
    'booking_com': 'Booking.com',
    'vrbo': 'Vrbo',
}


def platform_label(source: Optional[str]) -> str:
    return PLATFORM_LABELS.get((source or 'direct').lower(), (source or 'direct').title())


def get_linked_earning(reservation: Reservation):
    """Return linked Earning row if any (by reservation_id or confirmation code)."""
    try:
        from app.models import Earning

        if getattr(reservation, 'id', None):
            e = Earning.query.filter_by(reservation_id=reservation.id).first()
            if e:
                return e
        code = (reservation.external_uid or '').strip()
        if code:
            e = Earning.query.filter_by(confirmation_code=code).first()
            if e:
                return e
    except Exception:
        pass
    return None


def get_external_ref(reservation: Reservation, earning=None) -> Optional[str]:
    """Best Booking number / Airbnb code for this reservation."""
    if earning is not None and getattr(earning, 'confirmation_code', None):
        return earning.confirmation_code
    linked = earning if earning is not None else get_linked_earning(reservation)
    if linked is not None and getattr(linked, 'confirmation_code', None):
        return linked.confirmation_code
    if getattr(reservation, 'external_uid', None):
        return reservation.external_uid
    return None


def suggest_ota_amounts(reservation: Reservation) -> dict:
    """Prefill helper for the admin form — gross paid by guest + net payout.

    Gross (lordo ospite) goes on the receipt; net (bank payout) is stored
    as platform_net for info only and never overwrites dashboard revenue.
    """
    earning = get_linked_earning(reservation)
    gross = 0.0
    net = None
    guest = None
    code = get_external_ref(reservation, earning)
    if earning is not None:
        gross = float(getattr(earning, 'gross_earnings', 0) or 0)
        net = float(getattr(earning, 'net', 0) or 0) or None
        if getattr(earning, 'guest_name', None):
            guest = earning.guest_name
    if not gross:
        gross = float(reservation.total_price or 0.0)
    return {'gross': round(gross, 2), 'net': round(net, 2) if net else None, 'guest': guest, 'code': code}


def _is_generic_ota_name(name: Optional[str]) -> bool:
    if not name:
        return True
    n = name.strip()
    if len(n) < 3:
        return True
    low = n.lower()
    return 'guest' in low or 'blocked' in low or 'external' in low


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


def create_or_get_receipt(
    reservation: Reservation,
    issue_date: Optional[date] = None,
    bollo_id: Optional[str] = None,
    stay_amount: Optional[float] = None,
    tourist_tax_amount: Optional[float] = None,
    guest_full_name: Optional[str] = None,
    payment_method: Optional[str] = None,
    external_ref: Optional[str] = None,
    charge_bollo: Optional[bool] = None,
    entity_type: Optional[str] = None,
    company_name: Optional[str] = None,
    company_vat: Optional[str] = None,
    company_address: Optional[str] = None,
) -> Receipt:
    """Create receipt for any reservation (direct + OTA) if not exists, else return existing.

    For direct stays amounts default to the reservation row (net stay + tourist tax).
    For OTA stays (Booking/Airbnb/VRBO) the receipt is for the GROSS paid by the
    guest — pass stay_amount explicitly (prefilled in admin from Earning.gross)
    because Reservation.total_price stores the NET bank payout for the dashboard
    and must NOT be overwritten.

    Bollo: direct stays auto-apply €2 when stay > 77.47 (charge_bollo defaults
    to True). OTA stays default to NO bollo surcharge (charge_bollo defaults
    to False) — the receipt only references the Booking/Airbnb stay + tourist
    tax, no €2 line. Pass charge_bollo=True to force it.

    Company: pass entity_type='company' + company_name + 11-digit company_vat
    to make the receipt out to a company (denominazione/P.IVA/sede) instead of
    a natural person. Still a fuori-campo-IVA ricevuta, not an e-fattura.
    """

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

    origin = (reservation.source or 'direct').lower()
    is_direct = origin in ('direct', 'stripe')
    earning = None if is_direct else get_linked_earning(reservation)

    # Importi: override manuale (form admin) > suggerimento OTA > riga prenotazione
    if stay_amount is not None:
        stay = round(float(stay_amount), 2)
    elif is_direct:
        stay = float(reservation.total_price or 0.0)
    else:
        sugg = suggest_ota_amounts(reservation)
        stay = sugg['gross'] or float(reservation.total_price or 0.0)
        if stay <= 0:
            raise ValueError("Inserisci l'importo lordo pagato dall'ospite (Prezzo Booking / Gross Airbnb) — non trovato in automatico")
    if tourist_tax_amount is not None:
        tourist_tax = round(float(tourist_tax_amount), 2)
    else:
        tourist_tax = float(reservation.tourist_tax_amount or 0.0)
    # Imponibile = soggiorno (senza tassa/bollo); totale pagato = stay + tassa [+ bollo se addebitato]
    # Nota direct: total_price in DB è netto; per OTA stay è il LORDO ospite
    # (Prezzo Booking / Gross Airbnb), mentre Reservation.total_price resta il
    # NETTO banca per la dashboard e non viene sovrascritto.

    if charge_bollo is None:
        charge_bollo = is_direct  # direct: auto si / OTA: no (solo causale + booking ref)
    bollo_required = bool(charge_bollo) and stay > BOLLO_THRESHOLD
    bollo_amount = BOLLO_AMOUNT if bollo_required else 0.0
    # ricalcola totale coerente
    total = round(stay + tourist_tax + bollo_amount, 2)

    apartment = Apartment.query.first()
    host = _snapshot_host(apartment)
    guest_snap = _guest_address_snapshot(reservation)

    if guest_full_name and guest_full_name.strip():
        guest_full = guest_full_name.strip()
    elif not is_direct and earning is not None and _is_generic_ota_name(reservation.guest_name) and getattr(earning, 'guest_name', None):
        guest_full = earning.guest_name
    else:
        guest_full = reservation.guest_full_name if hasattr(reservation, 'guest_full_name') else reservation.guest_name
    # Prefer guest_codice_fiscale, fallback to document number for foreign
    guest_cf = reservation.guest_codice_fiscale or ''

    if payment_method and payment_method.strip():
        pay_method = payment_method.strip()
    elif is_direct:
        pay_method = reservation.payment_method or 'stripe'
    else:
        # default OTA: piattaforma (es. booking_com) salvo override admin
        pay_method = reservation.payment_method if reservation.payment_method not in (None, '', 'n/a', 'automatic') else origin

    ext_ref = (external_ref or '').strip() or get_external_ref(reservation, earning)
    plat_net = None
    if earning is not None:
        try:
            plat_net = round(float(getattr(earning, 'net', 0) or 0), 2) or None
        except Exception:
            plat_net = None

    is_company = (entity_type or '').strip().lower() == 'company'
    comp_name = (company_name or '').strip() or None
    comp_vat_raw = ''.join(c for c in (company_vat or '') if c.isdigit())
    comp_addr = (company_address or '').strip() or None
    if is_company:
        if not comp_name:
            raise ValueError('Per intestazione azienda serve la denominazione / ragione sociale')
        if len(comp_vat_raw) != 11:
            raise ValueError('P.IVA azienda deve avere 11 cifre')
        if not comp_addr:
            raise ValueError("Per intestazione azienda serve la sede legale")

    receipt = Receipt(
        reservation_id=reservation.id,
        year=year,
        sequence=sequence,
        receipt_number=receipt_number,
        issue_date=issue_date or date.today(),
        stay_amount=round(stay, 2),
        tourist_tax_amount=round(tourist_tax, 2),
        total_amount=total,
        payment_method=pay_method,
        stripe_payment_intent_id=reservation.stripe_payment_intent_id,
        stripe_charge_id=reservation.stripe_charge_id,
        stripe_receipt_url=reservation.stripe_receipt_url,
        origin_source=origin,
        external_ref=ext_ref,
        platform_net=plat_net,
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
        entity_type='company' if is_company else 'person',
        company_name=comp_name,
        company_vat=comp_vat_raw or None,
        company_address=comp_addr,
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

        # ── CF / Passaporto da Stripe custom_fields (fatturazione)
        # Stripe invia: custom_fields=[{key:tipo_documento_fiscale, dropdown:{value:cf/passport/...}}, {key:codice_fiscale_documento, text:{value:RSS...}}]
        # Gestisce anche fallback solo testo (vecchio) e CF autodetect
        try:
            cf_fields = session_obj.get('custom_fields') or []
            # alcuni webhook mettono in collected_information
            if not cf_fields and session_obj.get('collected_information'):
                cf_fields = session_obj.get('collected_information', {}).get('custom_fields') or []
            tipo = None
            codice = None
            for f in cf_fields:
                k = f.get('key')
                if k == 'tipo_documento_fiscale':
                    tipo = (f.get('dropdown') or {}).get('value') or f.get('value')
                    if isinstance(tipo, str):
                        tipo = tipo.strip().lower()
                elif k == 'codice_fiscale_documento':
                    codice = (f.get('text') or {}).get('value') or f.get('value') or ''
                    if isinstance(codice, str):
                        codice = codice.strip()
            # normalizza codice
            if codice:
                codice_clean = codice.strip().upper()
                # CF IT: 16 alfanumerici
                is_cf_format = len(''.join(c for c in codice_clean if c.isalnum())) == 16 and codice_clean.replace(' ', '').isalnum()
                if (tipo == 'cf' or (not tipo and is_cf_format)):
                    # salva CF
                    cf_val = ''.join(c for c in codice_clean if c.isalnum())[:16]
                    if len(cf_val) == 16 and cf_val != (reservation.guest_codice_fiscale or ''):
                        reservation.guest_codice_fiscale = cf_val
                        # se era documento straniero prima, pulisci per coerenza IT
                        # mantieni document_number solo se già presente e diverso — non sovrascrivere
                        updated = True
                elif codice_clean:
                    # straniero: tipo mappato su guest_document_type
                    type_map = {'passport': 'passport', 'id_card': 'id_card', 'other': 'id_card', 'cf': 'passport'}
                    doc_type = type_map.get(tipo or 'passport', 'passport')
                    if not reservation.guest_codice_fiscale or tipo != 'cf':
                        # non sovrascrivere CF se già valido
                        pass
                    if codice_clean != (reservation.guest_document_number or ''):
                        reservation.guest_document_type = doc_type
                        reservation.guest_document_number = codice_clean[:50]
                        updated = True
                    # se CF era vuoto e tipo non cf, assicurati CF resti vuoto
            elif tipo:
                # solo tipo senza codice — niente da fare
                pass
        except Exception as cf_e:
            current_app.logger.debug(f"CF parse skip for res#{reservation.id}: {cf_e}")

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

def _causale_dates(check_in, check_out) -> str:
    """'dal 23 al 24 settembre 2026' style range for the causale line."""
    try:
        import locale

        months = ['', 'gennaio', 'febbraio', 'marzo', 'aprile', 'maggio', 'giugno', 'luglio', 'agosto', 'settembre', 'ottobre', 'novembre', 'dicembre']
        if not check_in or not check_out:
            return ''
        if check_in.month == check_out.month and check_in.year == check_out.year:
            return f"dal {check_in.day} al {check_out.day} {months[check_out.month]} {check_out.year}"
        return f"dal {check_in.day} {months[check_in.month]} {check_in.year} al {check_out.day} {months[check_out.month]} {check_out.year}"
    except Exception:
        return ''


def build_causale_text(receipt: "Receipt") -> str:
    """Causale line: 'Saldo per locazione breve turistica ... (Prenotazione Booking n. X)'."""
    dates = _causale_dates(receipt.check_in, receipt.check_out)
    place = 'Roma, Garbatella'
    base = f"Saldo per locazione breve turistica dell'immobile situato a {place}"
    if dates:
        base += f", relativo al soggiorno {dates}"
    src = (getattr(receipt, 'origin_source', None) or '').lower()
    ref = getattr(receipt, 'external_ref', None) or ''
    if src in ('booking', 'booking_com') and ref:
        base += f" (Prenotazione Booking n. {ref})"
    elif src == 'airbnb' and ref:
        base += f" (Prenotazione Airbnb n. {ref})"
    elif src == 'vrbo' and ref:
        base += f" (Prenotazione Vrbo n. {ref})"
    elif ref and src not in ('direct', 'stripe'):
        base += f" (Rif. piattaforma {ref})"
    return base + '.'


def generate_receipt_pdf_bytes(receipt: Receipt) -> bytes:
    """Generate PDF bytes for a receipt using fpdf2."""
    from fpdf import FPDF

    pdf = FPDF(format='A4')
    pdf.set_auto_page_break(auto=True, margin=18)
    pdf.add_page()

    # ── Marca da bollo stamp — solo se dovuta (>77.47), sempre visibile quando required
    # Top-right, con contenuto spostato in basso per non coprire testo
    # Per-ricevuta (foto tabaccaio) > template globale > fallback disegnato
    if getattr(receipt, 'bollo_required', False):
        try:
            bollo_img = get_bollo_image_path(receipt)
            if bollo_img and os.path.exists(bollo_img):
                pdf.image(bollo_img, x=168, y=6, w=30)
                # identificativo sotto l'immagine (14 cifre)
                id_txt = receipt.bollo_id if receipt.bollo_id else '--- DA INSERIRE ---'
                pdf.set_xy(168, 26)
                pdf.set_font('Helvetica', '', 6)
                pdf.set_text_color(0, 0, 0)
                pdf.cell(30, 3, id_txt, align='C')
                pdf.set_text_color(0, 0, 0)
            else:
                # fallback disegnato se manca file — garantisce sempre visibile
                pdf.set_draw_color(180, 150, 0)
                pdf.set_fill_color(255, 248, 200)
                pdf.set_line_width(0.4)
                pdf.rect(168, 6, 30, 22, style='DF')
                pdf.set_xy(168, 8)
                pdf.set_font('Helvetica', 'B', 6)
                pdf.set_text_color(120, 90, 0)
                pdf.cell(30, 3, 'MARCA DA BOLLO', align='C')
                pdf.set_xy(168, 11.5)
                pdf.set_font('Helvetica', 'B', 7)
                pdf.set_text_color(0, 0, 0)
                pdf.cell(30, 3, 'EUR 2,00', align='C')
                pdf.set_xy(168, 15.5)
                pdf.set_font('Helvetica', '', 5.5)
                pdf.cell(30, 3, receipt.bollo_id if receipt.bollo_id else 'DA INSERIRE', align='C')
                pdf.set_xy(168, 19.5)
                pdf.set_font('Helvetica', '', 5)
                pdf.set_text_color(100, 100, 100)
                pdf.cell(30, 3, 'Identificativo 14 cifre', align='C')
                pdf.set_text_color(0, 0, 0)
                pdf.set_line_width(0.2)
                pdf.set_draw_color(200, 200, 200)
            # sposta tutto il contenuto sotto il timbro per non sovrapporre
            pdf.set_y(34)
            pdf.set_x(10)
        except Exception:
            import logging as _lg
            _lg.getLogger(__name__).warning("Marca da bollo stamp failed", exc_info=True)
            pdf.set_y(10)
    else:
        pdf.set_y(10)

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
    id_line = f"ID prenotazione #{receipt.reservation_id}  |  Emessa il {receipt.created_at.strftime('%d/%m/%Y %H:%M') if receipt.created_at else ''}"
    pdf.cell(0, 4, id_line, new_x='LMARGIN', new_y='NEXT')
    # Riferimento piattaforma (Booking N. / Airbnb code) — richiesto per ricevute OTA
    _src = (getattr(receipt, 'origin_source', None) or '').lower()
    _ref = getattr(receipt, 'external_ref', None) or ''
    if _ref:
        pdf.set_font('Helvetica', 'B', 7)
        pdf.set_text_color(0, 0, 0)
        if _src in ('booking', 'booking_com'):
            pdf.cell(0, 4, f"Prenotazione Booking n. {_ref}  (ID interno #{receipt.reservation_id})", new_x='LMARGIN', new_y='NEXT')
        elif _src == 'airbnb':
            pdf.cell(0, 4, f"Prenotazione Airbnb n. {_ref}  (ID interno #{receipt.reservation_id})", new_x='LMARGIN', new_y='NEXT')
        elif _src and _src not in ('direct', 'stripe'):
            pdf.cell(0, 4, f"Riferimento {platform_label(_src)}: {_ref}  (ID interno #{receipt.reservation_id})", new_x='LMARGIN', new_y='NEXT')
        pdf.set_font('Helvetica', '', 7)
        pdf.set_text_color(100, 100, 100)
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

    # Intestatario: azienda oppure persona fisica
    _is_company = (getattr(receipt, 'entity_type', None) or 'person') == 'company'
    if _is_company:
        pdf.set_font('Helvetica', 'B', 9)
        pdf.cell(0, 6, '  DATI INTESTATARIO (Azienda)', fill=True, new_x='LMARGIN', new_y='NEXT')
        pdf.set_font('Helvetica', '', 8)
        pdf.cell(0, 4, f"Denominazione: {receipt.company_name or '—'}", new_x='LMARGIN', new_y='NEXT')
        pdf.cell(0, 4, f"P.IVA: {receipt.company_vat or '—'}", new_x='LMARGIN', new_y='NEXT')
        pdf.cell(0, 4, f"Sede legale: {receipt.company_address or '—'}", new_x='LMARGIN', new_y='NEXT')
        pdf.cell(0, 4, f"Email: {receipt.guest_email or '—'}", new_x='LMARGIN', new_y='NEXT')
        pdf.set_font('Helvetica', '', 7)
        pdf.set_text_color(90, 90, 90)
        pdf.cell(0, 4, f"Ospite soggiornante: {receipt.guest_full_name or '—'}", new_x='LMARGIN', new_y='NEXT')
        pdf.set_text_color(0, 0, 0)
    else:
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
    # Causale estesa (stile richiesto: Saldo per locazione breve ... Prenotazione Booking n. X)
    pdf.set_font('Helvetica', 'I', 7.5)
    pdf.set_text_color(60, 60, 60)
    pdf.multi_cell(0, 3.8, f"Causale: {build_causale_text(receipt)}")
    pdf.set_text_color(0, 0, 0)
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
    # Riga tassa soggiorno — on top, non reddito
    pdf.cell(col_w[0], 6, 'Contributo di Soggiorno Roma Capitale (incassato per conto del Comune)', border=1)
    pdf.cell(col_w[1], 6, f"{receipt.nights} x {receipt.num_guests}", border=1, align='C')
    unit_tax = receipt.tourist_tax_amount / (receipt.nights * receipt.num_guests) if receipt.nights and receipt.num_guests and receipt.tourist_tax_amount else 0
    pdf.cell(col_w[2], 6, _money(unit_tax) if unit_tax else '—', border=1, align='R')
    pdf.cell(col_w[3], 6, _money(receipt.tourist_tax_amount), border=1, align='R')
    pdf.ln()
    # Riga bollo 2€ se dovuto — incluso nel totale pagato da sito (fuori reddito)
    if receipt.bollo_required:
        pdf.cell(col_w[0], 6, 'Imposta di bollo (art.13 DPR 642/1972)', border=1)
        pdf.cell(col_w[1], 6, '1', border=1, align='C')
        pdf.cell(col_w[2], 6, _money(2.00), border=1, align='R')
        pdf.cell(col_w[3], 6, _money(2.00), border=1, align='R')
        pdf.ln()
    # Totale pagato dal sito = soggiorno (imponibile cedolare) + tassa + bollo
    pdf.set_font('Helvetica', 'B', 8)
    label_tot = 'TOTALE PAGATO (soggiorno + tassa + bollo)' if receipt.bollo_required else 'TOTALE PAGATO (soggiorno + tassa)'
    pdf.cell(col_w[0] + col_w[1] + col_w[2], 7, label_tot, border=1, align='R')
    pdf.cell(col_w[3], 7, _money(receipt.total_amount), border=1, align='R')
    pdf.ln(8)

    # Pagamento + Stripe refs
    pdf.set_font('Helvetica', '', 8)
    _pay_raw = receipt.payment_method or ''
    _pay_map = {
        'stripe': 'Carta di Credito / Stripe',
        'wire_transfer': 'Bonifico',
        'cash': 'Contanti',
        'n/a': '—',
        'booking': 'Booking.com (tramite piattaforma)',
        'booking_com': 'Booking.com (tramite piattaforma)',
        'airbnb': 'Airbnb (tramite piattaforma)',
        'vrbo': 'Vrbo (tramite piattaforma)',
    }
    pay_label = _pay_map.get(_pay_raw, _pay_raw or '—')
    pdf.cell(0, 4, f"Modalità pagamento: {pay_label} — {_money(receipt.total_amount)}", new_x='LMARGIN', new_y='NEXT')
    # Nota trasparenza OTA: netto banca vs lordo ospite (non in totale)
    _pnet = getattr(receipt, 'platform_net', None)
    if _pnet and _src not in ('direct', 'stripe', '') and abs(float(_pnet) - float(receipt.stay_amount or 0)) > 0.01:
        pdf.set_font('Helvetica', '', 6.5)
        pdf.set_text_color(100, 100, 100)
        pdf.cell(0, 3.5, f"Nota: importo incassato tramite {platform_label(_src)} al netto di commissioni = {_money(_pnet)} (vedi estratto piattaforma).", new_x='LMARGIN', new_y='NEXT')
        pdf.set_text_color(0, 0, 0)
        pdf.set_font('Helvetica', '', 8)
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

    # Bollo — sempre visibile quando dovuto (>77.47), con identificativo manuale
    if receipt.bollo_required:
        pdf.set_text_color(0, 0, 0)
        pdf.set_font('Helvetica', '', 7)
        id_display = receipt.bollo_id if receipt.bollo_id else 'DA INSERIRE (inserisci identificativo 14 cifre in /admin/receipts)'
        pdf.cell(0, 4, f"Imposta di bollo 2,00 EUR assolta — Contrassegno: {id_display}", new_x='LMARGIN', new_y='NEXT')
        pdf.set_font('Helvetica', '', 6)
        pdf.set_text_color(100, 100, 100)
        pdf.cell(0, 3, "Marca da bollo apposta sull'originale — identificativo univoco 14 cifre (art.13 DPR 642/1972).", new_x='LMARGIN', new_y='NEXT')
        pdf.set_text_color(0, 0, 0)
        pdf.ln(2)
    else:
        pdf.ln(1)

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

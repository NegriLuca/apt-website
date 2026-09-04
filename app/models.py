from datetime import date, datetime
from typing import Any, Optional

from flask_login import UserMixin

from app import bcrypt, db, login_manager


@login_manager.user_loader
def load_user(user_id: int) -> Optional['User']:
    return User.query.get(int(user_id))


class User(db.Model, UserMixin):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(20), unique=True, nullable=False)
    password = db.Column(db.String(128), nullable=False)  # stores bcrypt hash
    is_admin = db.Column(db.Boolean, default=False)

    def set_password(self, raw_password: str) -> None:
        """Hash and store a new password."""
        self.password = bcrypt.generate_password_hash(raw_password).decode('utf-8')

    def check_password(self, raw_password: str) -> bool:
        """Return True if raw_password matches the stored hash."""
        return bcrypt.check_password_hash(self.password, raw_password)


class Apartment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    price_per_night = db.Column(db.Float, nullable=False)
    image_file = db.Column(db.String(100), nullable=False, default='default.jpg')

    # Italian compliance fields
    cin_code = db.Column(db.String(50), nullable=True, comment='Codice Identificativo Nazionale')
    cir_code = db.Column(db.String(50), nullable=True, comment='Codice Identificativo Regionale Lazio')
    tourist_tax_category = db.Column(db.String(20), nullable=True, default='CAV', comment='CAV, BB, etc.')
    tourist_tax_rate = db.Column(db.Float, nullable=True, default=6.00, comment='Euro per night per person')
    max_guests = db.Column(db.Integer, nullable=True, default=4)

    # Ricevuta / Fattura — Dati emittente (proprietario)
    host_full_name = db.Column(db.String(150), nullable=True, comment='Nome e Cognome proprietario/gestore')
    host_codice_fiscale = db.Column(db.String(20), nullable=True, comment='Codice Fiscale proprietario')
    host_address = db.Column(db.String(250), nullable=True, comment='Indirizzo struttura (Via, CAP, Roma)')
    host_vat_mode = db.Column(db.String(50), nullable=True, default='fuori_campo_iva', comment='Regime fiscale')

    # Questura configuration
    questura_protocol = db.Column(db.String(50), nullable=True, comment='Protocollo Questura per AlloggiatiWeb')
    questura_ip_whitelisted = db.Column(db.Boolean, default=False)

    # Smart Access Configuration (Shelly Gate + Nuki Door)
    # Shelly Mini 1 Gen 4 (Gate)
    shelly_enabled = db.Column(db.Boolean, default=False)
    shelly_host = db.Column(
        db.String(100), nullable=True, comment='Shelly IP or hostname (e.g., 192.168.1.50 or shelly-gate.local)'
    )
    shelly_auth_key = db.Column(db.String(100), nullable=True, comment='Shelly Gen4 auth key (if auth enabled)')
    shelly_relay_channel = db.Column(db.Integer, default=0, comment='Relay channel (0 for Shelly 1 Mini)')
    shelly_pulse_duration = db.Column(db.Integer, default=3, comment='Gate pulse duration in seconds')

    # Boiler Shelly (hot water) — second Shelly on same cloud account
    boiler_shelly_enabled = db.Column(db.Boolean, default=False, comment='Enable boiler auto ON/OFF')
    boiler_shelly_device_id = db.Column(db.String(100), nullable=True, comment='Shelly device ID for boiler (e.g., 206ef104b850)')
    boiler_shelly_channel = db.Column(db.Integer, default=0, comment='Relay channel for boiler (0 for Shelly 1 Mini)')
    boiler_shelly_host = db.Column(db.String(100), nullable=True, comment='Optional fallback host/IP for boiler Shelly (local mode)')

    # Nuki Smart Lock Ultra (Apartment Door)
    nuki_enabled = db.Column(db.Boolean, default=False)
    nuki_show_door_button = db.Column(db.Boolean, default=True, comment='Show the door button on the guest page (else guests use the keypad only)')
    nuki_smartlock_id = db.Column(db.String(50), nullable=True, comment='Nuki Smart Lock ID (decimal)')
    nuki_web_token = db.Column(db.String(200), nullable=True, comment='Nuki Web API token (Bearer)')
    nuki_web_base_url = db.Column(db.String(100), default='https://api.nuki.io', comment='Nuki Web API base URL')
    nuki_unlock_action = db.Column(db.String(20), default='unlock', comment='unlock or unlatch (open door)')

    # WhatsApp Contact
    whatsapp_number = db.Column(
        db.String(30), nullable=True, comment='WhatsApp number in international format (e.g., 393000000000)'
    )
    whatsapp_default_message = db.Column(db.Text, nullable=True, comment='Default pre-filled message for WhatsApp')

    # Trust Badges & Reviews
    # Review Platforms
    booking_review_score = db.Column(db.Float, nullable=True, comment='Booking.com review score (0-10)')
    booking_review_count = db.Column(db.Integer, nullable=True, comment='Booking.com number of reviews')
    booking_property_id = db.Column(db.String(50), nullable=True, comment='Booking.com property ID for widget')

    airbnb_review_score = db.Column(db.Float, nullable=True, comment='Airbnb review score (0-5)')
    airbnb_review_count = db.Column(db.Integer, nullable=True, comment='Airbnb number of reviews')
    airbnb_listing_id = db.Column(db.String(50), nullable=True, comment='Airbnb listing ID')

    google_review_score = db.Column(db.Float, nullable=True, comment='Google reviews score (0-5)')
    google_review_count = db.Column(db.Integer, nullable=True, comment='Google reviews count')
    google_place_id = db.Column(db.String(100), nullable=True, comment='Google Places ID')

    tripadvisor_review_score = db.Column(db.Float, nullable=True, comment='TripAdvisor score (0-5)')
    tripadvisor_review_count = db.Column(db.Integer, nullable=True, comment='TripAdvisor review count')
    tripadvisor_location_id = db.Column(db.String(50), nullable=True, comment='TripAdvisor location ID')

    vrbo_review_score = db.Column(db.Float, nullable=True, comment='VRBO/HomeAway score (0-5)')
    vrbo_review_count = db.Column(db.Integer, nullable=True, comment='VRBO review count')
    vrbo_listing_id = db.Column(db.String(50), nullable=True, comment='VRBO listing ID')

    # Payment & Security Badges
    stripe_verified = db.Column(db.Boolean, default=False, comment='Show Stripe verified badge')
    ssl_certified = db.Column(db.Boolean, default=True, comment='Show SSL/Secure badge')
    gdpr_compliant = db.Column(db.Boolean, default=True, comment='Show GDPR compliant badge')
    pci_compliant = db.Column(db.Boolean, default=False, comment='Show PCI DSS compliant badge')

    # Custom Trust Badges (upload images)
    custom_badge_1_image = db.Column(db.String(200), nullable=True, comment='Custom badge 1 image filename')
    custom_badge_1_link = db.Column(db.String(300), nullable=True, comment='Custom badge 1 link URL')
    custom_badge_1_alt = db.Column(db.String(100), nullable=True, comment='Custom badge 1 alt text')

    custom_badge_2_image = db.Column(db.String(200), nullable=True, comment='Custom badge 2 image filename')
    custom_badge_2_link = db.Column(db.String(300), nullable=True, comment='Custom badge 2 link URL')
    custom_badge_2_alt = db.Column(db.String(100), nullable=True, comment='Custom badge 2 alt text')

    custom_badge_3_image = db.Column(db.String(200), nullable=True, comment='Custom badge 3 image filename')
    custom_badge_3_link = db.Column(db.String(300), nullable=True, comment='Custom badge 3 link URL')
    custom_badge_3_alt = db.Column(db.String(100), nullable=True, comment='Custom badge 3 alt text')

    # Display Settings
    show_reviews_in_footer = db.Column(db.Boolean, default=True, comment='Show review badges in footer')
    show_reviews_on_homepage = db.Column(db.Boolean, default=True, comment='Show review badges on homepage')
    show_reviews_on_booking = db.Column(db.Boolean, default=True, comment='Show review badges on booking page')
    show_payment_badges_in_footer = db.Column(db.Boolean, default=True, comment='Show payment badges in footer')
    show_payment_badges_on_checkout = db.Column(db.Boolean, default=True, comment='Show payment badges on checkout')

    # Official Widget Embeds (JavaScript snippets)
    booking_widget_js = db.Column(db.Text, nullable=True, comment='Booking.com official widget embed JS')
    airbnb_widget_js = db.Column(db.Text, nullable=True, comment='Airbnb official widget embed JS')
    google_widget_js = db.Column(db.Text, nullable=True, comment='Google Reviews widget embed JS')
    trustpilot_widget_js = db.Column(db.Text, nullable=True, comment='Trustpilot widget embed JS')

    # Guest Wi-Fi
    wifi_ssid = db.Column(db.String(100), nullable=True, comment='Guest Wi-Fi network name (SSID)')
    wifi_password = db.Column(db.String(100), nullable=True, comment='Guest Wi-Fi password')
    wifi_security = db.Column(db.String(20), nullable=True, default='WPA', comment='WPA, WEP or nopass')
    wifi_band = db.Column(db.String(50), nullable=True, comment='Band label shown on the card (e.g. 2.4GHz & 5GHz)')
    wifi_hidden = db.Column(db.Boolean, default=False, comment='SSID is hidden / not broadcast')

    @property
    def wifi_configured(self) -> bool:
        return bool(self.wifi_ssid)

    def wifi_payload(self) -> str | None:
        """QR payload in the standard WIFI: format."""
        if not self.wifi_ssid:
            return None

        def _escape(value: str) -> str:
            return value.replace('\\', '\\\\').replace(';', '\\;').replace(',', '\\,').replace(':', '\\:').replace('"', '\\"')

        security = (self.wifi_security or 'WPA').upper()
        if security == 'NONE':
            security = 'nopass'
        parts = [
            'WIFI:',
            f'T:{security};',
            f'S:{_escape(self.wifi_ssid)};',
        ]
        if self.wifi_password:
            parts.append(f'P:{_escape(self.wifi_password)};')
        if self.wifi_hidden:
            parts.append('H:true;')
        parts.append(';')
        return ''.join(parts)

    def wifi_connect_uri(self) -> str | None:
        """Tap-to-connect URI. ``wifi:`` scheme is handled natively by Android;
        iOS ignores the scheme (browsers block it) so the QR stays the fallback."""
        payload = self.wifi_payload()
        if not payload:
            return None
        return 'wifi:' + payload[len('WIFI:'):]


class Reservation(db.Model):
    __tablename__ = 'reservation'

    id = db.Column(db.Integer, primary_key=True)
    guest_name = db.Column(db.String(100), nullable=False)
    guest_email = db.Column(db.String(120))  # nullable for external bookings
    check_in = db.Column(db.Date, nullable=False)
    check_out = db.Column(db.Date, nullable=False)
    num_guests = db.Column(db.Integer, nullable=False, default=1)
    num_adults = db.Column(db.Integer, nullable=True, comment='Number of adults (taxable for city tax)')
    num_children = db.Column(db.Integer, nullable=True, default=0, comment='Number of children aged 3-9 (exempt from city tax)')
    status = db.Column(db.String(20), nullable=False, default='pending')
    source = db.Column(db.String(20), default='direct')
    is_block = db.Column(db.Boolean, nullable=False, default=False, comment='True when imported from iCal as a calendar block (not a real reservation)')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    cancel_token = db.Column(db.String(128), unique=True, index=True)
    external_uid = db.Column(db.String(128), unique=True, index=True)
    coupon_code = db.Column(db.String(20), nullable=True)

    # New Fields for Payment Tracking
    total_price = db.Column(db.Float, nullable=False, default=0.0)
    payment_status = db.Column(db.String(20), default='unpaid')
    payment_method = db.Column(db.String(50), default='n/a')
    # ── Stripe ────────────────────────────────────────────────────────────────
    stripe_payment_intent_id = db.Column(db.String(128), unique=True, index=True)

    # Deposit / partial payment tracking
    amount_paid = db.Column(db.Float, nullable=True, default=0.0, comment='Amount actually charged so far')
    balance_payment_intent_id = db.Column(db.String(128), nullable=True, unique=True, index=True)
    balance_invoice_sent_at = db.Column(db.DateTime, nullable=True, comment='When the balance invoice reminder email was sent')

    # ── Italian Compliance (Questura Alloggiati) ─────────────────────────────
    guest_surname = db.Column(db.String(100), nullable=True)
    guest_first_name = db.Column(db.String(100), nullable=True)
    guest_birth_date = db.Column(db.Date, nullable=True)
    guest_birth_place = db.Column(db.String(100), nullable=True)
    guest_nationality = db.Column(db.String(50), nullable=True)
    guest_document_type = db.Column(db.String(20), nullable=True, comment='passport, id_card, driving_license')
    guest_document_number = db.Column(db.String(50), nullable=True)
    guest_document_expiry = db.Column(db.Date, nullable=True)
    guest_document_country = db.Column(db.String(3), nullable=True, comment='ISO 3166-1 alpha-3')
    guest_gender = db.Column(db.String(1), nullable=True, comment='M/F')
    companions = db.Column(db.JSON, nullable=True, comment='List of additional guest dicts (surname, first_name, birth_date, birth_place, nationality, gender, document_type, document_number, document_expiry, document_country)')

    # Guest self-service check-in
    checkin_token = db.Column(db.String(128), unique=True, index=True, nullable=True)
    checkin_completed_at = db.Column(db.DateTime, nullable=True)
    checkin_token_used = db.Column(db.Boolean, default=False)

    # Questura sync tracking
    questura_submitted_at = db.Column(db.DateTime, nullable=True)
    questura_response = db.Column(db.Text, nullable=True)
    questura_status = db.Column(db.String(20), nullable=True, comment='pending, sent, accepted, rejected')
    questura_error = db.Column(db.Text, nullable=True)

    # ROSS1000 (Regione Lazio) tracking
    ross1000_status = db.Column(db.String(20), nullable=True, comment='pending, accepted, rejected')
    ross1000_submitted_at = db.Column(db.DateTime, nullable=True)
    ross1000_response = db.Column(db.Text, nullable=True)
    ross1000_error = db.Column(db.Text, nullable=True)

    # Guest Access Token (for gate/door opening during stay)
    access_token = db.Column(db.String(128), unique=True, index=True, nullable=True)
    access_token_created = db.Column(db.DateTime, nullable=True)

    # Nuki Keypad 2 temporary code (valid same window as smart access)
    keypad_code = db.Column(db.String(6), nullable=True)
    keypad_auth_id = db.Column(db.String(64), nullable=True)
    keypad_created_at = db.Column(db.DateTime, nullable=True)

    # Smart access window overrides — hours only, dates stay fixed to check_in/check_out.
    # NULL means default 13:00 (kept for backward compat). Stored as HH:MM strings "13:00".
    access_checkin_time = db.Column(db.String(5), nullable=True, comment='HH:MM on check-in day, Rome time (default 13:00)')
    access_checkout_time = db.Column(db.String(5), nullable=True, comment='HH:MM on check-out day, Rome time (default 13:00)')

    # Tourist tax
    tourist_tax_amount = db.Column(db.Float, nullable=True, default=0.0)
    tourist_tax_paid = db.Column(db.Boolean, default=False)
    tourist_tax_excluded = db.Column(db.Boolean, default=False, comment='Exclude from tourist tax reports')

    # ── Ricevuta — Dati cliente supplementari (indirizzo / CF) ─────────────────
    guest_residence_address = db.Column(db.String(250), nullable=True, comment='Via / indirizzo di residenza')
    guest_residence_city = db.Column(db.String(100), nullable=True, comment='Città di residenza')
    guest_residence_zip = db.Column(db.String(20), nullable=True, comment='CAP / ZIP')
    guest_residence_country = db.Column(db.String(100), nullable=True, comment='Nazione di residenza (IT o estero)')
    guest_codice_fiscale = db.Column(db.String(20), nullable=True, comment='CF 16 char per ospiti IT')
    # Billing data captured from Stripe (customer_details.address)
    guest_billing_address_line1 = db.Column(db.String(250), nullable=True)
    guest_billing_address_line2 = db.Column(db.String(250), nullable=True)
    guest_billing_city = db.Column(db.String(100), nullable=True)
    guest_billing_postal_code = db.Column(db.String(20), nullable=True)
    guest_billing_country = db.Column(db.String(5), nullable=True, comment='ISO 2-letter from Stripe')
    guest_billing_state = db.Column(db.String(100), nullable=True)
    stripe_charge_id = db.Column(db.String(128), nullable=True, comment='Stripe charge / payment_intent expanded')
    stripe_receipt_url = db.Column(db.String(500), nullable=True, comment='Stripe hosted receipt URL')

    # Guest communication
    guest_city_tax_enabled = db.Column(
        db.Boolean, default=False, comment='Show the city tax Stripe payment option to this guest in check-in/messages'
    )

    __table_args__ = (
        db.CheckConstraint('check_out > check_in', name='ck_dates_valid'),
        db.CheckConstraint('num_guests >= 1 AND num_guests <= 4', name='ck_guests_range'),
        db.UniqueConstraint('external_uid', name='uq_reservation_external_uid'),
        db.UniqueConstraint('cancel_token', name='uq_reservation_cancel_token'),
        db.CheckConstraint("status IN ('pending', 'confirmed', 'cancelled')", name='ck_reservation_status'),
    )

    @property
    def nights(self) -> int:
        if self.check_out and self.check_in:
            return (self.check_out - self.check_in).days
        return 0

    @property
    def guest_full_name(self) -> str:
        """Return formatted full name for Questura"""
        if self.guest_surname and self.guest_first_name:
            return f'{self.guest_surname} {self.guest_first_name}'
        return self.guest_name

    def questura_ready(self) -> bool:
        """Check if all required fields for Questura are present"""
        required = [
            self.guest_surname,
            self.guest_first_name,
            self.guest_birth_date,
            self.guest_birth_place,
            self.guest_nationality,
            self.guest_document_type,
            self.guest_document_number,
            self.guest_document_expiry,
            self.guest_document_country,
            self.guest_gender,
        ]
        return all(required)

    # ── Smart access window helpers (hours configurable, dates fixed) ──────────

    def _parse_hhmm(self, value: str | None, default: str = '13:00') -> tuple[int, int]:
        """Parse HH:MM string, fallback to default 13:00 on bad input."""
        src = (value or default).strip()
        try:
            h_str, m_str = src.split(':')
            h, m = int(h_str), int(m_str)
            if 0 <= h <= 23 and 0 <= m <= 59:
                return h, m
        except Exception:
            pass
        return 13, 0

    def get_access_window(self):
        """Return (start, end) datetimes with Europe/Rome tz for this reservation.

        Hours come from access_checkin_time / access_checkout_time (HH:MM),
        dates from check_in / check_out. Defaults to 13:00→13:00 for backward compat.
        """
        try:
            from zoneinfo import ZoneInfo
            rome = ZoneInfo('Europe/Rome')
        except Exception:
            from datetime import timedelta, timezone
            m = date.today().month
            rome = timezone(timedelta(hours=2 if 3 <= m <= 10 else 1))
        sh, sm = self._parse_hhmm(getattr(self, 'access_checkin_time', None), '13:00')
        eh, em = self._parse_hhmm(getattr(self, 'access_checkout_time', None), '13:00')
        start = datetime(self.check_in.year, self.check_in.month, self.check_in.day, sh, sm, tzinfo=rome)
        end = datetime(self.check_out.year, self.check_out.month, self.check_out.day, eh, em, tzinfo=rome)
        return start, end

    def get_access_window_utc(self):
        """Same window converted to UTC (for Nuki API)."""
        from datetime import timezone as _tz
        start, end = self.get_access_window()
        return start.astimezone(_tz.utc), end.astimezone(_tz.utc)

    def access_window_display(self) -> str:
        """Human string e.g. '13:00 24/08 → 11:00 27/08 (Rome)' for UI/flash."""
        sh, sm = self._parse_hhmm(getattr(self, 'access_checkin_time', None), '13:00')
        eh, em = self._parse_hhmm(getattr(self, 'access_checkout_time', None), '13:00')
        return f"{sh:02d}:{sm:02d} {self.check_in.strftime('%d/%m/%Y')} → {eh:02d}:{em:02d} {self.check_out.strftime('%d/%m/%Y')} (Rome)"

    def is_access_valid(self) -> bool:
        """Check if now (Rome) is inside the access window."""
        try:
            from zoneinfo import ZoneInfo
            rome = ZoneInfo('Europe/Rome')
        except Exception:
            from datetime import timedelta, timezone
            m = date.today().month
            rome = timezone(timedelta(hours=2 if 3 <= m <= 10 else 1))
        now = datetime.now(rome)
        start, end = self.get_access_window()
        return start <= now <= end

    def generate_access_token(self) -> str:
        """Generate a new access token for the reservation"""
        import secrets

        self.access_token = secrets.token_urlsafe(32)
        self.access_token_created = datetime.utcnow()
        return self.access_token


class Earning(db.Model):
    """Persisted payout for a single confirmation code (Airbnb + Booking).

    One row per confirmation code (e.g. HMNYSZ9PEA). Stores the two CSV lines
    (Reservation + Tax Withholding) collapsed into one: gross, service, withholding,
    net = amount + withholding (amount already net of service). Linked to a
    Reservation via external_uid or guest+dates when possible.
    """

    __tablename__ = 'earnings'

    id = db.Column(db.Integer, primary_key=True)
    platform = db.Column(db.String(20), nullable=False, default='airbnb', comment='airbnb / booking / vrbo')
    confirmation_code = db.Column(db.String(64), nullable=False, index=True, comment='Airbnb confirmation code or Booking reservation id')
    guest_name = db.Column(db.String(120), nullable=True)
    listing = db.Column(db.String(200), nullable=True)
    start_date = db.Column(db.Date, nullable=True)
    end_date = db.Column(db.Date, nullable=True)
    payout_date = db.Column(db.Date, nullable=True, comment='Date column = payout date')
    booking_date = db.Column(db.Date, nullable=True)
    nights = db.Column(db.Integer, nullable=True)
    currency = db.Column(db.String(10), nullable=False, default='EUR')
    amount = db.Column(db.Float, nullable=False, default=0.0, comment='CSV Amount (host payout before withholding, already net of service)')
    service_fee = db.Column(db.Float, nullable=False, default=0.0)
    cleaning_fee = db.Column(db.Float, nullable=False, default=0.0)
    gross_earnings = db.Column(db.Float, nullable=False, default=0.0)
    airbnb_tax = db.Column(db.Float, nullable=False, default=0.0, comment='Airbnb remitted tourist tax')
    withholding = db.Column(db.Float, nullable=False, default=0.0, comment='Negative cedolare secca, e.g. -68.88')
    net = db.Column(db.Float, nullable=False, default=0.0, comment='amount + withholding = bank payout')
    reservation_id = db.Column(db.Integer, db.ForeignKey('reservation.id'), nullable=True, index=True)
    raw_json = db.Column(db.JSON, nullable=True, comment='Original CSV row(s) collapsed')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    reservation = db.relationship('Reservation', backref=db.backref('earnings', lazy='dynamic'))

    __table_args__ = (
        db.UniqueConstraint('platform', 'confirmation_code', name='uq_earning_platform_code'),
    )

    def __repr__(self) -> str:
        return f'<Earning {self.platform}:{self.confirmation_code} {self.guest_name} {self.net:.2f}>'


class CleaningAccess(db.Model):
    """Shareable door/gate access window for staff (e.g. cleaning after check-out).

    ``starts_at`` / ``ends_at`` are naive datetimes in Europe/Rome (the timezone
    the admin configures them in). The public link stays available until the
    window is deactivated; the Nuki keypad code, when generated, physically
    expires on the device at ``ends_at``.
    """

    __tablename__ = 'cleaning_access'

    id = db.Column(db.Integer, primary_key=True)
    label = db.Column(db.String(100), nullable=False, comment='Short name, e.g. Cleaning 24/08')
    message = db.Column(db.Text, nullable=True, comment='Message shown on the share link')
    starts_at = db.Column(db.DateTime, nullable=True, comment='Access window start (Europe/Rome naive)')
    ends_at = db.Column(db.DateTime, nullable=True, comment='Access window end (Europe/Rome naive)')
    token = db.Column(db.String(128), unique=True, index=True, nullable=True)
    active = db.Column(db.Boolean, default=True, comment='Link responds; inactive links show disabled state')
    auto_generated = db.Column(db.Boolean, default=False, comment='Prefilled from a reservation gap')
    reservation_id = db.Column(db.Integer, db.ForeignKey('reservation.id'), nullable=True)
    keypad_code = db.Column(db.String(6), nullable=True)
    keypad_auth_id = db.Column(db.String(64), nullable=True)
    keypad_created_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    reservation = db.relationship('Reservation', backref=db.backref('cleaning_accesses', lazy='dynamic'))

    def generate_token(self) -> str:
        import secrets

        self.token = secrets.token_urlsafe(32)
        return self.token

    def is_now_active(self) -> bool:
        """True when the link is enabled (time window, if set, is ignored: the
        link stays usable until manually deactivated)."""
        return bool(self.active)

    def __repr__(self) -> str:
        return f'<CleaningAccess {self.id} {self.label!r}>'


class ICalFeed(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    source = db.Column(db.String(20))  # airbnb / vrbo / booking
    url = db.Column(db.Text, nullable=False)
    last_synced_at = db.Column(db.DateTime)
    active = db.Column(db.Boolean, default=True)


class Coupon(db.Model):
    __tablename__ = 'coupons'

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(20), unique=True, nullable=False, index=True)
    discount_type = db.Column(db.String(20), nullable=False, default='percentage')
    discount_value = db.Column(db.Float, nullable=False)
    active = db.Column(db.Boolean, default=True)

    def apply_discount(self, original_price: float) -> float:
        if not self.active:
            return original_price
        if self.discount_type == 'percentage':
            return max(0.0, original_price * (1 - (self.discount_value / 100.0)))
        elif self.discount_type == 'flat':
            return max(0.0, original_price - self.discount_value)
        return original_price


class Testimonial(db.Model):
    __tablename__ = 'testimonials'

    id = db.Column(db.Integer, primary_key=True)
    guest_name = db.Column(db.String(100), nullable=False)
    guest_location = db.Column(db.String(100), nullable=True)
    rating = db.Column(db.Integer, nullable=False, default=5)
    content = db.Column(db.Text, nullable=False)
    stay_date = db.Column(db.Date, nullable=True)
    is_published = db.Column(db.Boolean, default=True)
    is_featured = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    source = db.Column(db.String(50), default='direct')
    external_url = db.Column(db.String(255), nullable=True)

    def __repr__(self) -> str:
        return f'<Testimonial {self.guest_name} - {self.rating}★>'


class ComplianceConfig(db.Model):
    """Encrypted storage for API credentials and compliance settings"""

    __tablename__ = 'compliance_config'

    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(100), unique=True, nullable=False, index=True)
    value_encrypted = db.Column(db.Text, nullable=True)
    description = db.Column(db.Text, nullable=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Known keys:
    # - questura_wsdl_url
    # - questura_username
    # - questura_password
    # - questura_ws_key
    # - questura_cert_path
    # - questura_cert_password
    # - questura_protocol_number
    # - roma_tax_office_email
    # - ross1000_username
    # - ross1000_password

    def set_value(self, plain_value: str | None, encryption_key: Any | None = None) -> None:
        """Encrypt and store value"""
        if plain_value is None:
            self.value_encrypted = None
            return
        import base64
        import os

        from cryptography.fernet import Fernet

        if encryption_key is None:
            encryption_key = os.environ.get('COMPLIANCE_ENCRYPTION_KEY')
            if not encryption_key:
                from flask import current_app

                derived = current_app.config.get('SECRET_KEY', '')[:32].encode()
                encryption_key = base64.urlsafe_b64encode(derived.ljust(32, b'0')[:32])

        f = Fernet(encryption_key)
        self.value_encrypted = f.encrypt(plain_value.encode()).decode()

    def get_value(self, encryption_key: Any | None = None) -> str | None:
        """Decrypt and return value"""
        if not self.value_encrypted:
            return None
        import base64
        import os

        from cryptography.fernet import Fernet

        if encryption_key is None:
            encryption_key = os.environ.get('COMPLIANCE_ENCRYPTION_KEY')
            if not encryption_key:
                from flask import current_app

                derived = current_app.config.get('SECRET_KEY', '')[:32].encode()
                encryption_key = base64.urlsafe_b64encode(derived.ljust(32, b'0')[:32])

        f = Fernet(encryption_key)
        return f.decrypt(self.value_encrypted.encode()).decode()

    @classmethod
    def get(cls, key: str, default: str | None = None) -> str | None:
        """Get decrypted config value"""
        cfg = cls.query.filter_by(key=key).first()
        if cfg:
            try:
                return cfg.get_value()
            except Exception:
                return default
        return default

    @classmethod
    def set(cls, key: str, value: str | None, description: str | None = None) -> 'ComplianceConfig':
        """Set config value (encrypted)"""
        cfg = cls.query.filter_by(key=key).first()
        if not cfg:
            cfg = cls(key=key, description=description)
            db.session.add(cfg)
        cfg.set_value(value)
        if description:
            cfg.description = description
        db.session.commit()
        return cfg


class AuditLog(db.Model):
    """Audit trail for all admin actions"""

    __tablename__ = 'audit_log'

    id = db.Column(db.Integer, primary_key=True)
    action = db.Column(db.String(100), nullable=False, index=True)
    entity_type = db.Column(db.String(50), nullable=True)
    entity_id = db.Column(db.Integer, nullable=True)
    admin_user = db.Column(db.String(50), nullable=False)
    details = db.Column(db.Text, nullable=True)
    ip_address = db.Column(db.String(45), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class Notification(db.Model):
    """In-app notifications for the admin"""

    __tablename__ = 'notifications'

    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    message = db.Column(db.Text, nullable=True)
    category = db.Column(db.String(50), nullable=False, default='info')
    link = db.Column(db.String(300), nullable=True)
    is_read = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class QuesturaLog(db.Model):
    """Audit log for all Questura submissions"""

    __tablename__ = 'questura_log'

    id = db.Column(db.Integer, primary_key=True)
    reservation_id = db.Column(db.Integer, db.ForeignKey('reservation.id'), nullable=False, index=True)
    action = db.Column(db.String(20), nullable=False)  # submit, retry, manual
    request_xml = db.Column(db.Text, nullable=True)
    response_xml = db.Column(db.Text, nullable=True)
    status = db.Column(db.String(20), nullable=False)  # success, error, pending
    error_message = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    reservation = db.relationship('Reservation', backref=db.backref('questura_logs', lazy='dynamic'))


class Ross1000Log(db.Model):
    """Audit log for all ROSS1000 (Regione Lazio) submissions"""

    __tablename__ = 'ross1000_log'

    id = db.Column(db.Integer, primary_key=True)
    reservation_id = db.Column(db.Integer, db.ForeignKey('reservation.id'), nullable=False, index=True)
    action = db.Column(db.String(20), nullable=False)  # submit, retry, manual
    request_xml = db.Column(db.Text, nullable=True)
    response_xml = db.Column(db.Text, nullable=True)
    status = db.Column(db.String(20), nullable=False)  # success, error, pending
    error_message = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    reservation = db.relationship('Reservation', backref=db.backref('ross1000_logs', lazy='dynamic'))


class Receipt(db.Model):
    """Ricevuta fiscale non-fattura — numerazione progressiva annuale.

    Solo per prenotazioni direct/stripe (sito). Reset 01/01 ogni anno:
    01/2026, 02/2026 … 01/2027.
    Snapshot dei dati emittente/ospite al momento dell'emissione per
    validità legale anche se i dati cambiano in futuro.
    """

    __tablename__ = 'receipts'

    id = db.Column(db.Integer, primary_key=True)
    reservation_id = db.Column(db.Integer, db.ForeignKey('reservation.id'), nullable=False, unique=True, index=True)
    year = db.Column(db.Integer, nullable=False, index=True, comment='Anno solare della numerazione')
    sequence = db.Column(db.Integer, nullable=False, comment='Progressivo nel year (1..n)')
    receipt_number = db.Column(db.String(20), nullable=False, unique=True, index=True, comment='Es. 01/2026')
    issue_date = db.Column(db.Date, nullable=False, default=date.today)

    # Snapshot importi
    stay_amount = db.Column(db.Float, nullable=False, comment='Imponibile soggiorno (esclusa tassa)')
    tourist_tax_amount = db.Column(db.Float, nullable=False, default=0.0)
    total_amount = db.Column(db.Float, nullable=False, comment='stay + tourist tax')
    payment_method = db.Column(db.String(50), nullable=True, comment='stripe / wire_transfer snapshot')
    stripe_payment_intent_id = db.Column(db.String(128), nullable=True)
    stripe_charge_id = db.Column(db.String(128), nullable=True)
    stripe_receipt_url = db.Column(db.String(500), nullable=True)

    # Marca da bollo (obbligatoria se stay_amount > 77.47)
    bollo_required = db.Column(db.Boolean, default=False)
    bollo_amount = db.Column(db.Float, default=0.0, comment='2.00 se required altrimenti 0')
    bollo_id = db.Column(db.String(30), nullable=True, comment='14 cifre marca da bollo su copia cartacea')
    bollo_image_path = db.Column(db.String(300), nullable=True, comment='Path immagine marca (per-ricevuta, sovrascrive template globale)')

    # Snapshot emittente
    host_full_name = db.Column(db.String(150), nullable=True)
    host_codice_fiscale = db.Column(db.String(20), nullable=True)
    host_address = db.Column(db.String(250), nullable=True)
    cin_code = db.Column(db.String(50), nullable=True)
    cir_code = db.Column(db.String(50), nullable=True)

    # Snapshot ospite
    guest_full_name = db.Column(db.String(150), nullable=True)
    guest_email = db.Column(db.String(120), nullable=True)
    guest_residence_address = db.Column(db.String(250), nullable=True)
    guest_residence_city = db.Column(db.String(100), nullable=True)
    guest_residence_zip = db.Column(db.String(20), nullable=True)
    guest_residence_country = db.Column(db.String(100), nullable=True)
    guest_codice_fiscale = db.Column(db.String(20), nullable=True)
    guest_document_type = db.Column(db.String(20), nullable=True)
    guest_document_number = db.Column(db.String(50), nullable=True)

    # Dettaglio soggiorno snapshot
    check_in = db.Column(db.Date, nullable=True)
    check_out = db.Column(db.Date, nullable=True)
    nights = db.Column(db.Integer, nullable=True)
    num_guests = db.Column(db.Integer, nullable=True)

    is_confirmed = db.Column(db.Boolean, default=False, nullable=False, comment='Bloccata: non eliminabile')
    confirmed_at = db.Column(db.DateTime, nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    reservation = db.relationship('Reservation', backref=db.backref('receipt', uselist=False))

    __table_args__ = (
        db.UniqueConstraint('year', 'sequence', name='uq_receipt_year_sequence'),
        db.UniqueConstraint('receipt_number', name='uq_receipt_number'),
    )

    def __repr__(self) -> str:
        return f'<Receipt {self.receipt_number} res={self.reservation_id}>'

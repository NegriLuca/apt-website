from datetime import datetime
from app import db, login_manager, bcrypt
from flask_login import UserMixin

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

class User(db.Model, UserMixin):
    id         = db.Column(db.Integer, primary_key=True)
    username   = db.Column(db.String(20), unique=True, nullable=False)
    password   = db.Column(db.String(128), nullable=False)   # stores bcrypt hash
    is_admin   = db.Column(db.Boolean, default=False)

    def set_password(self, raw_password: str):
        """Hash and store a new password."""
        self.password = bcrypt.generate_password_hash(raw_password).decode('utf-8')

    def check_password(self, raw_password: str) -> bool:
        """Return True if raw_password matches the stored hash."""
        return bcrypt.check_password_hash(self.password, raw_password)

class Apartment(db.Model):
    id              = db.Column(db.Integer, primary_key=True)
    name            = db.Column(db.String(100), nullable=False)
    price_per_night = db.Column(db.Float, nullable=False)
    image_file      = db.Column(db.String(100), nullable=False, default='default.jpg')
    
    # Italian compliance fields
    cin_code        = db.Column(db.String(50), nullable=True, comment='Codice Identificativo Nazionale')
    cir_code        = db.Column(db.String(50), nullable=True, comment='Codice Identificativo Regionale Lazio')
    tourist_tax_category = db.Column(db.String(20), nullable=True, default='CAV', comment='CAV, BB, etc.')
    tourist_tax_rate = db.Column(db.Float, nullable=True, default=6.00, comment='Euro per night per person')
    max_guests      = db.Column(db.Integer, nullable=True, default=4)
    
    # Questura configuration
    questura_protocol = db.Column(db.String(50), nullable=True, comment='Protocollo Questura per AlloggiatiWeb')
    questura_ip_whitelisted = db.Column(db.Boolean, default=False)
    
    # Smart Access Configuration (Shelly Gate + Nuki Door)
    # Shelly Mini 1 Gen 4 (Gate)
    shelly_enabled = db.Column(db.Boolean, default=False)
    shelly_host = db.Column(db.String(100), nullable=True, comment='Shelly IP or hostname (e.g., 192.168.1.50 or shelly-gate.local)')
    shelly_auth_key = db.Column(db.String(100), nullable=True, comment='Shelly Gen4 auth key (if auth enabled)')
    shelly_relay_channel = db.Column(db.Integer, default=0, comment='Relay channel (0 for Shelly 1 Mini)')
    shelly_pulse_duration = db.Column(db.Integer, default=3, comment='Gate pulse duration in seconds')
    
    # Nuki Smart Lock Ultra (Apartment Door)
    nuki_enabled = db.Column(db.Boolean, default=False)
    nuki_smartlock_id = db.Column(db.String(50), nullable=True, comment='Nuki Smart Lock ID (decimal)')
    nuki_web_token = db.Column(db.String(200), nullable=True, comment='Nuki Web API token (Bearer)')
    nuki_web_base_url = db.Column(db.String(100), default='https://api.nuki.io', comment='Nuki Web API base URL')
    nuki_unlock_action = db.Column(db.String(20), default='unlock', comment='unlock or unlatch (open door)')


class Reservation(db.Model):
    __tablename__ = "reservation"

    id              = db.Column(db.Integer, primary_key=True)
    guest_name      = db.Column(db.String(100), nullable=False)
    guest_email     = db.Column(db.String(120))                # nullable for external bookings
    check_in        = db.Column(db.Date, nullable=False)
    check_out       = db.Column(db.Date, nullable=False)
    num_guests      = db.Column(db.Integer, nullable=False, default=1)
    status          = db.Column(db.String(20), nullable=False, default='pending')
    source          = db.Column(db.String(20), default="direct")
    created_at      = db.Column(db.DateTime, default=datetime.utcnow)
    cancel_token    = db.Column(db.String(128), unique=True, index=True)
    external_uid    = db.Column(db.String(128), unique=True, index=True)
    coupon_code     = db.Column(db.String(20), nullable=True)

    # New Fields for Payment Tracking
    total_price = db.Column(db.Float, nullable=False, default=0.0) 
    payment_status = db.Column(db.String(20), default='unpaid')
    payment_method = db.Column(db.String(50), default='n/a')
    # ── Stripe ────────────────────────────────────────────────────────────────
    stripe_payment_intent_id = db.Column(db.String(128), unique=True, index=True)

    # ── Italian Compliance (Questura Alloggiati) ─────────────────────────────
    guest_surname       = db.Column(db.String(100), nullable=True)
    guest_first_name    = db.Column(db.String(100), nullable=True)
    guest_birth_date    = db.Column(db.Date, nullable=True)
    guest_birth_place   = db.Column(db.String(100), nullable=True)
    guest_nationality   = db.Column(db.String(50), nullable=True)
    guest_document_type = db.Column(db.String(20), nullable=True, comment='passport, id_card, driving_license')
    guest_document_number = db.Column(db.String(50), nullable=True)
    guest_document_expiry = db.Column(db.Date, nullable=True)
    guest_document_country = db.Column(db.String(3), nullable=True, comment='ISO 3166-1 alpha-3')
    guest_gender        = db.Column(db.String(1), nullable=True, comment='M/F')
    
    # Guest self-service check-in
    checkin_token       = db.Column(db.String(128), unique=True, index=True, nullable=True)
    checkin_completed_at = db.Column(db.DateTime, nullable=True)
    checkin_token_used  = db.Column(db.Boolean, default=False)
    
    # Questura sync tracking
    questura_submitted_at = db.Column(db.DateTime, nullable=True)
    questura_response     = db.Column(db.Text, nullable=True)
    questura_status       = db.Column(db.String(20), nullable=True, comment='pending, sent, accepted, rejected')
    questura_error        = db.Column(db.Text, nullable=True)
    
    # Self-service check-in token (for guest-facing form)
    checkin_token         = db.Column(db.String(128), unique=True, index=True, nullable=True)
    checkin_completed_at  = db.Column(db.DateTime, nullable=True)
    checkin_token_used    = db.Column(db.Boolean, default=False)
    
    # Guest Access Token (for gate/door opening during stay)
    access_token          = db.Column(db.String(128), unique=True, index=True, nullable=True)
    access_token_created  = db.Column(db.DateTime, nullable=True)
    
    # Tourist tax
    tourist_tax_amount  = db.Column(db.Float, nullable=True, default=0.0)
    tourist_tax_paid    = db.Column(db.Boolean, default=False)

    __table_args__ = (
        db.CheckConstraint("check_out > check_in",      name="ck_dates_valid"),
        db.CheckConstraint("num_guests >= 1 AND num_guests <= 4", name="ck_guests_range"),
        db.UniqueConstraint("external_uid",  name="uq_reservation_external_uid"),
        db.UniqueConstraint("cancel_token",  name="uq_reservation_cancel_token"),
        db.CheckConstraint(
            "status IN ('pending', 'confirmed', 'cancelled')",
            name="ck_reservation_status"
        ),
    )

    @property
    def nights(self):
        if self.check_out and self.check_in:
            return (self.check_out - self.check_in).days
        return 0
    
    @property
    def guest_full_name(self):
        """Return formatted full name for Questura"""
        if self.guest_surname and self.guest_first_name:
            return f"{self.guest_surname} {self.guest_first_name}"
        return self.guest_name
    
    def questura_ready(self):
        """Check if all required fields for Questura are present"""
        required = [
            self.guest_surname, self.guest_first_name, self.guest_birth_date,
            self.guest_birth_place, self.guest_nationality,
            self.guest_document_type, self.guest_document_number,
            self.guest_document_expiry, self.guest_document_country,
            self.guest_gender
        ]
        return all(required)
    
    def is_access_valid(self):
        """Check if access token is valid for current date (during stay period)"""
        from datetime import date
        today = date.today()
        return self.check_in <= today <= self.check_out
    
    def generate_access_token(self):
        """Generate a new access token for the reservation"""
        import secrets
        self.access_token = secrets.token_urlsafe(32)
        self.access_token_created = datetime.utcnow()
        return self.access_token

class ICalFeed(db.Model):
    id            = db.Column(db.Integer, primary_key=True)
    source        = db.Column(db.String(20))   # airbnb / vrbo / booking
    url           = db.Column(db.Text, nullable=False)
    last_synced_at = db.Column(db.DateTime)
    active        = db.Column(db.Boolean, default=True)


class Coupon(db.Model):
    __tablename__ = 'coupons'
    
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(20), unique=True, nullable=False, index=True)
    discount_type = db.Column(db.String(20), nullable=False, default='percentage')
    discount_value = db.Column(db.Float, nullable=False)
    active = db.Column(db.Boolean, default=True)

    def apply_discount(self, original_price):
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

    def __repr__(self):
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
    # - questura_cert_path
    # - questura_cert_password
    # - questura_protocol_number
    # - roma_tax_office_email
    # - ross1000_username
    # - ross1000_password
    
    def set_value(self, plain_value, encryption_key=None):
        """Encrypt and store value"""
        if plain_value is None:
            self.value_encrypted = None
            return
        from cryptography.fernet import Fernet
        import base64
        import os
        
        if encryption_key is None:
            encryption_key = os.environ.get('COMPLIANCE_ENCRYPTION_KEY')
            if not encryption_key:
                # Generate from app secret as fallback
                from flask import current_app
                encryption_key = current_app.config.get('SECRET_KEY', '')[:32].encode()
                encryption_key = base64.urlsafe_b64encode(encryption_key.ljust(32, b'0')[:32])
        
        f = Fernet(encryption_key)
        self.value_encrypted = f.encrypt(plain_value.encode()).decode()
    
    def get_value(self, encryption_key=None):
        """Decrypt and return value"""
        if not self.value_encrypted:
            return None
        from cryptography.fernet import Fernet
        import base64
        import os
        
        if encryption_key is None:
            encryption_key = os.environ.get('COMPLIANCE_ENCRYPTION_KEY')
            if not encryption_key:
                from flask import current_app
                encryption_key = current_app.config.get('SECRET_KEY', '')[:32].encode()
                encryption_key = base64.urlsafe_b64encode(encryption_key.ljust(32, b'0')[:32])
        
        f = Fernet(encryption_key)
        return f.decrypt(self.value_encrypted.encode()).decode()
    
    @classmethod
    def get(cls, key, default=None):
        """Get decrypted config value"""
        cfg = cls.query.filter_by(key=key).first()
        if cfg:
            try:
                return cfg.get_value()
            except Exception:
                return default
        return default
    
    @classmethod
    def set(cls, key, value, description=None):
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
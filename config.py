import os
from dotenv import load_dotenv

import stripe
# Load .env file when running locally.
# In production (Heroku, Railway, VPS) set these as real env vars instead.
load_dotenv()

class Config:
    # ── Security ──────────────────────────────────────────────────────────────
    SECRET_KEY = os.environ.get('SECRET_KEY') or 'dev-only-change-in-production'
    WTF_CSRF_TIME_LIMIT = 86400  # 24 hours
    WTF_CSRF_SSL_STRICT = False

    # ── Database ──────────────────────────────────────────────────────────────
    SQLALCHEMY_DATABASE_URI = os.environ.get('DATABASE_URL', 'sqlite:///app.db')
    if SQLALCHEMY_DATABASE_URI and SQLALCHEMY_DATABASE_URI.startswith("postgres://"):
        SQLALCHEMY_DATABASE_URI = SQLALCHEMY_DATABASE_URI.replace("postgres://", "postgresql://", 1)
        
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    # ── Email ─────────────────────────────────────────────────────────────────
    MAIL_SERVER  = 'smtp-relay.brevo.com'
    MAIL_PORT    = 587
    MAIL_USE_TLS = False
    MAIL_USE_SSL = True
    TESTING      = False
    MAIL_USERNAME       = os.environ.get('MAIL_USERNAME')
    MAIL_PASSWORD       = os.environ.get('MAIL_PASSWORD')
    MAIL_DEFAULT_SENDER = os.environ.get('ADMIN_EMAIL')

    # Host e-mail for admin notifications
    ADMIN_EMAIL = os.environ.get('ADMIN_EMAIL') or os.environ.get('MAIL_USERNAME')

    # ── Stripe ────────────────────────────────────────────────────────────────
    STRIPE_SECRET_KEY      = os.environ.get('STRIPE_SECRET_KEY', '')
    STRIPE_PUBLISHABLE_KEY = os.environ.get('STRIPE_PUBLISHABLE_KEY', '')
    STRIPE_WEBHOOK_SECRET  = os.environ.get('STRIPE_WEBHOOK_SECRET', '')
    stripe.api_key = os.environ.get('STRIPE_SECRET_KEY', '')

    # ── iCal scheduler ────────────────────────────────────────────────────────
    ICAL_SYNC_INTERVAL_MINUTES = int(os.environ.get('ICAL_SYNC_INTERVAL_MINUTES', 30))

    # ── App public URL (used for Stripe redirect URLs) ────────────────────────
    BASE_URL = os.environ.get('BASE_URL', 'http://localhost:5001')

    # ── Analytics ─────────────────────────────────────────────────────────────
    GTM_ID = os.environ.get('GTM_ID')  # e.g., GTM-XXXXXXX
    GA4_ID = os.environ.get('GA4_ID')  # e.g., G-XXXXXXXXXX

    # ── Italian Compliance Codes (CIN/CIR) ────────────────────────────────────
    CIN_CODE = os.environ.get('CIN_CODE', 'IT058091C2TXZ44TA6')
    CIR_CODE = os.environ.get('CIR_CODE', '058091-LOC-19856')

    # ── Ricevuta / Fattura — Dati emittente ───────────────────────────────────
    HOST_FULL_NAME = os.environ.get('HOST_FULL_NAME', '')
    HOST_CODICE_FISCALE = os.environ.get('HOST_CODICE_FISCALE', '')
    HOST_ADDRESS = os.environ.get('HOST_ADDRESS', 'Via Lotto 235, 00153 Roma')
    HOST_VAT_MODE = os.environ.get('HOST_VAT_MODE', 'fuori_campo_iva')

    # ── ROSS1000 (Regione Lazio SOAP) ──────────────────────────────────────────
    ROSS1000_USERNAME = os.environ.get('ROSS1000_USERNAME', '')
    ROSS1000_PASSWORD = os.environ.get('ROSS1000_PASSWORD', '')
    ROSS1000_STRUCTURE_CODE = os.environ.get('ROSS1000_STRUCTURE_CODE', '')
    ROSS1000_PRODUCT = os.environ.get('ROSS1000_PRODUCT', 'CAV')
    ROSS1000_ENDPOINT = os.environ.get('ROSS1000_ENDPOINT', 'https://lazioturismo.ross1000.it/ws/checkinV2')
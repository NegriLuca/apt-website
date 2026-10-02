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
    # ── Email (Brevo HTTPS API — SMTP ports are blocked on Railway) ─────────
    # Get a free API key at app.brevo.com → SMTP & API → API Keys (300 mails/day free).
    # IMPORTANT: register MAIL_DEFAULT_SENDER as a validated sender in Brevo
    # (Senders → Add) or Brevo rejects every request.
    # Legacy Gmail SMTP vars (MAIL_SERVER/MAIL_PORT/MAIL_USERNAME/MAIL_PASSWORD)
    # are no longer used by the app and can be removed from Railway Variables.
    BREVO_API_KEY = os.environ.get('BREVO_API_KEY', '')
    MAIL_DEFAULT_SENDER = os.environ.get('MAIL_DEFAULT_SENDER') or os.environ.get('ADMIN_EMAIL') or 'lotto235roma@gmail.com'

    # ── Slack admin alerts (Incoming Webhook — also HTTPS, always works) ─────
    # Slack → Apps → Incoming Webhooks → add to a channel → paste URL here.
    # Empty = Slack alerts disabled (emails still go through Brevo).
    SLACK_WEBHOOK_URL = os.environ.get('SLACK_WEBHOOK_URL', '')

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
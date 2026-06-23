import os
from dotenv import load_dotenv

import stripe
# Load .env file when running locally.
# In production (Heroku, Railway, VPS) set these as real env vars instead.
load_dotenv()

class Config:
    # ── Security ──────────────────────────────────────────────────────────────
    SECRET_KEY = os.environ.get('SECRET_KEY') or 'dev-only-change-in-production'

    # ── Database ──────────────────────────────────────────────────────────────
    SQLALCHEMY_DATABASE_URI = os.environ.get('DATABASE_URL') or 'sqlite:///site.db'
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # ── Email ─────────────────────────────────────────────────────────────────
    MAIL_SERVER  = 'smtp.gmail.com'
    MAIL_PORT    = 587
    MAIL_USE_TLS = True
    MAIL_USERNAME       = os.environ.get('MAIL_USERNAME')
    MAIL_PASSWORD       = os.environ.get('MAIL_PASSWORD')
    MAIL_DEFAULT_SENDER = os.environ.get('MAIL_USERNAME')

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
# AGENTS.md — Apt_Website

## Quick start

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in secrets
python run.py           # serves on http://localhost:5001
```

No test runner, linter, or typechecker is configured. There is no CI.

## Commands

| Action | Command |
|---|---|
| Run dev server | `python run.py` (port 5001, or `$PORT`) |
| Prod server | `gunicorn "app:create_app()"` (already in `Procfile`) |
| DB migrations | `flask db migrate -m "msg"` then `flask db upgrade` |
| Compile translations | `pybabel compile -d translations` (runs automatically on startup) |

## Architecture

- **Monorepo/single app**: `app/` is a Flask application factory (`app:create_app()`)
- **Routes**: single blueprint (`routes.bp`) registered in `app/__init__.py`
- **Models**: SQLAlchemy in `app/models.py` — `User`, `Apartment`, `Reservation`, `ICalFeed`, `Coupon`, `Testimonial`, `ComplianceConfig`, `QuesturaLog`
- **Templates**: Jinja2 in `app/templates/` — booking flow, admin panels, email templates, legal policies
- **Services**: `app/services/` — `ical_sync.py`, `smart_lock.py` (Shelly+Nuki), `questura.py`, `tourist_tax.py`, `email_service.py`
- **Background tasks**: `app/tasks/compliance.py` — Celery tasks (Celery optional; tasks work synchronously as fallback). APScheduler runs iCal sync in-process.

## Quirks & gotchas

- **Email uses Brevo REST API (not Flask-Mail SMTP)**: `MAIL_PASSWORD` holds the Brevo API key. All email is sent via `POST https://api.brevo.com/v3/smtp/email`.
- **CSRF config**: `WTF_CSRF_SSL_STRICT = False`, `WTF_CSRF_TIME_LIMIT = 86400` (24h)
- **Database URI**: `DATABASE_URL` — auto-fixes `postgres://` → `postgresql://` in config. Local dev defaults to `sqlite:///app.db` inside `instance/`.
- **Admin auto-creation**: If `ADMIN_PASSWORD` env var is set, an admin user is created/updated on every startup in `run.py:62-79`.
- **Schema patches**: `run.py:48-59` attempts `ALTER TABLE reservations ADD COLUMN coupon_code` on startup (safe to ignore if column already exists).
- **Default apartment**: If the DB has no apartments, one named "Lotto 235 Garbatella" is seeded at startup.
- **iCal sync**: Background scheduler runs every `ICAL_SYNC_INTERVAL_MINUTES` (default 30). Syncs Airbnb/Booking/VRBO feeds.
- **Translations**: Compiled `.mo` files are built automatically at startup via `pybabel compile`. Supported: `en, it, de, fr, es`.
- **Rate limiting**: 200/day, 50/hour per IP via in-memory Flask-Limiter.
- **Port 5001** (not the Flask default 5000). Override via `$PORT`.
- **Brevo sender email**: Hardcoded as `lotto235roma@gmail.com` in `email_service.py` and `routes.py`.
- **Smart lock**: Shelly + Nuki use `requests` directly (no SDK). Configured via `Apartment` model fields.
- **Questura/tourist tax**: Italian compliance features are in `app/services/questura.py` and `app/services/tourist_tax.py`. Celery tasks defined in `app/tasks/compliance.py` but also callable synchronously.

## Directory layout

```
run.py                — entrypoint (starts app, compiles translations, seeds admin + defaults)
app/
  __init__.py         — create_app() factory, scheduler start
  routes.py           — single blueprint, ~2400 lines (booking, admin, API, webhooks)
  models.py           — all SQLAlchemy models (~364 lines)
  forms.py            — WTForms (Reservation, Login, Contact, ICalFeed, Testimonial)
  services/           — ical_sync, smart_lock, questura, tourist_tax, email_service
  tasks/              — compliance (Questura + tourist tax)
  templates/          — Jinja2 (public, admin, emails, policies, components)
config.py             — Config class (loaded by create_app)
Procfile              — web: gunicorn "app:create_app()"
migrations/           — Alembic/Flask-Migrate
translations/         — Babel .po/.mo (en, it, de, fr, es)
```

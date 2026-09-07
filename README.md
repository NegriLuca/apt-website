# Lotto 235 Garbatella — Apartment Booking Platform

> Direct-booking website for a vacation rental in Garbatella, Rome (CAV — *Casa per Vacanze*). Guests book on the site via Stripe or bank transfer; the platform handles availability (iCal), payments, Italian compliance (Questura / ROSS1000 / tourist tax / *ricevuta*), smart-lock access (Shelly + Nuki), guest self check-in with MRZ OCR, and multilingual content.

- **Production:** `gunicorn "app:create_app()"` — see `Procfile` (Railway / any PaaS)
- **Stack:** Flask 3.1 · SQLAlchemy 2 · Stripe · Brevo · APScheduler · Babel (5 locales) · Shelly Cloud + Nuki Web APIs
- **Primary locale:** `en` · Supported: `en`, `it`, `de`, `fr`, `es` (Babel)
- **Port:** `5001` (override with `$PORT`)

---

## Table of Contents

- [Features](#features)
- [Tech Stack](#tech-stack)
- [Quick Start (local)](#quick-start-local)
- [Docker](#docker)
- [Configuration](#configuration)
- [Project Structure](#project-structure)
- [Architecture](#architecture)
- [Booking & Payment Flow](#booking--payment-flow)
- [Availability / iCal Sync](#availability--ical-sync)
- [Smart Access & Boiler](#smart-access--boiler)
- [Italian Compliance](#italian-compliance)
- [Emails](#emails)
- [Guest-Facing Pages](#guest-facing-pages)
- [Admin Panel](#admin-panel)
- [Internationalization](#internationalization)
- [Testing & Linting](#testing--linting)
- [Deployment](#deployment)
- [Quirks & Gotchas](#quirks--gotchas)

---

## Features

| Area | What it does |
|---|---|
| **Direct booking** | Date/guest picker → dynamic pricing (weekend/holiday surcharge, extra-guest fee, weekly discount, coupon) → Stripe checkout or wire-transfer hold |
| **Payments** | Stripe Checkout (full payment with 5% discount or 30% deposit), balance-invoice cron, tourist-tax Stripe link, webhooks, refunds on cancellation |
| **Availability** | Single source of truth in DB; iCal import from Airbnb/Booking/VRBO; calendar blocks and orphan cleanup |
| **Compliance** | Questura AlloggiatiWeb (SOAP `GenerateToken` + `Send` schedine 168-char), ROSS1000 Regione Lazio, Roma tourist tax (€6/night/adult, max 10 nights, children 3-9 exempt), *ricevuta fiscale* with progressive numbering + bollo |
| **Self check-in** | Token link per reservation; collects all Questura fields (incl. companions); dual-engine MRZ OCR (CNN + Tesseract) for passport/ID |
| **Smart access** | Shelly Mini 1 Gen4 gate relay (cloud or local RPC) + Nuki Ultra door (Web API + Keypad 2 temporary PINs); time-windowed access links; cleaning-share links |
| **Operations** | APScheduler jobs (iCal sync, balance invoices, cleaning, keypad revocation, Questura daily, boiler ON/OFF), audit log, notifications, earnings CSV import (Airbnb) |
| **Content** | Homepage with testimonials/reviews, FAQ, policies, attractions/food guides, sitemap/robots, admin pricing/coupons/badges |

---

## Tech Stack

| Layer | Library |
|---|---|
| Web | Flask 3.1, Jinja2, Flask-WTF (CSRF), Flask-Login, Flask-Limiter (memory) |
| DB / ORM | Flask-SQLAlchemy 3.1, SQLAlchemy 2, Flask-Migrate / Alembic, psycopg2-binary, SQLite fallback |
| Payments | `stripe` 15.x |
| Email | Brevo REST API (`POST https://api.brevo.com/v3/smtp/email`) via `requests` |
| Background | APScheduler 3.11 (in-process), Celery tasks present but optional (`app/tasks/compliance.py` also runs synchronously) |
| iCal | `icalendar` 6.x + `requests` |
| i18n | Flask-Babel 4, `pybabel` |
| OCR / Docs | `opencv-python-headless`, `pytesseract`, `PyMuPDF`, `pillow`, `numpy`, `segno` (QR), `fpdf2` |
| Auth | `Flask-Bcrypt` / `bcrypt` |
| Ops | `gunicorn`, `holidays` (IT), `python-dotenv` |
| Dev | `ruff` 0.11, `pytest` 8 + `pytest-flask` — see `pyproject.toml` |

---

## Quick Start (local)

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env   # fill in at least SECRET_KEY + Stripe + Brevo
python run.py          # http://localhost:5001  (or $PORT)
```

Useful commands:

| Action | Command |
|---|---|
| Dev server | `python run.py` (port `5001`, respects `$PORT`) |
| Prod server | `gunicorn "app:create_app()" --access-logfile=- --error-logfile=-` |
| Shell | `flask shell` |
| DB migrations | `flask db migrate -m "msg"` then `flask db upgrade` (also runs automatically on startup via `app/__init__.py:_run_migrations` and `run.py`) |
| Compile translations | `pybabel compile -d translations` (runs automatically on startup) |
| Extract translations | `pybabel extract -F babel.cfg -o messages.pot .` then `pybabel update -i messages.pot -d translations` |
| Tests | `pytest` or `pytest -q` |
| Lint | `ruff check .` · `ruff format .` |
| Health check | `curl http://localhost:5001/health` → `{"status":"ok","version":"1.0"}` |

> `DATABASE_URL` defaults to `sqlite:///app.db` inside `instance/` when unset; `postgres://` is auto-rewritten to `postgresql://` in `config.py:17`.

---

## Docker

`Dockerfile` (`python:3.13-slim`) installs `tesseract-ocr` + `ocrb_int` traineddata, `pip install -r requirements.txt`, copies the app, exposes `5001`, and runs `gunicorn "app:create_app()"`. Railway uses this `Dockerfile` (not `nixpacks.toml`).

```bash
docker build -t apt-website .
docker run --env-file .env -p 5001:5001 apt-website
```

---

## Configuration

All config lives in `config.py:Config` (loaded by `create_app`) and is overridden by env vars. `.env.example` documents every variable.

### Required

| Var | Purpose |
|---|---|
| `SECRET_KEY` | Flask session/CSRF signing. Warns if left at `dev-only-change-in-production` (`app/__init__.py:60`). |
| `ADMIN_PASSWORD` | On every startup `run.py` creates/updates `User(username='admin')` with this password. If unset, admin setup is skipped. |

### Database

| Var | Default | Notes |
|---|---|---|
| `DATABASE_URL` | `sqlite:///app.db` | Supports `postgres://` and `postgresql://`. Retried 5× with 3s delay in `run.py`. |

### Email (Brevo)

| Var | Notes |
|---|---|
| `MAIL_USERNAME` / `ADMIN_EMAIL` | Admin recipient; `ADMIN_EMAIL` falls back to `MAIL_USERNAME`. |
| `MAIL_PASSWORD` | **Brevo API key** (not SMTP password). All sends go to `POST https://api.brevo.com/v3/smtp/email`. |
| Sender | Hardcoded `lotto235roma@gmail.com` in `app/services/email_service.py` and `app/routes/helpers.py` / `app/routes/public.py`. |

### Stripe

| Var |
|---|
| `STRIPE_SECRET_KEY` / `STRIPE_PUBLISHABLE_KEY` / `STRIPE_WEBHOOK_SECRET` |
| `BASE_URL` — used for `success_url`/`cancel_url` (default `http://localhost:5001`) |

### i18n / Analytics

| Var | Notes |
|---|---|
| `GTM_ID` / `GA4_ID` | Injected in `base.html` when set. |
| `ICAL_SYNC_INTERVAL_MINUTES` | Default `30`. |

### Italian compliance (CIN/CIR + host)

| Var | Default | Notes |
|---|---|---|
| `CIN_CODE` | `IT058091C2TXZ44TA6` | Synced to `Apartment.cin_code` on startup if changed (`run.py:161`). |
| `CIR_CODE` | `058091-LOC-19856` | Synced to `Apartment.cir_code`. |
| `HOST_FULL_NAME` / `HOST_CODICE_FISCALE` / `HOST_ADDRESS` | — | Synced to `Apartment.host_*` on every startup (`run.py:136`). |
| `HOST_VAT_MODE` | `fuori_campo_iva` | |
| `ROSS1000_USERNAME` / `ROSS1000_PASSWORD` / `ROSS1000_STRUCTURE_CODE` / `ROSS1000_PRODUCT` / `ROSS1000_ENDPOINT` | Endpoint default `https://lazioturismo.ross1000.it/ws/checkinV2` | ROSS1000 SOAP (Regione Lazio). |
| `QUESTURA_USERNAME` / `QUESTURA_PASSWORD` / `QUESTURA_WS_KEY` / `QUESTURA_PROTOCOL_NUMBER` / `QUESTURA_ENDPOINT` | Endpoint default `https://alloggiatiweb.poliziadistato.it/service/service.asmx` | AlloggiatiWeb SOAP; env vars override `ComplianceConfig` DB values (`app/services/questura.py:92`). |

### Smart access

| Var | Notes |
|---|---|
| `SHELLY_CLOUD_SERVER` / `SHELLY_CLOUD_KEY` / `SHELLY_DEVICE_ID` | Gate relay — cloud Control API v2 (`/v2/devices/api/...`). `SHELLY_DEVICE_ID` or `Apartment.shelly_host` holds the device ID (e.g. `98a31678b358`). |
| `SHELLY_BOILER_DEVICE_ID` / `SHELLY_BOILER_CHANNEL` / `SHELLY_BOILER_HOST` | Second Shelly for hot-water boiler (device `206ef104b850`, ch `0`). Auto-enabled on startup when device ID is set. |
| `NUKI_SMARTLOCK_ID` / `NUKI_WEB_TOKEN` / `NUKI_WEB_BASE_URL` / `NUKI_UNLOCK_ACTION` | Nuki door. Synced to `Apartment.nuki_*` on startup; `NUKI_UNLOCK_ACTION` is `unlatch` (open) vs `unlock` (turn cylinder) (`run.py:89`). |

### Wi-Fi

| Var | Notes |
|---|---|
| `WIFI_SSID` / `WIFI_PASSWORD` / `WIFI_SECURITY` (`WPA`/`WEP`/`nopass`) / `WIFI_BAND` / `WIFI_HIDDEN` | Guest Wi-Fi QR (`app/services/wifi_qr.py`). Synced from env on startup (`run.py:105`). |

### Compliance encryption

| Var | Notes |
|---|---|
| `COMPLIANCE_ENCRYPTION_KEY` | Fernet key for `ComplianceConfig` values. If unset, falls back to `SECRET_KEY[:32]` padded with `0` (`app/models.py:554`) — avoid in production. |

---

## Project Structure

```
run.py                   — entrypoint: compiles translations, seeds admin + apartment,
                           syncs Nuki/boiler/Wi-Fi/host/CIN-CIR from env
config.py                — Config class (env → Flask config, postgres:// fix)
Procfile                 — web: gunicorn "app:create_app()"
Dockerfile               — python:3.13-slim + tesseract + ocrb traineddata
babel.cfg                — pybabel extract config
pyproject.toml           — ruff + pytest config
requirements.txt
translations/            — Babel .po/.mo (en, it, de, fr, es) — compiled on startup
migrations/              — Alembic / Flask-Migrate
instance/                — SQLite DB (local dev)

app/
  __init__.py            — create_app() factory, logging, Babel, context processors,
                           _compile_translations(), _run_migrations(), _start_scheduler()
  models.py              — all SQLAlchemy models (User, Apartment, Reservation, Earning,
                           CleaningAccess, ICalFeed, Coupon, Testimonial, ComplianceConfig,
                           AuditLog, Notification, QuesturaLog, Ross1000Log, Receipt)
  forms.py               — WTForms (ReservationForm, LoginForm, ContactForm, ICalFeedForm, …)
  routes/
    __init__.py          — Blueprint routes.bp
    public.py            — /, /faq, policies, /contact, /ical/apartment.ics, /sitemap.xml,
                           /robots.txt, /testimonial/submit, /set-language/<lang>
    booking.py           — /reserve, /checkout, /process-payment, /checkout/create-session,
                           /payment/success, /stripe/webhook, /booking/confirmed/<id>,
                           /cancel/<token>, /review-and-pay
    admin.py             — /admin, /admin/calendar, /admin/pricing, /admin/earnings,
                           /admin/smart-access, /admin/boiler/*, /admin/wifi/*,
                           /admin/trust-badges, coupons, reservations, feeds, testimonials,
                           access links, keypad codes, audit, notifications, …
    compliance.py        — /admin/compliance*, Questura/ROSS1000/tourist-tax/config/check-in links
    api.py               — /api/validate-coupon, /api/calculate-price, /api/access/{gate,door}/open,
                           /api/cleaning-access/*, /checkin/<token>, /access/<token>,
                           /cleaning-access/<token>, /portal/<token>, …
    helpers.py           — get_apartment(), pricing helpers, Brevo send, Stripe helpers,
                           balance-invoice reminders
  services/
    ical_sync.py         — feed fetch + dedup + orphan cancellation + cleanup
    smart_lock.py        — ShellyService (cloud + local RPC) + NukiService + keypad lifecycle
    boiler.py            — BoilerShellyService + gap-aware ON/OFF jobs
    questura.py          — QuesturaService (SOAP, schedine 168-char, Tabella lookups)
    ross1000.py          — ROSS1000Service (Regione Lazio SOAP)
    tourist_tax.py       — TouristTaxService (€6/night/adult, max 10, children exempt)
    email_service.py     — Brevo senders (check-in, access, admin notification)
    wifi_qr.py           — WIFI: payload + QR PNG/data-URI (segno)
    receipts.py          — ricevuta fiscale helpers
    airbnb_earnings.py   — Airbnb earnings CSV parser
    mrz_extractor.py     — MRZ orchestration (dual-engine CNN + Tesseract)
    mrz_cnn.py           — char-CNN ONNX OCR (20×20 → 37 symbols)
    models/mrz-cnn.onnx + .json
  tasks/
    compliance.py        — Celery tasks (also callable synchronously): Questura daily/retry,
                           ROSS1000 daily, tourist-tax report, check-in reminders
  templates/             — Jinja2 (public, admin, emails, policies, components)
  static/images/apartment/
docs/
  MRZ_HANDOFF.md         — MRZ extraction design, benchmarks, prototype notes
EMAILS.md                — all 10 email flows (triggers, templates, subjects)
IMPROVEMENTS.md          — roadmap / completed items
tests/
  conftest.py            — TestConfig (in-memory SQLite, CSRF/Limiter off), seed, login_admin()
  test_booking.py / test_cancellation.py / test_external_reservations.py
  test_questura.py / test_tourist_tax.py
```

---

## Architecture

### App factory — `app/__init__.py:create_app()`

- Reads `config.Config`, warns on insecure `SECRET_KEY`, configures logging (stdout + rotating `app.log` when not in debug).
- Inits `SQLAlchemy`, `Migrate`, `LoginManager` (`login_view = routes.login`), `CSRFProtect` (`WTF_CSRF_SSL_STRICT=False`, `WTF_CSRF_TIME_LIMIT=86400`), `Mail` (unused — Brevo is used instead), `Bcrypt`, `Flask-Moment`, `Limiter` (`200/day`, `50/hour`, `memory://`), `Babel` (locale selector: `?lang=` → `session['language']` → `Accept-Language` → `en`).
- Auto-compiles translations and runs `flask db upgrade` on boot; registers `routes.bp`; adds `/health`, CSRF handler, and context processors (`apartment`, `unread_notifications`, `get_locale`, `now`).
- Starts `APScheduler` (`BackgroundScheduler`, skipped when `WERKZEUG_RUN_MAIN=false`).

### Models — `app/models.py`

Core tables:

- **User** (`username`, bcrypt `password`, `is_admin`) — `load_user` via `LoginManager`.
- **Apartment** (singleton) — nightly rate, CIN/CIR, tourist-tax category/rate, host *ricevuta* fields, Shelly/Nuki/boiler config, WhatsApp, review trust badges, Wi-Fi. `wifi_payload()` / `wifi_connect_uri()`.
- **Reservation** — guest identity (Questura fields + companions JSON + document), dates (`CheckConstraint check_out > check_in`, `1..4` guests), `status` (`pending`/`confirmed`/`cancelled`), `source` (`direct`/`stripe` vs `airbnb`/`booking_com`/`vrbo`), `is_block`, pricing (`total_price`, `amount_paid`, `coupon_code`, Stripe IDs, `balance_invoice_sent_at`), compliance (`questura_*`, `ross1000_*`, `checkin_token`, `access_token`, Nuki `keypad_*`, configurable access window `access_checkin_time`/`access_checkout_time` HH:MM, tourist tax, billing address). Helpers: `nights`, `questura_ready()`, `get_access_window*()`, `is_access_valid()`, `generate_access_token()`.
- **Earning** — one row per Airbnb/Booking confirmation code (`platform`, `confirmation_code`, `gross_earnings`, `amount`, `service_fee`, `withholding`, `net`, linked to `Reservation`).
- **CleaningAccess** — shareable gate/door window (`token`, `starts_at`/`ends_at` in `Europe/Rome`, `keypad_*`).
- **ICalFeed** (`source`, `url`, `active`, `last_synced_at`).
- **Coupon** (`code`, `discount_type` `percentage`/`flat`, `discount_value`) — `apply_discount()`.
- **Testimonial** / **ComplianceConfig** (Fernet-encrypted `value_encrypted`, `get`/`set` helpers) / **AuditLog** / **Notification** / **QuesturaLog** / **Ross1000Log** / **Receipt** (progressive `receipt_number` `NN/YYYY`, snapshot of host/guest/stay, bollo handling).

### Services — `app/services/`

- **ical_sync.py** — `sync_feed()` fetches one feed, parses `VEVENT`s, classifies blocks vs bookings (`_classify_event`), dedups by `external_uid` or exact dates, repairs legacy `is_block` rows, adds new `Reservation`s, cancels orphans whose UID *and* dates vanished; `sync_all_feeds()` + `cleanup_past_external_reservations()`.
- **smart_lock.py** — `ShellyService` (cloud v2 `auth_key` + `ids` vs local `/rpc/Switch.Set`+`/rpc/Shelly.GetStatus`), `NukiService` (Web API `/smartlock/{id}/action`, keypad lifecycle `PUT /smartlock/auth` / `POST /smartlock/{id}/auth/{authId}` / `DELETE .../auth/{authId}`, windowed PINs), `revoke_reservation_keypad()` / `revoke_expired_keypad_codes()`.
- **boiler.py** — `BoilerShellyService` (shared cloud creds, second device ID) + `should_turn_off_on_checkout()` (gap ≥2 days) + `run_boiler_checkin_job()` (07:00 ON) / `run_boiler_checkout_job()` (16:00 conditional OFF).
- **questura.py** — `QuesturaService` (retrying `requests.Session`, env-or-DB config, 168-char schedine `build_schedine()` via `Tabella` lookups for comune/document codes, `GenerateToken` → `Send` SOAP envelope, `QuesturaLog` + `Reservation.questura_*` updates).
- **ross1000.py** — `ROSS1000Service` (similar SOAP pattern for Regione Lazio).
- **tourist_tax.py** — `TouristTaxService` (rate from `Apartment`, `calculate_tax` with `MAX_TAXABLE_NIGHTS=10`, semicolon-delimited CSV export, rate `6.00` default).
- **email_service.py** / **helpers.py** — Brevo `POST https://api.brevo.com/v3/smtp/email` everywhere (suppressed when `TESTING` or `MAIL_SUPPRESS_SEND`).
- **wifi_qr.py** — `WIFI:` payload + PNG via `segno`.
- **mrz_extractor.py + mrz_cnn.py** — dual-engine OCR (CNN ONNX 20×20 → `0-9A-Z<` then Tesseract fallback/confirmation), TD3 (2×44) / TD1 (3×30) parser, `_crop_mrz_region` gap-merge, PDF via PyMuPDF. See `docs/MRZ_HANDOFF.md`.

### Routes

Single blueprint `routes.bp` (`app/routes/__init__.py`) split into `public`, `booking`, `admin`, `compliance`, `api`. All admin routes guard with `@login_required` + `is_admin` 403.

### Scheduler — `app/__init__.py:_start_scheduler()`

Only when `WERKZEUG_RUN_MAIN != "false"` (avoids double-start under reloader):

| ID | Trigger | Job |
|---|---|---|
| `ical_sync` | every `ICAL_SYNC_INTERVAL_MINUTES` | `sync_all_feeds()` |
| `balance_invoice_reminder` | daily 09:00 | `send_balance_invoice_reminders()` (deposit → balance Stripe session) |
| `cleanup_external_reservations` | daily 04:30 | `cleanup_past_external_reservations()` |
| `revoke_expired_keypad_codes` | daily 12:00 | `revoke_expired_keypad_codes()` |
| `questura_daily_submission` | daily 08:00 | `run_daily_questura()` (SOAP submit check-ins) |
| `boiler_checkin_on` | daily 07:00 | `run_boiler_checkin_job()` (ON unconditionally) |
| `boiler_checkout_off` | daily 16:00 | `run_boiler_checkout_job()` (OFF only if gap ≥2) |

Celery Beat schedule mirrors these in `app/tasks/compliance.py:CELERY_BEAT_SCHEDULE` for deployments that run Celery.

### Startup seeding — `run.py`

1. Compiles Babel translations (`pybabel compile -d translations`).
2. Creates `app`, retries DB connect 5×, runs `db.create_all()` + `upgrade()`.
3. Creates/updates `admin` from `ADMIN_PASSWORD`.
4. Seeds `Apartment(name="Lotto 235 Garbatella")` when empty.
5. Syncs `Nuki` (`NUKI_*`), `Wi-Fi` (`WIFI_*` via `sync_wifi_from_env()`), `boiler Shelly` (`SHELLY_BOILER_*`), `host ricevuta` (`HOST_*`), and `CIN`/`CIR` from env (DB overridden on every boot).

---

## Booking & Payment Flow

### Pricing — `app/routes/helpers.py:calculate_dynamic_total()`

Per night: `base_rate` (`Apartment.price_per_night`) + `+10%` on Italian holidays (`holidays.Italy`) or Fri/Sat + `€15` per extra guest beyond 2. Whole stay `×0.90` when `nights ≥ 7`. Rounded to cents. `apply_full_payment_discount()` applies `FULL_PAYMENT_DISCOUNT_PCT = 5%` on the stay total (full-pay path).

City tax excluded from stay total: `calculate_city_tax()` = `min(nights,10) × num_adults × rate` (rate from `TouristTaxService`, default `6.00`).

### Reserve → Checkout — `app/routes/booking.py`

1. **`GET /reserve`** — loads `Apartment`, collects blocked dates from non-cancelled reservations, computes `disabled_dates` (nights) + `checkin_blocked`. Renders `reservation.html`.
2. **`POST /reserve`** — validates `ReservationForm` (+ `MAX_GUESTS=4`), checks `check_out > check_in`, `nights ≤ 28`, `is_available()` (no overlapping `status != cancelled`), coupon lookup, computes `base_total` → `total` via `apply_discount`, stores `pending_reservation` in session, redirects to `/checkout`.
3. **`GET /checkout`** (`checkout.html`) — recomputes pricing + surcharges + discount + `city_tax` + `full_pay_total` + `deposit_total` (`30%`), renders Stripe key.
4. **`POST /process-payment`** — branches on `payment_method`:
   - `wire_transfer` → creates `Reservation(status=pending, payment_status=unpaid, payment_method=wire_transfer, cancel_token, tourist_tax_amount)`, sends `send_pending_payment_email()`, stores `completed_wire_res_id` in session, redirects to `/checkout/wire-transfer` (`wire_transfer.html`).
   - `stripe` → delegates to `create_checkout_session()`.

### Stripe Checkout — `app/routes/booking.py:create_checkout_session()`

- `stripe_amount` = `deposit` → charge `30%` of full total; else `full` → charge `apply_full_payment_discount(full_total) + tourist_tax + bollo` (`€2` when `full_total > 77.47`).
- Creates `stripe.checkout.Session` with `billing_address_collection=required`, `customer_creation=always`, `phone_number_collection`, custom fields for `tipo_documento_fiscale` / `codice_fiscale_documento`, metadata (`guest_name`, `check_in`, … `is_deposit`, `coupon_code`), `success_url` → `/payment/success?session_id={CHECKOUT_SESSION_ID}`.
- Stores `pending_stripe_session` in session, redirects `303` to `checkout_session.url`.

### Success / Webhook — `app/routes/booking.py`

- **`GET /payment/success?session_id=`** — `stripe.checkout.Session.retrieve(expand=['line_items','payment_intent.charges','customer_details'])`, calls `_create_reservation_from_stripe()` (idempotent on `stripe_payment_intent_id`), `enrich_reservation_from_stripe_session()` (billing address/charge/receipt), pops session keys, redirects to `/booking/confirmed/<id>`.
- **`POST /stripe/webhook`** (`@csrf.exempt`) — `stripe.Webhook.construct_event` with `STRIPE_WEBHOOK_SECRET`. Handles `checkout.session.completed`:
  - `metadata.type == tourist_tax` → mark `tourist_tax_paid=True`.
  - `metadata.type == balance_payment` → flip `deposit_paid` → `paid`, bump `amount_paid`.
  - else → create/ensure reservation + enrich + `send_payment_verified_email()`.

### Cancellation — `app/routes/booking.py:cancel_reservation()`

`GET /cancel/<cancel_token>` — blocks if already cancelled / not confirmed / `today >= check_in`; computes `refund_percentage` via `helpers.calculate_refund_percentage()` (`>14d → 100%`, `7..14d → 50%`, `<7d → 0%`), attempts `stripe.Refund.create(amount=refund_amount*100)` when `refund_amount>0`, revokes Nuki keypad, marks `cancelled`, sends `send_cancellation_emails()`.

---

## Availability / iCal Sync

Feeds (`ICalFeed`, `app/routes/admin.py` + `app/services/ical_sync.py`):

- Sources: `airbnb`, `booking`/`booking_com`, `vrbo`, `other`; `active` flag; `last_synced_at`.
- `sync_feed(feed)` — `GET feed.url`, `Calendar.from_ical`, collects `live_uids` + `live_date_pairs`; classifies each `VEVENT` (Booking feeds: every event is a booking; others: `_classify_event` filters `Blocked`/`Not available`); dedups on `external_uid` or exact `(check_in, check_out)`; repairs legacy `is_block` rows; inserts new `Reservation(source=display_source, is_block=False, total_price=0)`.
- Orphan cancellation: non-cancelled OTA rows whose `external_uid` not in `live_uids` **and** whose dates not in `live_date_pairs` → `revoke_reservation_keypad()` + `status=cancelled`; empty feed never cancels (fetch-error guard).
- `cleanup_past_external_reservations()` (daily 04:30) hard-deletes past `is_block=True` rows (repairs legacy `booking_com` blocks instead).
- Export: `GET /ical/apartment.ics` (`app/routes/public.py`) publishes non-cancelled reservations as `VEVENT`s.

---

## Smart Access & Boiler

### Apartment gate (Shelly Mini 1 Gen4) — `app/services/smart_lock.py:ShellyService`

- Config: `Apartment.shelly_enabled`, `shelly_host` (device ID in cloud mode else IP/hostname), `shelly_auth_key`, `shelly_relay_channel`.
- Cloud mode when `SHELLY_CLOUD_SERVER` + `SHELLY_CLOUD_KEY` + `SHELLY_DEVICE_ID`/`shelly_host` all set → `POST https://<server>/v2/devices/api/set/switch` (`{id, channel, on}`) + `/v2/devices/api/get`; else local RPC `POST http://<host>/rpc/Switch.Set` + `POST /rpc/Shelly.GetStatus` (Bearer if `shelly_auth_key`).
- `pulse_relay()` / `turn_on()` / `turn_off()` / `get_status()`; gate relies on device `auto_off` for pulse.

### Apartment door (Nuki Ultra + Keypad 2) — `NukiService`

- Config: `Apartment.nuki_enabled`, `nuki_smartlock_id`, `nuki_web_token`, `nuki_web_base_url` (default `https://api.nuki.io`), `nuki_unlock_action` (`unlock`→code 1, `lock`→2, `unlatch`→3), `nuki_show_door_button`.
- Door API: `POST /smartlock/{id}/action` (`{action: code}`), `GET /smartlock/{id}`, `GET /smartlock/{id}/auth` for keypad listing.
- Temporary PINs: `_generate_keypad_code()` (6 digits, no `0`, not `12*`), `create_keypad_code(name, allowed_from, allowed_until)` → `PUT /smartlock/auth` (`type:13`, `smartlockIds`, `allowedFromDate/UntilDate` UTC `YYYY-MM-DDTHH:MM:SS.000Z`); `find_keypad_auth_id()` (polls 6×3s), `update_keypad_code_window()` (`POST /smartlock/{id}/auth/{authId}`), `revoke_keypad_code()` (`DELETE .../auth/{authId}`). Helpers `revoke_reservation_keypad()` / `revoke_expired_keypad_codes()` manage lifecycle.

### Boiler (hot water) — `app/services/boiler.py:BoilerShellyService`

Second Shelly (`206ef104b850` ch `0`) sharing the same cloud creds. Manual controls at `POST /admin/boiler/on|off`, status at `GET /admin/boiler/status`, dry-run at `POST /admin/boiler/test-jobs`. Jobs: 07:00 unconditional `turn_on()` on check-in days; 16:00 `turn_off()` only when *every* checkout today has gap ≥2 days to next check-in (`should_turn_off_on_checkout`).

### Access model — `app/models.py:Reservation`

`access_token` (`token_urlsafe(32)`, indexed), `checkin_token`, configurable window `access_checkin_time`/`access_checkout_time` (`HH:MM`, default `13:00→13:00`, `Europe/Rome` → UTC for Nuki). `is_access_valid()` gates both guest pages and `POST /api/access/{gate,door}/open` (token in `X-Access-Token` or `token` form, `10/min` rate limit, `@csrf.exempt`). `CleaningAccess` offers shareable windows with the same keypad pattern.

---

## Italian Compliance

### Questura — AlloggiatiWeb — `app/services/questura.py`

- `QuesturaService` wraps `Requests.Session` with retries; config from env `QUESTURA_*` or encrypted `ComplianceConfig` (`questura_username/password/ws_key/protocol_number/wsdl_url`); `is_configured()` guards; endpoint defaults to prod, `alloggiatiwebtest` in `test_mode`.
- **Schedine:** `build_schedine(guests)` emits 168-char *tabella 1* records (`tipo` `16`=singolo, `17`=capo famiglia, `19`=familiare; document fields blank for familiari; `sesso` `1`=M/`2`=F; `stato_nascita`/`cittadinanza` via `ITALY_STATE_CODE`; `comune`/`provincia` via `Tabella Luoghi` lookup; `tipo documento` via `Tabella Tipi_Documento` → `IDENT` etc.). Supports companions via `Reservation.companions` JSON.
- **SOAP:** `GenerateToken` (`Utente/Password/WsKey`) → `Send` (`Utente/token/ElencoSchedine<string>*`); helpers `_build_soap_envelope` / `_call` / `_parse_send_response` (checks `SchedineValide` + `ErroreDettaglio`); logs to `QuesturaLog` + `Reservation.questura_*`.
- Admin: `/admin/compliance/questura`, `.../logs`, `POST .../submit`, `POST .../run-daily` (also via APScheduler daily 08:00 / `app/tasks/compliance.py:run_daily_questura()`). `ComplianceConfig` at `/admin/compliance/config`.

### ROSS1000 — Regione Lazio — `app/services/ross1000.py` + `config.py:ROSS1000_*`

SOAP service for Lazio (`https://lazioturismo.ross1000.it/ws/checkinV2`, `ROSS1000_PRODUCT=CAV`). Mirrors the Questura pattern; admin at `/admin/compliance/ross1000*`, daily task `submit_ross1000_daily`.

### Tourist tax — `app/services/tourist_tax.py`

`TouristTaxService` with `DEFAULT_RATES` (all `6.00`), `EXEMPT_AGE=10`, `MAX_TAXABLE_NIGHTS=10`; `calculate_tax()` uses `Reservation.num_adults` (fallback `num_guests`), skips non-`confirmed`; CSV export `delimiter=';'` with headers `ID Prenotazione … Stato` + `;`-totals row; `generate_detailed_report()` for `/admin/compliance/tourist-tax` (report, config save, CSV download, per-reservation tax override/exclude).

### Receipts — `app/models.py:Receipt` + `app/services/receipts.py`

*Ricevuta fiscale* for `direct`/`stripe` stays: annual progressive `receipt_number` `NN/YYYY` (`year`/`sequence`), snapshot of host (`host_full_name`, `codice_fiscale`, `address`, `CIN`/`CIR`) + guest (residence, `CF`, document) + stay + amounts (`stay_amount`, `tourist_tax_amount`, `total_amount`, `payment_method`, Stripe IDs), `bollo` (`bollo_required` when `stay_amount > 77.47`, `€2`, `bollo_id` 14-digit), `is_confirmed` lock. Managed at `/admin/receipts*`.

---

## Emails

All via Brevo REST (`POST https://api.brevo.com/v3/smtp/email`, sender `lotto235roma@gmail.com`, admin `ADMIN_EMAIL` fallback). Suppressed when `TESTING` or `MAIL_SUPPRESS_SEND` (`tests/conftest.py` enables this). See `EMAILS.md` for the full table; helpers in `app/routes/helpers.py` and `app/services/email_service.py`.

| # | Flow | Recipients | Trigger |
|---|---|---|---|
| 1 | Booking confirmation (Stripe) | Guest + Admin | `_send_confirmation_emails()` after Stripe success |
| 2 | Pending payment (wire) | Guest + Admin | `send_pending_payment_email()` on `wire_transfer` choice |
| 3 | Payment verified | Guest + Admin | `send_payment_verified_email()` (admin confirms wire / webhook) |
| 4 | Cancellation | Guest + Admin | `send_cancellation_emails()` on cancel |
| 5 | Check-in link (manual) | Guest | `POST /admin/compliance/send-checkin-link` |
| 6 | Check-in email (automated) | Guest | `send_checkin_email()` |
| 7 | Access link | Guest | `send_access_email()` |
| 8 | Admin check-in notification | Admin | `send_admin_checkin_notification()` on guest form submit |
| 9 | Review request | Guest | Admin single + bulk on dashboard |
| 10 | Contact inquiry | Admin | `POST /contact` (inline HTML) |

Balance invoice (`email_balance_invoice.html`), tourist-tax success, and guest self check-in templates are also present under `app/templates/`.

---

## Guest-Facing Pages

| Route | Purpose |
|---|---|
| `GET /` | Homepage (`apartment.html`): gallery, pricing, availability, testimonials |
| `GET /reserve` | Date/guest/coupon form; shows blocked dates |
| `GET /checkout` | Quote + payment choice (Stripe full/deposit vs wire) |
| `GET /payment/success?session_id=` | Post-Stripe landing → creates reservation → `booking_confirmed` |
| `GET /booking/confirmed/<id>` | Confirmation page |
| `GET /cancel/<token>` | Guest self-cancel + refund |
| `GET /ical/apartment.ics` | Public iCal export |
| `GET /sitemap.xml` / `GET /robots.txt` | SEO |
| Policy pages | `/terms`, `/cancellation-policy`, `/refund-policy`, `/house-rules`, `/privacy`, `/faq`, `/food_recommendations`, `/attractions`, `/contact` |
| `GET /checkin/<token>` | Token self check-in form (all guests, required Questura fields; blocks cancelled) |
| `POST /checkin/<token>/pay-tax` + `GET /checkin/<token>/tax-link` | Create tourist-tax Stripe session for the reservation |
| `GET /access/<token>` | Guest gate/door page (window-gated `13:00→13:00` Rome, `wifi_qr`) |
| `POST /api/access/gate/open` / `POST /api/access/door/open` | Token + window-gated relay/Nuki calls (`limiter 10/min`) |
| `GET /cleaning-access/<token>` + `POST /api/cleaning-access/gate/open` | Shareable cleaning window |
| `GET /checkin-guide/<token>` | Personalised guide (keypad code, window `window_start_iso/end_iso`) — not window-gated |
| `GET /portal/<token>` | Unified portal: check-in + access + tax + Wi-Fi in one view |
| `GET /set-language/<lang>` | `session['language']` switch (`en/it/de/fr/es`) |

---

## Admin Panel

Login at `GET /login` (`LoginForm`, `limiter 10/min`, `Flask-Login`). Every admin route requires `@login_required` + `is_admin` (403 otherwise). Audited via `AuditLog`.

| Route | Panel |
|---|---|
| `GET /admin` | Dashboard: totals, monthly/yearly revenue, source breakdown, occupancy (next 3 months), today check-ins/outs, in-house, pending Questura, upcoming |
| `GET /admin/calendar` | FullCalendar view; `GET /api/admin/calendar-reservations` JSON |
| `GET+POST /admin/pricing` | Base rate + `Coupon` CRUD (`POST /admin/coupons/create`, `POST /admin/coupons/<id>/delete`) |
| `GET+POST /admin/earnings` | Airbnb earnings CSV import (`parse_earnings_csv`), stored `Earning`s, booking sync |
| `GET /admin/smart-access` + `GET /admin/smart-access/preview` | Shelly/Nuki/boiler status + preview of `guest_access.html` |
| `POST /admin/boiler/on|off` + `GET /admin/boiler/status` + `POST /admin/boiler/test-jobs` | Boiler manual & dry-run |
| `GET+POST /admin/wifi` + `GET /admin/wifi/qr.png` + `GET /admin/wifi/print` | Wi-Fi SSID/security + `segno` QR |
| `GET+POST /admin/trust-badges` | Review badges, custom badges, widget JS (`booking_widget_js` etc.), Shelly/Nuki/boiler fields, WhatsApp |
| Reservations | `POST /admin/reservations/<id>/{confirm,cancel,edit,delete}`, `POST …/bulk-delete`, `GET /admin/cancel-booking/<token>` |
| iCal feeds | `GET+POST /admin/feeds/add|{id}/edit`, `POST …/{id}/delete`, `POST /admin/feeds/sync` |
| Testimonials | `GET /admin/testimonials`, `POST …/{id}/{publish,feature,delete}` |
| Access | `POST /admin/access/{generate-link,send-link,regenerate-token,generate-keypad-code}` |
| Compliance | `GET /admin/compliance` (Questura/ROSS1000 counters, tax, config status), `/admin/compliance/{questura,ross1000, tourist-tax, config, send-checkin-link, regenerate-checkin-token}` + logs pages |
| Receipts | `GET /admin/receipts*` |
| System | `GET /admin/audit-log`, `GET /admin/notifications` (with `unread_notifications` badge), review-request sends |

---

## Internationalization

- `Flask-Babel` with `get_locale()` (`app/__init__.py:38`): `?lang=` override → `session['language']` → `request.accept_languages.best_match` → `en`.
- `BABEL_DEFAULT_LOCALE='en'`, `LANGUAGES=['en','it','de','fr','es']`, `WTF_CSRF_TIME_LIMIT=86400`, `WTF_CSRF_SSL_STRICT=False`.
- Translations under `translations/{en,it,de,fr,es}/LC_MESSAGES/messages.{po,mo}`; compiled on startup (`_compile_translations` + `run.py` `pybabel compile`).
- Compile: `pybabel compile -d translations`; extract: `pybabel extract -F babel.cfg -o messages.pot .` (`[python: **.py]` + `[jinja2: **/templates/**.html]`).

---

## Testing & Linting

```bash
pytest                 # 5 suites: test_booking, test_cancellation, test_external_reservations, test_questura, test_tourist_tax
pytest -q
pytest tests/test_booking.py -v
ruff check .           # pyproject.toml: E,F,W,I,N,UP,SIM, line-length 120, single quotes
ruff format .
```

`tests/conftest.py:TestConfig` uses `sqlite:///:memory:`, `WTF_CSRF_ENABLED=False`, `SECRET_KEY='test-secret'`, `MAIL_SUPPRESS_SEND=True`, `RATELIMIT_ENABLED=False`; `app`/`client`/`runner` fixtures + `_seed_data()` (admin `admin123` + apartment) + `login_admin(client)` helper.

---

## Deployment

- **Entrypoint:** `run.py` (also the `create_app` factory for `gunicorn "app:create_app()"`). Adaptable to Railway/Heroku/VPS by setting env vars.
- **Procfile:** `web: gunicorn "app:create_app()" --access-logfile=- --error-logfile=- --log-level=info`.
- **Docker:** `Dockerfile` — `python:3.13-slim` + `tesseract-ocr`/`tesseract-ocr-eng` + `ocrb_int.traineddata` for MRZ.
- **Migrations:** `migrations/` (Alembic). `run.py` + `app/__init__.py` run `upgrade()` on boot.
- **Logging:** `logging.basicConfig` to stdout + `RotatingFileHandler('app.log')` (1 MB × 5) when not debug; all levels `INFO` and `propagate=True` so Gunicorn/Railway see errors.
- **Rate limiting:** in-memory (`memory://`) — not shared across Gunicorn workers; use Redis `storage_uri` for multi-worker strictness.
- **Secrets:** `.env` is gitignored; see `.env.example` for the full list.

---

## Quirks & Gotchas

- **Brevo, not SMTP:** `MAIL_PASSWORD` is the Brevo API key; `Flask-Mail` is initialised but unused. Sender is hardcoded `lotto235roma@gmail.com`.
- **Single apartment:** `Apartment.query.first()` is injected everywhere (`app/__init__.py:inject_apartment`); the app is single-property despite `Apartment` being a table.
- **Access window:** `Reservation.access_checkin_time` / `access_checkout_time` are `HH:MM` strings (Rome tz) defaulting to `13:00`; Nuki windows are converted to UTC. Guest access (`/access/<token>`, `/api/access/*`) is 403 outside the window; `/checkin-guide/<token>` is intentionally not.
- **Booking sources:** `direct`/`stripe` are direct; `airbnb`/`booking_com`/`vrbo` are OTA (`is_block` distinguishes real stays vs calendar blocks before cleanup).
- **No multi-worker limiter:** `Limiter(storage_uri='memory://')` counts per process.
- **Startup overrides:** `ADMIN_PASSWORD`, `NUKI_*`, `SHELLY_BOILER_*`, `WIFI_*`, `HOST_*`, `CIN`/`CIR` all override DB on every boot (`run.py`).
- **Payment badges:** Stripe/SSL/GDPR/PCI badges were removed from the public frontend; only `custom_badge_*` images remain.
- **Docker vs Nixpacks:** Railway honours `Dockerfile`; `nixpacks.toml` (tesseract) is only the local Nixpacks fallback.
- **iCal default:** 30-minute sync; Booking feeds never produce blocks (only booked dates).
- **Bollo:** `€2` marca da bollo on the *ricevuta* when `stay_amount > 77.47`; stored per-receipt (`bollo_required`, `bollo_amount`, `bollo_id`, `bollo_image_path`).

---

## Related Docs

- `AGENTS.md` — agent/colleague handbook (commands, architecture, env, gotchas)
- `EMAILS.md` — all 10 email flows, triggers, templates
- `IMPROVEMENTS.md` — completed and open roadmap items
- `docs/MRZ_HANDOFF.md` — MRZ extraction design, benchmarks, and prototype notes

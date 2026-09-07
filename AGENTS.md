# AGENTS.md — Apt_Website

> Colleague handbook for humans and AI agents working on **Lotto 235 Garbatella** (CAV — *Casa per Vacanze*). Single-property Flask booking platform — Stripe + Brevo + Shelly/Nuki + Italian compliance (Questura / ROSS1000 / tourist tax / *ricevuta*).

## Quick start

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in at least SECRET_KEY + ADMIN_PASSWORD + Stripe + Brevo
python run.py          # http://localhost:5001  (or $PORT)
```

No CI. Lint/test are configured locally — see Commands.

## Commands

| Action | Command |
|---|---|
| Run dev server | `python run.py` (port `5001`, or `$PORT`) |
| Prod server | `gunicorn "app:create_app()" --access-logfile=- --error-logfile=- --log-level=info` (see `Procfile`) |
| Flask shell | `flask shell` |
| DB migrations | `flask db migrate -m "msg"` then `flask db upgrade` (also auto-runs on startup) |
| Compile translations | `pybabel compile -d translations` (auto-runs on startup via `app/__init__.py` + `run.py`) |
| Extract translations | `pybabel extract -F babel.cfg -o messages.pot .` then `pybabel update -i messages.pot -d translations` |
| Tests | `pytest` / `pytest -q` / `pytest tests/test_booking.py -v` |
| Lint | `ruff check .` · `ruff format .` |
| Health check | `curl http://localhost:5001/health` → `{"status":"ok","version":"1.0"}` |

## Architecture

- **App factory** — `app/__init__.py:create_app()` reads `config.Config`, configures logging (stdout + `app.log` rotation when not debug), inits `SQLAlchemy`/`Migrate`/`LoginManager`/`CSRFProtect`/`Bcrypt`/`Moment`/`Limiter` (`200/day`, `50/hour`, `memory://`)/`Babel`, auto-compiles translations + runs migrations, registers `routes.bp`, starts APScheduler.
- **Routes** — single blueprint `routes.bp` (`app/routes/__init__.py`) split across:
  - `public.py` — `/`, FAQ/policies, `/contact` (Brevo), `/ical/apartment.ics`, `/sitemap.xml`, `/robots.txt`, testimonials, language switch.
  - `booking.py` — `/reserve` → `/checkout` → `/process-payment` / `/checkout/create-session` (Stripe) → `/payment/success` + `/stripe/webhook`, `/booking/confirmed/<id>`, `/cancel/<token>`.
  - `admin.py` — `/admin` dashboard, calendar, pricing/coupons, earnings CSV, smart-access preview, boiler controls, Wi-Fi QR, trust badges/widgets, reservations/feeds/testimonials, access/keypad, audit/notifications.
  - `compliance.py` — `/admin/compliance*` (Questura, ROSS1000, tourist tax `;`-CSV, `ComplianceConfig`, check-in links/logs).
  - `api.py` — `/api/*` (coupon/price), `/api/access/{gate,door}/open` + cleaning, and token pages `/checkin/<token>`, `/access/<token>`, `/portal/<token>`, `/cleaning-access/<token>`, `/checkin-guide/<token>`.
  - `helpers.py` — `get_apartment()`, `calculate_dynamic_total()` / `apply_full_payment_discount()` / `calculate_city_tax()`, Brevo + Stripe balance/tourist-tax helpers, `send_balance_invoice_reminders()`.
- **Models** — `app/models.py` — `User` (bcrypt), `Apartment` (singleton + Shelly/Nuki/boiler/Wi-Fi/badges/CIN-CIR/host), `Reservation` (Questura docs + companions, access/keypad window `HH:MM` Rome→UTC, Stripe + tourist tax + billing, `is_block`/`source`/`external_uid`), `Earning` (Airbnb confirmation-code ledger), `CleaningAccess`, `ICalFeed`, `Coupon`, `Testimonial`, `ComplianceConfig` (Fernet), `AuditLog`, `Notification`, `QuesturaLog`/`Ross1000Log`, `Receipt` (`NN/YYYY`, bollo snapshot).
- **Services** — `app/services/` — `ical_sync.py` (OTA fetch/dedup/orphan-cancel/cleanup), `smart_lock.py` (Shelly cloud+local + Nuki Web + Keypad 2 lifecycle), `boiler.py` (second Shelly, gap-aware ON/OFF), `questura.py` / `ross1000.py` (SOAP `GenerateToken`→`Send`, 168-char schedine, `Tabella` lookups), `tourist_tax.py` (€6/night/adult, max 10, children 3-9 exempt), `email_service.py` (Brevo), `wifi_qr.py` (`segno`), `receipts.py`, `airbnb_earnings.py`, `mrz_extractor.py` + `mrz_cnn.py` (CNN ONNX + Tesseract dual-engine). See `docs/MRZ_HANDOFF.md`.
- **Tasks** — `app/tasks/compliance.py` — Celery tasks that also run synchronously (`run_daily_questura()` etc.); Beat schedule mirrors the APScheduler jobs.
- **Templates / Static** — Jinja2 in `app/templates/` (public, admin, email, policies, components) + `app/static/images/apartment/`.
- **i18n** — Flask-Babel, `babel.cfg` (`python` + `jinja2`), `translations/{en,it,de,fr,es}/LC_MESSAGES` — compiled on startup.

## Environment variables

See `config.py` and `.env.example` for the full list. Key vars:

- **Required:** `SECRET_KEY` (warns at `app/__init__.py:60` if insecure), `ADMIN_PASSWORD` (creates/updates `admin` user on every boot in `run.py`).
- **DB:** `DATABASE_URL` (defaults to `sqlite:///app.db` in `instance/`; `postgres://` → `postgresql://` in `config.py:17`; retried 5× in `run.py`).
- **Email:** `MAIL_USERNAME`/`MAIL_PASSWORD` (Brevo **API key**, not SMTP) / `ADMIN_EMAIL`; sender is hardcoded `lotto235roma@gmail.com`.
- **Stripe:** `STRIPE_SECRET_KEY`/`STRIPE_PUBLISHABLE_KEY`/`STRIPE_WEBHOOK_SECRET`, `BASE_URL` for redirect URLs.
- **Compliance:** `CIN_CODE`/`CIR_CODE` (`IT058091C2TXZ44TA6` / `058091-LOC-19856`), `HOST_FULL_NAME`/`HOST_CODICE_FISCALE`/`HOST_ADDRESS`/`HOST_VAT_MODE`, `ROSS1000_*` (`ROSS1000_ENDPOINT` defaults to `https://lazioturismo.ross1000.it/ws/checkinV2`), `QUESTURA_*` (`QUESTURA_ENDPOINT` defaults to `https://alloggiatiweb.poliziadistato.it/service/service.asmx`; env overrides `ComplianceConfig` DB).
- **Smart access:** `SHELLY_CLOUD_SERVER`/`SHELLY_CLOUD_KEY`/`SHELLY_DEVICE_ID`, `SHELLY_BOILER_DEVICE_ID`/`SHELLY_BOILER_CHANNEL`/`SHELLY_BOILER_HOST`, `NUKI_SMARTLOCK_ID`/`NUKI_WEB_TOKEN`/`NUKI_WEB_BASE_URL`/`NUKI_UNLOCK_ACTION` (`unlatch` vs `unlock`) — all sync to `Apartment` on startup (`run.py`).
- **Wi-Fi:** `WIFI_SSID`/`WIFI_PASSWORD`/`WIFI_SECURITY`/`WIFI_BAND`/`WIFI_HIDDEN` (via `sync_wifi_from_env()`).
- **Other:** `ICAL_SYNC_INTERVAL_MINUTES` (30), `GTM_ID`/`GA4_ID`, `COMPLIANCE_ENCRYPTION_KEY` (Fernet; falls back to `SECRET_KEY[:32]` padded with `0` in `app/models.py:554` — avoid in production).

## Startup / seeding — `run.py`

1. `pybabel compile -d translations`.
2. `create_app()` → retries DB connect 5× (3s), `db.create_all()` + `upgrade()`.
3. Ensures `User(username='admin')` from `ADMIN_PASSWORD`.
4. Seeds `Apartment(name="Lotto 235 Garbatella")` when empty, then syncs Nuki / Wi-Fi / boiler / `HOST_*` / `CIN`/`CIR` from env (env wins on every boot).

## Scheduler — `app/__init__.py:_start_scheduler()`

Skipped when `WERKZEUG_RUN_MAIN=false`. All via `APScheduler` `BackgroundScheduler(daemon=True)`:

| ID | Trigger | Job |
|---|---|---|
| `ical_sync` | every `ICAL_SYNC_INTERVAL_MINUTES` | `sync_all_feeds()` |
| `balance_invoice_reminder` | daily 09:00 | `send_balance_invoice_reminders()` |
| `cleanup_external_reservations` | daily 04:30 | `cleanup_past_external_reservations()` |
| `revoke_expired_keypad_codes` | daily 12:00 | `revoke_expired_keypad_codes()` |
| `questura_daily_submission` | daily 08:00 | `run_daily_questura()` |
| `boiler_checkin_on` | daily 07:00 | `run_boiler_checkin_job()` (unconditional ON) |
| `boiler_checkout_off` | daily 16:00 | `run_boiler_checkout_job()` (OFF only if gap ≥2 days) |

Celery Beat equivalents live in `app/tasks/compliance.py:CELERY_BEAT_SCHEDULE`.

## Testing & linting

- **Tests:** `pyproject.toml: [tool.pytest.ini_options]` `testpaths = ["tests"]`, `python_files = ["test_*.py"]`. Suites: `test_booking`, `test_cancellation`, `test_external_reservations`, `test_questura`, `test_tourist_tax`. `tests/conftest.py:TestConfig` uses `sqlite:///:memory:`, `WTF_CSRF_ENABLED=False`, `MAIL_SUPPRESS_SEND=True`, `RATELIMIT_ENABLED=False`; fixtures `app`/`client`/`runner` + `login_admin(client)`.
- **Lint:** `pyproject.toml: [tool.ruff]` `target-version = "py311"`, `line-length = 120`, `lint.select = ["E","F","W","I","N","UP","SIM"]` (ignore `E501`), `format.quote-style = "single"`.

## Quirks & gotchas

- **Email uses Brevo REST API, not SMTP:** `MAIL_PASSWORD` is the Brevo API key; `Flask-Mail` is initialised but unused. Sender `lotto235roma@gmail.com` is hardcoded in `email_service.py` / `helpers.py` / `public.py`.
- **CSRF:** `WTF_CSRF_SSL_STRICT=False`, `WTF_CSRF_TIME_LIMIT=86400` (24h) in `config.py`.
- **Single apartment:** `Apartment.query.first()` is injected globally (`app/__init__.py:inject_apartment`); multi-property would require refactoring route assumptions.
- **Access window:** `Reservation.access_checkin_time`/`access_checkout_time` are `HH:MM` (Rome tz, default `13:00→13:00`, stored on the row). Guest access (`/access/<token>`, `/api/access/*`) is 403 outside the window; `/checkin-guide/<token>` is intentionally not. Nuki API windows are converted to UTC (`get_access_window_utc()`).
- **OTA vs direct:** `source` `direct`/`stripe` are site bookings; `airbnb`/`booking_com`/`vrbo` are OTA iCal imports. `is_block` distinguishes real stays vs calendar closures before `cleanup_past_external_reservations()` hard-deletes past blocks (repairs legacy `booking_com` blocks).
- **Rate limiting:** `Limiter(storage_uri='memory://', default_limits=['200 per day','50 per hour'])` — per-process only; use Redis `storage_uri` for multi-worker correctness. Login additionally `10/min`.
- **iCal:** Booking.com feeds carry only booked dates (no blocks); other feeds are filtered by `_classify_event`. Empty feed never mass-cancels (fetch-error guard). Export at `GET /ical/apartment.ics`.
- **Translations:** `.mo` files are built automatically at startup; supported `en,it,de,fr,es` (`app/__init__.py:get_locale` via `?lang=` → session → `Accept-Language` → `en`).
- **Bollo:** `€2` marca da bollo on the *ricevuta* when `stay_amount > 77.47` (`Receipt.bollo_required/bollo_amount/bollo_id`).
- **Port:** `5001`, not Flask's default `5000` (override via `$PORT`).
- **Docker vs Nixpacks:** Railway honours `Dockerfile` (`python:3.13-slim` + `tesseract-ocr` + `ocrb_int`); `nixpacks.toml` is the local fallback only.

## Directory layout

```
run.py                — entrypoint (translations, DB connect, admin/apartment seed, env sync)
config.py             — Config class (env → Flask config)
Procfile              — web: gunicorn "app:create_app()" --access-logfile=- --error-logfile=-
Dockerfile            — python:3.13-slim + tesseract + ocrb_int traineddata
babel.cfg             — pybabel extract config
pyproject.toml        — ruff + pytest config
requirements.txt
translations/         — Babel .po/.mo (en, it, de, fr, es)
migrations/           — Alembic / Flask-Migrate
instance/             — SQLite DB (local dev)
app/
  __init__.py         — create_app() factory, logging, Babel, context processors, scheduler
  models.py           — all SQLAlchemy models (~739 lines)
  forms.py            — WTForms (ReservationForm, LoginForm, ContactForm, ICalFeedForm, …)
  routes/
    __init__.py       — blueprint registration
    public.py         — homepage, policies, contact, iCal/sitemap/robots
    booking.py        — reserve→checkout→Stripe/wire→webhook→cancel
    admin.py          — dashboard, calendar, pricing/coupons, earnings, smart-access, boiler, wifi, badges, reservations/feeds/testimonials, keypad
    compliance.py     — Questura/ROSS1000/tourist-tax/config/check-in links
    api.py            — coupon/price APIs + token guest pages (checkin/access/portal/cleaning)
    helpers.py        — pricing, Brevo/Stripe helpers, balance invoices
  services/
    ical_sync.py      — OTA feed sync + orphan/cleanup
    smart_lock.py     — Shelly (cloud+local) + Nuki (+Keypad 2) — app/services/smart_lock.py
    boiler.py         — second Shelly gap-aware ON/OFF — app/services/boiler.py
    questura.py       — AlloggiatiWeb SOAP — app/services/questura.py
    ross1000.py       — Regione Lazio SOAP — app/services/ross1000.py
    tourist_tax.py    — Roma tourist tax — app/services/tourist_tax.py
    email_service.py  — Brevo senders — app/services/email_service.py
    wifi_qr.py        — WIFI: payload + QR (segno)
    receipts.py       — ricevuta fiscale helpers
    airbnb_earnings.py — earnings CSV parser
    mrz_extractor.py + mrz_cnn.py + models/ — MRZ dual-engine OCR
  tasks/
    compliance.py     — Celery tasks (also sync): Questura/ROSS1000/tourist-tax/reminders
  templates/          — Jinja2 (public, admin, emails, policies, components)
  static/images/apartment/
docs/
  MRZ_HANDOFF.md      — MRZ design + benchmarks
EMAILS.md             — 10 email flows (triggers, templates)
IMPROVEMENTS.md       — roadmap / done items
tests/
  conftest.py         — TestConfig + fixtures
  test_booking.py / test_cancellation.py / test_external_reservations.py
  test_questura.py / test_tourist_tax.py
```

## Related docs

- `README.md` — comprehensive project overview (features, tech stack, flows, deployment, troubleshooting).
- `EMAILS.md` — email flows, triggers, templates, subjects.
- `IMPROVEMENTS.md` — completed and open roadmap items.
- `docs/MRZ_HANDOFF.md` — MRZ extraction design, benchmarks, prototype notes.

# Apt_Website — Improvement Opportunities

## Code Quality & Testing
- **Tests** — ✅ **DONE** (38 tests across 3 test suites covering booking flow, Questura model, tourist tax calculation; test config uses in-memory SQLite with disabled CSRF/rate limiting; `conftest.py` seeds admin + apartment).
- **Linter/formatter** — ✅ **DONE** (ruff configured in `pyproject.toml` with `ruff check` and `ruff format`; runs on `app/` and `tests/`; 20 pre-existing warnings remain, mostly multi-statement lines and bare excepts).
- **Type hints** — ✅ **DONE** (added to all models, forms, route handlers, and key service methods using `Optional`, `Response | str`, `-> None`, etc.).
- **No CI** — no GitHub Actions or other pipeline. Every deploy is a manual gamble.
- **Hardcoded secrets in config** — `SECRET_KEY` has a fallback `'dev-only-change-in-production'` which could ship to prod.

## Security
- **ComplianceConfig encryption uses SECRET_KEY as fallback** — if `COMPLIANCE_ENCRYPTION_KEY` is not set, Questura passwords are encrypted with the same key used for session signing.
- **Rate limiting on login** — ✅ **DONE** (50/hour, 200/day per IP via Flask-Limiter).
- **Audit logging** — ✅ **DONE** (`AuditLog` model, automatic logging on admin actions, filtered viewer at `/admin/audit-log`).
- **WTForms CSRF** — ✅ **DONE** (manual `{% csrf_token() %}` replaced with `{{ form.hidden_tag() }}` on forms backed by FlaskForm; manual tokens retained for simple POST buttons in admin panels).

## Infrastructure
- **Migration management** — ✅ **DONE** (raw SQL schema patches removed from `__init__.py`; `coupon_code` was already in a migration; `tourist_tax_excluded` now has its own migration `20260728_add_tourist_tax_excluded.py` chained to head `c99797619e6f`).
- **Health check endpoint** — ✅ **DONE** (`/health` returns JSON with status, timestamp, DB connectivity).
- **Structured logging** — ✅ **DONE** (migrated from `print()` to `app.logger.info/warning/error`; startup, DB connection, and admin operations are logged).
- **No Docker compose for local dev** — if PostgreSQL is needed, there's no `docker-compose.yml`.

## Features
- **Booking calendar only shows static availability** — no real-time blocking from iCal feeds on the frontend.
- **No guest portal** — guests have no way to view/modify their booking without contacting the host.
- **No payment automation** — Stripe integration exists but there's no automated payment collection flow (deposit/full payment).
- **Automated review requests** — ✅ **DONE** (admin can send individual or bulk review request emails to past guests).
- **No multi-apartment support** — though `Apartment` is a model, the app assumes a single property everywhere.

## Maintenance
- **No backup strategy** — database backups are not configured or documented.
- **Translation coverage unknown** — `.po` files exist but no check for missing translations across templates.
- **Stale routes cleaned** — ✅ **DONE** (`admin_guest_data` route was dead — now handled by compliance dashboard; Messages and Cleaning feature sets removed per user request).

## Host/User Experience

### Guest Portal
The app has the bones of a self check-in (token-based links) but no proper guest-facing experience:
- **No booking management** — guests receive a confirmation email but can't log in to view/modify/cancel their booking. A simple token-authenticated page (`/booking/<token>`) could show their details and let them update guest info for Questura.
- **No digital welcome guide** — a single page with WiFi credentials, house rules, checkout instructions, local recommendations, and emergency contacts, sent before arrival.
- **No automated pre-arrival sequence** — a series of timed emails/SMS: confirmation, pre-arrival (3 days before) with check-in instructions, day-of with access details.
- **No post-stay engagement** — automated email after checkout asking for a review, with direct links to Google/Booking/Airbnb.
- **No WhatsApp integration** — the `whatsapp_number` field exists on the `Apartment` model but isn't used anywhere in the flow. A "Contact on WhatsApp" button during booking and post-confirmation would reduce friction.
- **Multi-language polish** — some UI strings fall back to English/Italian inconsistently; the guest can't easily switch language mid-flow.

### Host Dashboard
The admin panel covers compliance and configurations but lacks operational tools:
- **Metrics/analytics dashboard** — ✅ **DONE** (home dashboard shows occupancy rate, monthly/YT revenue, booking source breakdown, upcoming check-ins/outs, pending Questura submissions).
- **Notification system** — ✅ **DONE** (`Notification` model, admin alerts page with read/unread, nav badge counter, auto-created on bookings/cancellations).
- **iCal feed visibility** — ✅ **DONE** (admin page at `/admin/ical-feeds` shows blocked dates with OTA source).
- **Mobile responsiveness** — ✅ **DONE** (added responsive CSS: `table-responsive` wrappers, stacked cards on mobile, improved padding).
- **Bulk operations** — ✅ **DONE** (bulk pricing update for date ranges via `/admin/pricing/bulk`).

### Booking Flow UX
- **No real-time availability calendar** — the booking page shows a static form but doesn't visually block dates already taken by iCal imports. Guests can submit and get rejected later.
- **No price summary before contact** — the booking flow requires contacting the host first. A real-time quote (nights × rate + cleaning + tax) before form submission would reduce abandonment.
- **Deposit/partial payment** — ✅ **DONE** (30% deposit option via Stripe; guest can choose full or deposit payment at checkout).
- **No coupon/promotion visibility** — coupons exist in the admin but guests never see a discount field or promo banner.

## 2026-09-30 — Session changes (admin + email + iCal)

- **Gmail SMTP replaces Brevo** — ✅ **DONE** (`config.py:22` `MAIL_SERVER=smtp.gmail.com:587` `MAIL_USE_TLS=True` env-driven; `app/routes/helpers.py:104` `_send_brevo_email()` now uses `Flask-Mail` `Message`/`mail.send()`; `app/services/email_service.py:1` `_smtp_send()` for check-in/access/admin notifications; `app/routes/public.py:104` contact, `app/routes/compliance.py:452` check-in link, `app/routes/admin.py:1414` access link now via Gmail SMTP; `Flask-Mail` was already in `requirements.txt:10` but unused; `.env.example:10` documents `MAIL_SERVER/PORT/USE_TLS/USERNAME/PASSWORD/DEFAULT_SENDER`; Railway vars `MAIL_USERNAME=lotto235roma@gmail.com` + 16-char App Password).
- **Questura single recoverable page** — ✅ **DONE** (`app/routes/compliance.py:90` `questura_list()` now `?q=` search + `?form=all|compiled|not_compiled|ready|not_ready` filter; `app/templates/admin_questura.html:1` rebuilt table with guest email/docs/companions/form badges, stats cards `All/Compiled/Not compiled/Ready` + green `Recently compiled` section `compiled_recent` 5 rows, link to full data `questura_guest_data` `app/routes/compliance.py:147`; guest-message page `app/templates/admin_guest_message.html:144` adds `Questura — Guest Form` section with direct link `/admin/compliance/questura/<id>/guest-data`).
- **Compliance hub simplified** — ✅ **DONE** (`app/templates/admin_compliance.html:1` now only 2 cards: Questura Guest Forms → `?status=all` and Tourist Tax → `/tourist-tax` + `/tourist-tax/paid`; removed ROSS1000/quick-actions/needing_data/config_status sections per request).
- **Tourist tax paid-only report** — ✅ **DONE** (`app/routes/compliance.py:202` `tourist_tax()` supports `?paid_only=1` + new `app/routes/compliance.py:227` `tourist_tax_paid()` at `GET /admin/compliance/tourist-tax/paid`; `app/routes/compliance.py:266` `tourist_tax_export` `?paid_only=1` filtered CSV; `app/templates/admin_tourist_tax.html:70` filter dropdown + Paid report toggle + `Paid-only` badge).
- **Booking.com iCal past/in-house fix** — ✅ **DONE** (`app/services/ical_sync.py:293` orphan cancellation now `Reservation.check_in > today` only — `check_in <= today` (arrived/in-house) or `check_out < today` (past) never auto-cancelled when vanished from feed; fixes `Booking Guest (…)` history showing `cancelled` after checkout requiring manual restore; verified 97 tests pass).
- **Questura Compiled section** — ✅ **DONE** (`app/routes/compliance.py:122` counts `total/compiled/not_compiled/ready` + `compiled_recent`; `admin_questura.html:22` 4 clickable stat cards + green `Recently compiled` table).

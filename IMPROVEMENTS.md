# Apt_Website — Improvement Opportunities

## Code Quality & Testing
- **No tests** — zero test files. Critical paths (booking flow, Questura submission, tourist tax calculation) have no coverage.
- **No linter/formatter** — no ruff, black, or flake8 config. Code style is inconsistent.
- **No type hints** — functions lack type annotations, making refactoring risky.
- **No CI** — no GitHub Actions or other pipeline. Every deploy is a manual gamble.
- **Hardcoded secrets in config** — `SECRET_KEY` has a fallback `'dev-only-change-in-production'` which could ship to prod.

## Security
- **ComplianceConfig encryption uses SECRET_KEY as fallback** — if `COMPLIANCE_ENCRYPTION_KEY` is not set, Questura passwords are encrypted with the same key used for session signing.
- **Rate limiting on login** — ✅ **DONE** (50/hour, 200/day per IP via Flask-Limiter).
- **Audit logging** — ✅ **DONE** (`AuditLog` model, automatic logging on admin actions, filtered viewer at `/admin/audit-log`).
- **WTForms CSRF exempt on many forms** — some forms use `{% csrf_token() %}` manually instead of Flask-WTF `form.hidden_tag()`, making it easy to forget.

## Infrastructure
- **No migration management** — schema changes are patched via raw SQL in `run.py`. Flask-Migrate exists but isn't consistently used.
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

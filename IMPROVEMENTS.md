# Apt_Website — Improvement Opportunities

## Code Quality & Testing
- **No tests** — zero test files. Critical paths (booking flow, Questura submission, tourist tax calculation) have no coverage.
- **No linter/formatter** — no ruff, black, or flake8 config. Code style is inconsistent.
- **No type hints** — functions lack type annotations, making refactoring risky.
- **No CI** — no GitHub Actions or other pipeline. Every deploy is a manual gamble.
- **Hardcoded secrets in config** — `SECRET_KEY` has a fallback `'dev-only-change-in-production'` which could ship to prod.

## Security
- **ComplianceConfig encryption uses SECRET_KEY as fallback** — if `COMPLIANCE_ENCRYPTION_KEY` is not set, Questura passwords are encrypted with the same key used for session signing.
- **No rate limiting on login** — brute-force protection is missing on the admin login endpoint.
- **No audit logging** — admin actions (config changes, badge updates, price changes) are not logged.
- **WTForms CSRF exempt on many forms** — some forms use `{% csrf_token() %}` manually instead of Flask-WTF `form.hidden_tag()`, making it easy to forget.

## Infrastructure
- **No migration management** — schema changes are patched via raw SQL in `run.py`. Flask-Migrate exists but isn't consistently used.
- **No health check endpoint** — `/health` or similar for load balancer/probe.
- **No structured logging** — uses `print()` for startup and `app.logger` sparsely. No log aggregation setup.
- **No Docker compose for local dev** — if PostgreSQL is needed, there's no `docker-compose.yml`.

## Features
- **Booking calendar only shows static availability** — no real-time blocking from iCal feeds on the frontend.
- **No guest portal** — guests have no way to view/modify their booking without contacting the host.
- **No payment automation** — Stripe integration exists but there's no automated payment collection flow (deposit/full payment).
- **No automated review requests** — could email guests post-checkout asking for a review.
- **No multi-apartment support** — though `Apartment` is a model, the app assumes a single property everywhere.

## Maintenance
- **No backup strategy** — database backups are not configured or documented.
- **Translation coverage unknown** — `.po` files exist but no check for missing translations across templates.
- **Deprecated routes are still registered** — the old monolithic `routes.py` file was removed from git but some stale route references may remain.

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
- **No metrics/analytics dashboard** — key numbers are scattered across pages. A home dashboard could show: occupancy rate (current month), revenue (monthly/YT), booking source breakdown (direct vs OTA), upcoming check-ins/outs, pending Questura submissions, unread inquiries.
- **No notification system** — the host has to manually refresh pages. In-app notifications for new bookings, booking modifications, cancellations, Questura errors, or tourist tax deadlines would help.
- **No unified inbox** — inquiries come via the contact form but there's no way to manage guest communications in one place.
- **No cleaning/task scheduler** — no way to track cleaning status between checkouts and check-ins, or assign tasks.
- **No iCal feed visibility** — imported iCal blocks are in the DB but there's no admin view showing which dates are blocked and from which OTA.
- **Mobile responsiveness** — some admin tables overflow on mobile; the smart access and compliance pages could use a mobile-first pass.
- **Bulk operations** — no way to update pricing or availability for a date range at once; each change goes through individual forms.

### Booking Flow UX
- **No real-time availability calendar** — the booking page shows a static form but doesn't visually block dates already taken by iCal imports. Guests can submit and get rejected later.
- **No price summary before contact** — the booking flow requires contacting the host first. A real-time quote (nights × rate + cleaning + tax) before form submission would reduce abandonment.
- **No deposit/partial payment** — Stripe is wired up but unused. Offering a 30% deposit at booking time (with auto-collection) would reduce no-shows.
- **No coupon/promotion visibility** — coupons exist in the admin but guests never see a discount field or promo banner.

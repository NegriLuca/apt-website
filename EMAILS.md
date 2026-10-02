# Apt_Website — Email Communications

All guest emails are sent via **Brevo REST API** (`POST https://api.brevo.com/v3/smtp/email`).
Sender: `MAIL_DEFAULT_SENDER` env var (must be a validated sender in Brevo, default `lotto235roma@gmail.com`).

> **Rule: Brevo is guests-only.** Admin-side notifications never use email — they go to **Slack** (`SLACK_WEBHOOK_URL` incoming webhook, `app/services/slack.py`) and, for the contact form, to the in-app admin inbox (`Notification`, `/admin/notifications`).

---

## 1. Booking Confirmation (Stripe — paid upfront)

**When:** After successful Stripe payment (webhook or `payment_success` redirect)
**Trigger:** `_send_confirmation_emails()` in `app/routes/helpers.py:89`
**Template (guest):** `email_confirmation.html`
**Admin:** Slack ping `🔔 New booking` with reservation summary + admin link (no email)

| Recipient | Subject | Content |
|---|---|---|
| **Guest** | `Booking confirmation — Lotto 235 Garbatella` | Booking summary (dates, nights, total), cancel link, payment status |

---

## 2. Pending Payment Confirmation (Wire Transfer)

**When:** Guest chooses "Bank Transfer" on checkout
**Trigger:** `send_pending_payment_email()` in `app/routes/helpers.py:165`
**Template (guest):** `email_pending_payment.html`
**Admin:** Slack ping `🔔 New booking (wire-transfer, pending payment)` (no email)

| Recipient | Subject | Content |
|---|---|---|
| **Guest** | `Booking received — Lotto 235 Garbatella` | Booking summary, check-in link, cancel link, payment instructions |

---

## 3. Payment Verified (after wire transfer confirmed by admin)

**When:** Admin marks payment as received
**Trigger:** `send_payment_verified_email()` in `app/routes/helpers.py:132`
**Template (guest):** `email_payment_verified.html`
**Admin:** Slack ping `✅ Payment confirmed` (no email)

| Recipient | Subject | Content |
|---|---|---|
| **Guest** | `✅ Pagamento Verificato e Confermato — #{id}` | Payment confirmed, booking is now active |

---

## 4. Cancellation (guest or admin)

**When:** Guest cancels via cancel link OR admin cancels from dashboard
**Trigger:** `send_cancellation_emails()` in `app/routes/helpers.py:213`
**Template (guest):** `email_cancellation.html`
**Admin:** Slack ping `❌ Cancelled` with refund info (no email)

| Recipient | Subject | Content |
|---|---|---|
| **Guest** | `Your reservation has been cancelled — Lotto 235 Garbatella` | Cancellation notice, refund percentage/amount, refund failure warning if applicable |

---

## 5. Check-in Link (manual send from admin)

**When:** Admin clicks "Send Check-in Link" on compliance dashboard
**Trigger:** `send_checkin_link()` in `app/routes/compliance.py:300`
**Template (guest):** `email_checkin_link.html`

| Recipient | Subject | Content |
|---|---|---|
| **Guest** | `Check-in Link — Lotto 235 Garbatella` | Link to complete online check-in (Questura data) |

---

## 6. Check-in Email (automated via email_service)

**When:** Called programmatically (e.g. after reservation created with Stripe)
**Trigger:** `send_checkin_email()` in `app/services/email_service.py:12`
**Template (guest):** `email_guest_checkin.html`

| Recipient | Subject | Content |
|---|---|---|
| **Guest** | `🔑 Completa il tuo Check-in Online — Lotto 235 Garbatella` | Check-in link for Questura data submission |

---

## 7. Access Link Email (automated via email_service)

**When:** Called programmatically
**Trigger:** `send_access_email()` in `app/services/email_service.py:55`
**Template (guest):** `email_guest_access.html`

| Recipient | Subject | Content |
|---|---|---|
| **Guest** | `🔑 Il tuo Accesso Gate & Porta — Lotto 235 Garbatella` | Gate and door access links for smart lock |

---

## 8. Admin Check-in Notification (Slack-only, no email)

**When:** Guest completes online check-in form
**Trigger:** `send_admin_checkin_notification()` in `app/services/email_service.py`
**Admin:** Slack ping `🔑 Guest check-in completed` with reservation summary + admin link

---

## 9. Review Request (manual + bulk from admin)

**When:** Admin clicks "Send Review Request" on dashboard or "Send Bulk" button
**Trigger:** `admin_send_review_request()` / `admin_send_review_requests_bulk()` in `app/routes/admin.py`
**Template (guest):** `email_review_request.html`

| Recipient | Subject | Content |
|---|---|---|
| **Guest** | `How was your stay? Leave a review!` | Link to submit a testimonial on the site |

---

## 10. Contact Form Inquiry (inbox + Slack, no email)

**When:** Guest submits contact form at `/contact`
**Trigger:** `contact()` in `app/routes/public.py`
**Admin:** in-app inbox entry (`Notification`, category `contact`, badge in admin header) + Slack ping `📬 Contact form`

---

## Summary Table

| # | Email | Recipient | Trigger | Template |
|---|---|---|---|---|
| 1 | Booking Confirmation | Guest (email) + Admin (Slack) | Stripe payment success | `email_confirmation.html` |
| 2 | Pending Payment | Guest (email) + Admin (Slack) | Wire transfer chosen | `email_pending_payment.html` |
| 3 | Payment Verified | Guest (email) + Admin (Slack) | Admin confirms payment | `email_payment_verified.html` |
| 4 | Cancellation | Guest (email) + Admin (Slack) | Cancel link or admin action | `email_cancellation.html` |
| 5 | Check-in Link | Guest | Admin manual send | `email_checkin_link.html` |
| 6 | Check-in Email | Guest | Automated after booking | `email_guest_checkin.html` |
| 7 | Access Link | Guest | Automated | `email_guest_access.html` |
| 8 | Check-in Notification | Admin (Slack only) | Guest completes check-in | — (Slack `app/services/slack.py`) |
| 9 | Review Request | Guest | Admin manual/bulk | `email_review_request.html` |
| 10 | Contact Inquiry | Admin (inbox + Slack) | Contact form submission | — (no email) |

**Notes:**
- All templates exist and are verified. No missing email files.

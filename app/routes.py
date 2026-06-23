from flask import (
    Blueprint, Response, render_template, redirect, url_for,
    flash, request, current_app, session, abort
)
from app.forms import ReservationForm, LoginForm, ContactForm, ICalFeedForm
from app.models import Reservation, User, Apartment, ICalFeed
from app.services.ical_sync import sync_all_feeds
from app import db, mail, csrf
from flask_login import login_user, logout_user, login_required, current_user
from flask_mail import Message
from datetime import datetime, date, timedelta
import secrets
from sqlalchemy.exc import IntegrityError
import stripe

bp = Blueprint('routes', __name__)


# ── Helpers ───────────────────────────────────────────────────────────────────

def get_apartment():
    return Apartment.query.first()


def is_available(check_in, check_out):
    conflicts = Reservation.query.filter(
        Reservation.status == "confirmed",
        Reservation.check_in < check_out,
        Reservation.check_out > check_in
    ).count()
    return conflicts == 0


def _send_confirmation_emails(reservation):
    """Send booking confirmation to guest and host."""
    cancel_url = url_for('routes.cancel_reservation', token=reservation.cancel_token, _external=True)
    apt = get_apartment()
    nights = reservation.nights
    total = reservation.total_price
    payment_summary = get_payment_summary(reservation) # Use your new helper

    sender_email = current_app.config.get('MAIL_USERNAME', 'lotto235roma@gmail.com')

    # 1. Guest Email
    mail.send(Message(
        subject=f"Booking confirmation — {apt.name if apt else 'My Apartment'}",
        sender=sender_email,
        recipients=[reservation.guest_email],
        html=render_template(
            'email_confirmation.html',
            reservation=reservation,
            cancel_url=cancel_url,
            nights=nights,
            total=total,
            apartment=apt,
            payment_summary=payment_summary # PASS THIS TO THE TEMPLATE
        )
    ))

    # 2. Admin Email (Updated to include payment status)
    mail.send(Message(
        subject="New booking received",
        sender=sender_email,
        recipients=[current_app.config['ADMIN_EMAIL']],
        body=(
            f"New booking details:\n\n"
            f"Guest: {reservation.guest_name}\n"
            f"Dates: {reservation.check_in} → {reservation.check_out} ({nights} nights)\n"
            f"{payment_summary}\n"
            f"Status: {reservation.payment_status.upper()}\n"
        )
    ))

# ── Public pages ──────────────────────────────────────────────────────────────

@bp.route('/')
def home():
    apartment = get_apartment()
    return render_template('home.html', apartment=apartment)


@bp.route('/apartment')
def apartment():
    apartment = get_apartment()
    return render_template('apartment.html', apartment=apartment)


@bp.route('/faq')
def faq():
    return render_template('faq.html')


@bp.route('/food_recommendations')
def food_recommendations():
    return render_template('food_recommendations.html')


@bp.route('/attractions')
def attractions():
    return render_template('attractions.html')


# ── Reservation / booking flow ────────────────────────────────────────────────

@bp.route('/reserve', methods=['GET', 'POST'])
def reserve():
    apartment = get_apartment()
    form = ReservationForm()

    reservations = Reservation.query.filter(
        Reservation.status != 'cancelled'
    ).all()

    disabled_dates = []
    for r in reservations:
        current = r.check_in
        last_night = r.check_out - timedelta(days=1)
        while current <= last_night:
            disabled_dates.append(current.isoformat())
            current += timedelta(days=1)

    if form.validate_on_submit():
        check_in  = form.check_in.data
        check_out = form.check_out.data

        if check_out <= check_in:
            flash("Check-out must be after check-in.", "danger")
            return redirect(request.url)

        nights = (check_out - check_in).days
        if nights > 28:
            flash("You can book a maximum of 28 nights.", "danger")
            return redirect(request.url)

        # Clear any old traces from memory before applying validation loops
        session.pop('pending_reservation', None)

        if not is_available(check_in, check_out):
            flash('Selected dates are not available.', 'danger')
            return redirect(request.url)

        # Store booking details in session and redirect to payment
        session['pending_reservation'] = {
            'guest_name':  form.guest_name.data,
            'guest_email': form.guest_email.data,
            'check_in':    check_in.isoformat(),
            'check_out':   check_out.isoformat(),
            'num_guests':  form.num_guests.data,
        }
        return redirect(url_for('routes.checkout'))

    return render_template(
        'reservation.html',
        form=form,
        apartment=apartment,
        disabled_dates=disabled_dates,
    )


@bp.route('/checkout')
def checkout():
    pending = session.get('pending_reservation')
    if not pending:
        flash('Please fill in the booking form first.', 'warning')
        return redirect(url_for('routes.reserve'))

    apartment = get_apartment()
    check_in  = date.fromisoformat(pending['check_in'])
    check_out = date.fromisoformat(pending['check_out'])
    nights    = (check_out - check_in).days
    total     = nights * apartment.price_per_night if apartment else 0

    stripe_pub = current_app.config.get('STRIPE_PUBLISHABLE_KEY', '')

    return render_template(
        'checkout.html',
        pending=pending,
        apartment=apartment,
        nights=nights,
        total=total,
        stripe_publishable_key=stripe_pub,
        check_in=check_in,
        check_out=check_out,
    )


@bp.route('/checkout/create-session', methods=['POST'])
def create_checkout_session():
    """Create a Stripe Checkout session and redirect to Stripe."""
    pending = session.get('pending_reservation')
    if not pending:
        flash('Session expired. Please start again.', 'warning')
        return redirect(url_for('routes.reserve'))

    apartment = get_apartment() 
    check_in  = date.fromisoformat(pending['check_in'])
    check_out = date.fromisoformat(pending['check_out'])
    nights    = (check_out - check_in).days
    total_cents = int(nights * apartment.price_per_night * 100) if apartment else 0

    if not is_available(check_in, check_out):
        session.pop('pending_reservation', None)
        flash('Sorry, those dates were just booked. Please choose again.', 'danger')
        return redirect(url_for('routes.reserve'))

    try:
        stripe.api_key = current_app.config['STRIPE_SECRET_KEY']
        base_url = current_app.config.get('BASE_URL', request.host_url.rstrip('/'))

        checkout_session = stripe.checkout.Session.create(
            payment_method_types=['card'],
            line_items=[{
                'price_data': {
                    'currency': 'eur',
                    'unit_amount': total_cents,
                    'product_data': {
                        'name': f"{apartment.name if apartment else 'Apartment'} — {nights} night{'s' if nights != 1 else ''}",
                        'description': f"Check-in: {check_in}  /  Check-out: {check_out}",
                    },
                },
                'quantity': 1,
            }],
            mode='payment',
            customer_email=pending['guest_email'],
            success_url=base_url + url_for('routes.payment_success') + '?session_id={CHECKOUT_SESSION_ID}',
            cancel_url=base_url + url_for('routes.checkout'),
            metadata={
                'guest_name':  pending['guest_name'],
                'guest_email': pending['guest_email'],
                'check_in':    pending['check_in'],
                'check_out':   pending['check_out'],
                'num_guests':  str(pending['num_guests']),
            }
        )
        return redirect(checkout_session.url, code=303)

    except Exception as exc:
        current_app.logger.error('Stripe error: %s', exc)
        flash('Payment provider error. Please try again later.', 'danger')
        return redirect(url_for('routes.checkout'))
    

@bp.route('/checkout/test-bypass', methods=['POST'])
@csrf.exempt
def test_bypass_booking():
    """Bypasses Stripe entirely and creates a confirmed booking for testing."""
    pending = session.get('pending_reservation')
    if not pending:
        flash('Session expired. Please start again.', 'warning')
        return redirect(url_for('routes.reserve'))

    check_in  = date.fromisoformat(pending['check_in'])
    check_out = date.fromisoformat(pending['check_out'])

    if not is_available(check_in, check_out):
        session.pop('pending_reservation', None)
        flash('Sorry, those dates were just booked.', 'danger')
        return redirect(url_for('routes.reserve'))

    reservation = Reservation(
        guest_name   = pending['guest_name'],
        guest_email  = pending['guest_email'],
        check_in     = check_in,
        check_out    = check_out,
        num_guests   = int(pending['num_guests']),
        status       = 'confirmed',
        source       = 'direct',
        cancel_token = secrets.token_urlsafe(32),
        stripe_payment_intent_id = f"test_bypass_{secrets.token_hex(8)}"
    )
    
    db.session.add(reservation)
    db.session.commit()
    
    try:
        _send_confirmation_emails(reservation)
        flash('Confirmation emails sent successfully!', 'info')
    except Exception as exc:
        current_app.logger.error('Failed to send confirmation email in test bypass: %s', exc)
        flash('Reservation saved, but email sending failed.', 'warning')

    session.pop('pending_reservation', None)
    return redirect(url_for('routes.booking_confirmed', reservation_id=reservation.id))


@bp.route('/payment/success')
def payment_success():
    """Stripe redirects here after a successful payment transaction."""
    session_id = request.args.get('session_id')
    reservation = None

    if session_id:
        try:
            stripe.api_key = current_app.config['STRIPE_SECRET_KEY']
            cs = stripe.checkout.Session.retrieve(session_id)
            
            # FIXED: Directly pull structural object property reference
            pi_id = cs.payment_intent

            if pi_id:
                reservation = Reservation.query.filter_by(
                    stripe_payment_intent_id=pi_id
                ).first()

            # Local transaction fallback processing link
            if not reservation and cs.payment_status == 'paid':
                reservation = _create_reservation_from_stripe(cs)

        except Exception as exc:
            current_app.logger.error('payment_success lookup error: %s', exc)

    session.pop('pending_reservation', None)
    return render_template('booking_confirmed.html', reservation=reservation)


def _create_reservation_from_stripe(cs) -> Reservation:
    """Safely build a database confirmed Reservation from a Stripe session."""
    data = cs.to_dict() if hasattr(cs, 'to_dict') else cs
    pi_id = data.get('payment_intent') or f"stripe_session_{data.get('id')}"
    
    existing = Reservation.query.filter_by(stripe_payment_intent_id=pi_id).first()
    if existing:
        return existing

    meta = data.get('metadata') or {}
    guest_name  = meta.get('guest_name', 'Guest')
    guest_email = data.get('customer_email') or meta.get('guest_email') or 'info@myapartment.com'
    
    # --- NIGHT COMPUTATION ---
    try:
        check_in  = date.fromisoformat(meta.get('check_in'))
        check_out = date.fromisoformat(meta.get('check_out'))
    except (TypeError, ValueError):
        pending = session.get('pending_reservation') or {}
        check_in  = date.fromisoformat(pending.get('check_in', date.today().isoformat()))
        check_out = date.fromisoformat(pending.get('check_out', (date.today() + timedelta(days=1)).isoformat()))
    
    # Calculate price safely
    apartment = get_apartment()
    nights = (check_out - check_in).days
    total_price = nights * (apartment.price_per_night if apartment else 0)
    # --------------------------

    reservation = Reservation(
        guest_name               = guest_name,
        guest_email              = guest_email,
        check_in                 = check_in,
        check_out                = check_out,
        num_guests               = int(meta.get('num_guests', 1)),
        status                   = 'confirmed',
        source                   = 'direct',
        cancel_token             = secrets.token_urlsafe(32),
        total_price              = total_price,
        payment_status           = 'paid' if data.get('payment_status') == 'paid' else 'unpaid',
        payment_method           = 'stripe',
        stripe_payment_intent_id = pi_id,
    )
    
    db.session.add(reservation)
    db.session.commit()
    
    try:
        _send_confirmation_emails(reservation)
    except Exception as exc:
        current_app.logger.error('Failed to send confirmation email: %s', exc)

    return reservation

@bp.route('/stripe/webhook', methods=['POST'])
@csrf.exempt
def stripe_webhook():
    payload = request.data
    sig_header = request.headers.get('Stripe-Signature')
    endpoint_secret = current_app.config['STRIPE_WEBHOOK_SECRET']

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, endpoint_secret)
    except Exception:
        return "Invalid signature", 400

    if event.type == 'checkout.session.completed':
        # Extract the dictionary representation directly
        cs = event.data.object
        data = cs.to_dict() if hasattr(cs, 'to_dict') else cs
        
        if data.get('payment_status') == 'paid':
            try:
                _create_reservation_from_stripe(cs)
            except Exception as e:
                current_app.logger.error("Error creating reservation: %s", e)
                return "Internal database error", 500

    return "Success", 200

@bp.route("/booking/confirmed/<int:reservation_id>")
def booking_confirmed(reservation_id):
    reservation = Reservation.query.get_or_404(reservation_id)
    return render_template("booking_confirmed.html", reservation=reservation)


@bp.route("/cancel/<token>")
def cancel_reservation(token):
    reservation = Reservation.query.filter_by(cancel_token=token).first_or_404()
    today = date.today()

    if reservation.status == "cancelled":
        return render_template(
            "cancellation_result.html",
            success=False,
            message="This reservation has already been cancelled."
        )

    if reservation.status != "confirmed":
        return render_template(
            "cancellation_result.html",
            success=False,
            message="This reservation cannot be cancelled."
        )

    if today >= reservation.check_in:
        return render_template(
            "cancellation_result.html",
            success=False,
            message="Cancellation is no longer possible after check-in."
        )

    refund_failed_warning = False
    if reservation.stripe_payment_intent_id:
        try:
            stripe.api_key = current_app.config['STRIPE_SECRET_KEY']
            stripe.Refund.create(
                payment_intent=reservation.stripe_payment_intent_id,
                reason='requested_by_customer'
            )
            current_app.logger.info(f"Stripe Refund issued for Guest Cancel: Res #{reservation.id}")
            
        except stripe.error.StripeError as e:
            current_app.logger.error(f"Stripe refund transaction failed: {str(e)}")
            refund_failed_warning = True

    reservation.status = "cancelled"
    db.session.commit()

    refund_notice = ""
    if refund_failed_warning:
        refund_notice = "\n\n⚠️ Note: There was a delay processing your automatic refund. Our team has been flagged to verify it manually."

    sender_email = current_app.config.get('MAIL_USERNAME', 'lotto235roma@gmail.com')

    mail.send(Message(
        subject="Your reservation has been cancelled",
        sender=sender_email,
        recipients=[reservation.guest_email],
        body=(
            f"Hello {reservation.guest_name},\n\n"
            f"Your reservation from {reservation.check_in} to {reservation.check_out} "
            f"has been successfully cancelled.{refund_notice}\n\n— My Apartment"
        )
    ))
    
    mail.send(Message(
        subject="Reservation cancelled" + (" [REFUND MANUAL CHECK REQUIRED]" if refund_failed_warning else ""),
        sender=sender_email,
        recipients=[current_app.config['ADMIN_EMAIL']],
        body=(
            f"Reservation cancelled:\n\n"
            f"Guest: {reservation.guest_name}\n"
            f"Dates: {reservation.check_in} → {reservation.check_out}\n"
            f"Stripe Refund Status: {'⚠️ FAILED / MANUAL CHECK REQUIRED' if refund_failed_warning else '✅ Fully Refunded Automatically'}\n"
        )
    ))

    final_ui_message = "Your reservation has been cancelled successfully."
    if not refund_failed_warning and reservation.stripe_payment_intent_id:
        final_ui_message += " A full refund has been issued back to your payment card."
    elif refund_failed_warning:
        final_ui_message += " Your dates are released, but there was an issue processing your automatic refund. We will review it manually."

    return render_template(
        "cancellation_result.html",
        success=True,
        message=final_ui_message
    )

def get_payment_summary(reservation):
    """Generates a human-readable payment summary for emails."""
    if reservation.payment_method == 'stripe':
        return f"Paid via Stripe (Total: €{reservation.total_price:.2f})"
    elif reservation.payment_method == 'iban':
        return f"Pending Bank Transfer (Total: €{reservation.total_price:.2f})"
    elif reservation.payment_method == 'cash':
        return f"Cash on Arrival (Total: €{reservation.total_price:.2f})"
    return f"Total: €{reservation.total_price:.2f}"

# ── Contact ───────────────────────────────────────────────────────────────────

@bp.route('/contact', methods=['GET', 'POST'])
def contact():
    form = ContactForm()
    if form.validate_on_submit():
        mail.send(Message(
            subject=f"New contact message from {form.name.data}",
            recipients=[current_app.config['ADMIN_EMAIL']],
            body=(
                f"Name: {form.name.data}\n"
                f"Email: {form.email.data}\n\n"
                f"Message:\n{form.message.data}"
            )
        ))
        flash('Your message has been sent successfully!', 'success')
        return redirect(url_for('routes.contact'))
    return render_template('contact.html', form=form)


# ── iCal export ───────────────────────────────────────────────────────────────

@bp.route("/ical/apartment.ics")
def export_ical():
    reservations = Reservation.query.filter(
        Reservation.status == "confirmed",
        Reservation.check_out > Reservation.check_in
    ).all()

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//My Apartment//Booking Calendar//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
    ]

    for r in reservations:
        lines.extend([
            "BEGIN:VEVENT",
            f"UID:{r.id}-{r.check_in}@myapartment",
            f"DTSTAMP:{datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')}",
            f"DTSTART;VALUE=DATE:{r.check_in.strftime('%Y%m%d')}",
            f"DTEND;VALUE=DATE:{r.check_out.strftime('%Y%m%d')}",
            "SUMMARY:Reserved",
            "END:VEVENT",
        ])

    lines.append("END:VCALENDAR")

    return Response(
        "\r\n".join(lines),
        mimetype="text/calendar; charset=utf-8",
        headers={
            "Content-Disposition": "inline; filename=apartment.ics",
            "Cache-Control": "no-cache",
        }
    )


# ── Admin dashboard ───────────────────────────────────────────────────────────

@bp.route('/admin')
@login_required
def admin_dashboard():
    if not current_user.is_admin:
        flash('Access denied.', 'danger')
        return redirect(url_for('routes.home'))

    status_filter = request.args.get('status', 'all')
    query = Reservation.query.order_by(Reservation.check_in.desc())
    if status_filter != 'all':
        query = query.filter(Reservation.status == status_filter)

    reservations = query.all()
    feeds        = ICalFeed.query.order_by(ICalFeed.source).all()

    stats = {
        'total':     Reservation.query.count(),
        'confirmed': Reservation.query.filter_by(status='confirmed').count(),
        'pending':   Reservation.query.filter_by(status='pending').count(),
        'cancelled': Reservation.query.filter_by(status='cancelled').count(),
    }

    return render_template(
        'admin_dashboard.html',
        reservations=reservations,
        feeds=feeds,
        stats=stats,
        status_filter=status_filter,
    )


@bp.route('/admin/reservations/<int:res_id>/confirm', methods=['POST'])
@login_required
def admin_confirm_reservation(res_id):
    if not current_user.is_admin:
        abort(403)
    reservation = Reservation.query.get_or_404(res_id)
    if reservation.status != 'pending':
        flash('Only pending reservations can be confirmed.', 'warning')
    else:
        reservation.status = 'confirmed'
        db.session.commit()
        flash(f'Reservation #{res_id} confirmed.', 'success')
    return redirect(url_for('routes.admin_dashboard'))


@bp.route('/admin/reservations/<int:res_id>/cancel', methods=['POST'])
@login_required
def admin_cancel_reservation(res_id):
    if not current_user.is_admin:
        abort(403)
    reservation = Reservation.query.get_or_404(res_id)
    if reservation.status == 'cancelled':
        flash('Reservation is already cancelled.', 'warning')
    else:
        refund_failed_warning = False
        if reservation.stripe_payment_intent_id and not reservation.stripe_payment_intent_id.startswith("test_bypass_"):
            try:
                stripe.api_key = current_app.config['STRIPE_SECRET_KEY']
                stripe.Refund.create(
                    payment_intent=reservation.stripe_payment_intent_id,
                    reason='requested_by_customer'
                )
                current_app.logger.info(f"Stripe Refund issued by Admin for Res #{reservation.id}")
            except stripe.error.StripeError as e:
                current_app.logger.error(f"Admin Stripe refund failed: {str(e)}")
                refund_failed_warning = True

        reservation.status = 'cancelled'
        db.session.commit()
        
        if refund_failed_warning:
            flash(f'Reservation #{res_id} cancelled locally, but Stripe refund failed.', 'warning')
        else:
            flash(f'Reservation #{res_id} cancelled and fully refunded successfully.', 'success')
            
    return redirect(url_for('routes.admin_dashboard'))


# ── iCal feed management ──────────────────────────────────────────────────────

@bp.route('/admin/feeds/add', methods=['GET', 'POST'])
@login_required
def add_feed():
    if not current_user.is_admin:
        abort(403)
    form = ICalFeedForm()
    if form.validate_on_submit():
        feed = ICalFeed(source=form.source.data, url=form.url.data, active=True)
        db.session.add(feed)
        db.session.commit()
        flash(f'Feed "{form.source.data}" added.', 'success')
        return redirect(url_for('routes.admin_dashboard'))
    return render_template('admin_feed_form.html', form=form, title='Add iCal Feed')


@bp.route('/admin/feeds/<int:feed_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_feed(feed_id):
    if not current_user.is_admin:
        abort(403)
    feed = ICalFeed.query.get_or_404(feed_id)
    form = ICalFeedForm(obj=feed)
    if form.validate_on_submit():
        feed.source = form.source.data
        feed.url    = form.url.data
        feed.active = form.active.data
        db.session.commit()
        flash('Feed updated.', 'success')
        return redirect(url_for('routes.admin_dashboard'))
    return render_template('admin_feed_form.html', form=form, title='Edit iCal Feed')


@bp.route('/admin/feeds/<int:feed_id>/delete', methods=['POST'])
@login_required
def delete_feed(feed_id):
    if not current_user.is_admin:
        abort(403)
    feed = ICalFeed.query.get_or_404(feed_id)
    db.session.delete(feed)
    db.session.commit()
    flash('Feed deleted.', 'success')
    return redirect(url_for('routes.admin_dashboard'))


@bp.route('/admin/feeds/sync', methods=['POST'])
@login_required
@csrf.exempt
def sync_feeds_now():
    if not current_user.is_admin:
        abort(403)
    added, removed, errors = sync_all_feeds()
    if errors:
        flash(f'Sync completed with errors: {"; ".join(errors)}', 'warning')
    else:
        flash(f'Sync complete — {added} added, {removed} cancelled.', 'success')
    return redirect(url_for('routes.admin_dashboard'))


# ── Auth ──────────────────────────────────────────────────────────────────────

@bp.route('/login', methods=['GET', 'POST'])
def login():
    form = LoginForm()
    if form.validate_on_submit():
        user = User.query.filter_by(username=form.username.data).first()
        if user and user.check_password(form.password.data):
            login_user(user, remember=form.remember.data)
            return redirect(url_for('routes.admin_dashboard'))
        else:
            flash('Login unsuccessful. Check username/password.', 'danger')
    return render_template('login.html', form=form)


@bp.route('/logout')
def logout():
    logout_user()
    return redirect(url_for('routes.home'))
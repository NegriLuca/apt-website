from flask import Blueprint, Response, render_template, redirect, url_for, flash, request, current_app
from app.forms import ReservationForm, LoginForm, ContactForm, ICalFeedForm
from app.models import Reservation, User, Apartment, ICalFeed
from app.services.ical_sync import sync_all_feeds
from app import db, mail
from flask_login import login_user, logout_user, login_required, current_user
from flask_mail import Message
from datetime import datetime, date, timedelta
import secrets
from sqlalchemy.exc import IntegrityError


bp = Blueprint('routes', __name__)

def get_apartment():
    return Apartment.query.first()

def is_available(check_in, check_out):
    conflicts = Reservation.query.filter(
        Reservation.status == "confirmed",
        Reservation.check_in < check_out,
        Reservation.check_out > check_in
    ).count()

    return conflicts == 0

# Public pages
@bp.route('/')
def home():
    apartment = get_apartment()
    return render_template('home.html', apartment=apartment)

@bp.route('/apartment')
def apartment():
    apartment = get_apartment()
    return render_template('apartment.html', apartment=apartment)


@bp.route('/reserve', methods=['GET', 'POST'])
def reserve():
    apartment = get_apartment()
    form = ReservationForm()

    # Get all active reservations
    reservations = Reservation.query.filter(
        Reservation.status != 'cancelled'
    ).all()

    # Build disabled dates list
    disabled_dates = []
    for r in reservations:
        current = r.check_in
        last_night = r.check_out - timedelta(days=1)

        while current <= last_night:
            disabled_dates.append(current.isoformat())
            current += timedelta(days=1)

    if form.validate_on_submit():
        if not is_available(form.check_in.data, form.check_out.data):
            flash('Selected dates are not available.', 'danger')
            return redirect(request.url)
        
        if form.check_out.data <= form.check_in.data:
            flash("Check-out must be after check-in.", "danger")
            return redirect(request.url)
        
        nights = (form.check_out.data - form.check_in.data).days

        if nights <= 0:
            flash("Check-out must be after check-in.", "danger")
            return redirect(request.url)

        if nights > 28:
            flash("You can book a maximum of 28 nights.", "danger")
            return redirect(request.url)

        try:
            conflict = Reservation.overlaps(
                form.check_in.data,
                form.check_out.data
            ).with_for_update().first()

            if conflict:
                flash("Those dates were just booked. Please choose another range.", "danger")
                return redirect(request.url)

            reservation = Reservation(
                guest_name=form.guest_name.data,
                guest_email=form.guest_email.data,
                check_in=form.check_in.data,
                check_out=form.check_out.data,
                num_guests=form.num_guests.data,
                status='confirmed',
                cancel_token=secrets.token_urlsafe(32)
            )
            db.session.add(reservation)
            db.session.commit()

        except IntegrityError:
            db.session.rollback()
            flash("Booking conflict detected. Please try again.", "danger")
            return redirect(request.url)

        cancel_url = url_for(
            'routes.cancel_reservation',
            token=reservation.cancel_token,
            _external=True
        )

        # Guest email
        msg = Message(
            subject="Booking confirmation",
            recipients=[reservation.guest_email],
            body=f"""
Hello {reservation.guest_name},

Your booking is confirmed 🎉

Check-in: {reservation.check_in}
Check-out: {reservation.check_out}
Guests: {reservation.num_guests}

Cancel your reservation:
{cancel_url}
— My Apartment
"""
        )
        mail.send(msg)

        # Admin email
        msg_admin = Message(
            subject="New booking received",
            recipients=[current_app.config['ADMIN_EMAIL']],
            body=f"""
New booking received:

Name: {reservation.guest_name}
Dates: {reservation.check_in} → {reservation.check_out}
Guests: {reservation.num_guests}
"""
        )
        mail.send(msg_admin)

        flash('Your booking has been confirmed!', 'success')
        return redirect(
            url_for('routes.booking_confirmed', reservation_id=reservation.id)
        )


    return render_template(
        'reservation.html',
        form=form,
        apartment=apartment,
        disabled_dates=disabled_dates
    )

@bp.route("/booking/confirmed/<int:reservation_id>")
def booking_confirmed(reservation_id):
    reservation = Reservation.query.get_or_404(reservation_id)

    return render_template(
        "booking_confirmed.html",
        reservation=reservation
    )

@bp.route("/cancel/<token>")
def cancel_reservation(token):
    reservation = Reservation.query.filter_by(cancel_token=token).first_or_404()

    today = date.today()

    # 1️⃣ Already cancelled → idempotent behavior
    if reservation.status == "cancelled":
        return render_template(
            "cancellation_result.html",
            success=False,
            message="This reservation has already been cancelled."
        )

    # 2️⃣ Only confirmed reservations can be cancelled
    if reservation.status != "confirmed":
        return render_template(
            "cancellation_result.html",
            success=False,
            message="This reservation cannot be cancelled."
        )

    # 3️⃣ Prevent cancellation after check-in day
    if today >= reservation.check_in:
        return render_template(
            "cancellation_result.html",
            success=False,
            message="Cancellation is no longer possible after check-in."
        )

    # ✅ Perform cancellation
    reservation.status = "cancelled"
    db.session.commit()

    # 📧 Email guest
    mail.send(Message(
        subject="Your reservation has been cancelled",
        recipients=[reservation.guest_email],
        body=f"""
Hello {reservation.guest_name},

Your reservation from {reservation.check_in} to {reservation.check_out}
has been successfully cancelled.

— My Apartment
"""
    ))

    # 📧 Email host
    mail.send(Message(
        subject="Reservation cancelled",
        recipients=[current_app.config['ADMIN_EMAIL']],
        body=f"""
Reservation cancelled:

Guest: {reservation.guest_name}
Dates: {reservation.check_in} → {reservation.check_out}
"""
    ))

    return render_template(
        "cancellation_result.html",
        success=True,
        message="Your reservation has been cancelled successfully."
    )


@bp.route('/contact', methods=['GET', 'POST'])
def contact():
    form = ContactForm()

    if form.validate_on_submit():
        msg = Message(
            subject=f"New contact message from {form.name.data}",
            recipients=[current_app.config['ADMIN_EMAIL']],
            body=f"""
Name: {form.name.data}
Email: {form.email.data}

Message:
{form.message.data}
"""
        )

        mail.send(msg)

        flash('Your message has been sent successfully!', 'success')
        return redirect(url_for('routes.contact'))

    return render_template('contact.html', form=form)

@bp.route('/faq')
def faq():
    return render_template('faq.html')


@bp.route('/recommendations')
def recommendations():
    return render_template('recommendations.html')

# ── Admin pages ───────────────────────────────────────────────────────────────

@bp.route('/admin')
@login_required
def admin_dashboard():
    if not current_user.is_admin:
        flash('Access denied.', 'danger')
        return redirect(url_for('routes.home'))
    reservations = Reservation.query.order_by(Reservation.check_in.desc()).all()
    feeds = ICalFeed.query.order_by(ICalFeed.source).all()
    return render_template('admin_dashboard.html', reservations=reservations, feeds=feeds)


# ── iCal feed management ──────────────────────────────────────────────────────

@bp.route('/admin/feeds/add', methods=['GET', 'POST'])
@login_required
def add_feed():
    if not current_user.is_admin:
        flash('Access denied.', 'danger')
        return redirect(url_for('routes.home'))
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
        flash('Access denied.', 'danger')
        return redirect(url_for('routes.home'))
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
        flash('Access denied.', 'danger')
        return redirect(url_for('routes.home'))
    feed = ICalFeed.query.get_or_404(feed_id)
    db.session.delete(feed)
    db.session.commit()
    flash('Feed deleted.', 'success')
    return redirect(url_for('routes.admin_dashboard'))


@bp.route('/admin/feeds/sync', methods=['POST'])
@login_required
def sync_feeds_now():
    if not current_user.is_admin:
        flash('Access denied.', 'danger')
        return redirect(url_for('routes.home'))
    added, removed, errors = sync_all_feeds()
    if errors:
        flash(f'Sync completed with errors: {"; ".join(errors)}', 'warning')
    else:
        flash(f'Sync complete — {added} added, {removed} cancelled.', 'success')
    return redirect(url_for('routes.admin_dashboard'))


# ── Reservation management ────────────────────────────────────────────────────

@bp.route('/admin/reservations/<int:res_id>/cancel', methods=['POST'])
@login_required
def admin_cancel_reservation(res_id):
    if not current_user.is_admin:
        flash('Access denied.', 'danger')
        return redirect(url_for('routes.home'))
    reservation = Reservation.query.get_or_404(res_id)
    if reservation.status == 'cancelled':
        flash('Reservation is already cancelled.', 'warning')
    else:
        reservation.status = 'cancelled'
        db.session.commit()
        flash(f'Reservation #{res_id} cancelled.', 'success')
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

@bp.route("/ical/apartment.ics")
def export_ical():
    reservations = Reservation.query.filter(
        Reservation.status == "confirmed",
        Reservation.check_out > Reservation.check_in  # 🔒 safety check
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
        "Cache-Control": "no-cache"
        }
    )


@bp.route('/logout')
def logout():
    logout_user()
    return redirect(url_for('routes.home'))
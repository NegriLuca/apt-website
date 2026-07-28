from flask import render_template, redirect, url_for, flash, request, current_app, abort, jsonify
from flask_babel import gettext as _
from flask_login import login_user, logout_user, login_required, current_user
from app.routes import bp
from app.routes.helpers import get_apartment, calculate_dynamic_total, get_payment_summary
from app import db
from app.models import Apartment, Reservation, User, ICalFeed, Coupon, Testimonial
from app.forms import LoginForm, ICalFeedForm
from datetime import datetime, date, timedelta
from sqlalchemy.exc import IntegrityError
import stripe
import secrets
import json


# ── Auth ─────────────────────────────────────────────────────────────────────


@bp.route('/login', methods=['GET', 'POST'])
def login():
    form = LoginForm()
    if form.validate_on_submit():
        user = User.query.filter_by(username=form.username.data).first()
        if user and user.check_password(form.password.data):
            login_user(user)
            flash('Welcome back!', 'success')
            return redirect(url_for('routes.admin_dashboard'))
        flash('Invalid username or password.', 'danger')
    return render_template('login.html', form=form)


@bp.route('/logout')
@login_required
def logout():
    logout_user()
    flash('You have been logged out.', 'info')
    return redirect(url_for('routes.home'))


# ── Admin Dashboard ──────────────────────────────────────────────────────────


@bp.route('/admin')
@login_required
def admin_dashboard():
    if not current_user.is_admin:
        abort(403)

    reservations = Reservation.query.order_by(Reservation.check_in.desc()).all()
    thirty_days_ago = date.today() - timedelta(days=30)
    recent_reservations = [r for r in reservations if r.created_at and r.created_at.date() >= thirty_days_ago]

    confirmed = sum(1 for r in reservations if r.status == 'confirmed')
    cancelled = sum(1 for r in reservations if r.status == 'cancelled')
    pending = sum(1 for r in reservations if r.status == 'pending')
    paid_stripe = sum(1 for r in reservations if r.payment_method == 'stripe' and r.payment_status == 'paid')
    paid_iban = sum(1 for r in reservations if r.payment_method == 'iban' and r.payment_status == 'paid')

    dashboard_data = {
        'total': len(reservations),
        'confirmed': confirmed,
        'cancelled': cancelled,
        'pending': pending,
        'paid_stripe': paid_stripe,
        'paid_iban': paid_iban,
        'recent': len(recent_reservations),
        'monthly_revenue': sum(r.total_price for r in reservations if r.status == 'confirmed' and r.check_in.month == date.today().month and r.check_in.year == date.today().year),
    }

    return render_template('admin_dashboard.html', reservations=reservations, data=dashboard_data)


@bp.route('/admin/calendar')
@login_required
def admin_calendar():
    if not current_user.is_admin:
        abort(403)
    return render_template('admin_calendar.html')


@bp.route('/admin/pricing', methods=['GET', 'POST'])
@login_required
def admin_pricing():
    if not current_user.is_admin:
        abort(403)

    apartment = Apartment.query.first()
    if not apartment:
        apartment = Apartment(price_per_night=130.00)
        db.session.add(apartment)
        db.session.commit()

    if request.method == 'POST':
        new_price = request.form.get('price_per_night')
        if new_price:
            try:
                apartment.price_per_night = float(new_price)
                db.session.commit()
                flash('Nightly base rate updated successfully!', 'success')
            except ValueError:
                flash('Invalid price format entered.', 'danger')
            return redirect(url_for('routes.admin_pricing'))

    all_coupons = Coupon.query.all()
    return render_template('admin_pricing.html', apartment=apartment, coupons=all_coupons)


@bp.route('/admin/smart-access', methods=['GET', 'POST'])
@login_required
def admin_smart_access():
    if not current_user.is_admin:
        abort(403)

    apartment = Apartment.query.first()
    if not apartment:
        apartment = Apartment(price_per_night=130.00)
        db.session.add(apartment)
        db.session.commit()

    if request.method == 'POST':
        apartment.shelly_enabled = bool(request.form.get('shelly_enabled'))
        apartment.shelly_host = request.form.get('shelly_host', '').strip() or None
        apartment.shelly_auth_key = request.form.get('shelly_auth_key', '').strip() or None
        apartment.shelly_relay_channel = request.form.get('shelly_relay_channel', type=int) or 0
        apartment.shelly_pulse_duration = request.form.get('shelly_pulse_duration', type=int) or 3

        apartment.nuki_enabled = bool(request.form.get('nuki_enabled'))
        apartment.nuki_smartlock_id = request.form.get('nuki_smartlock_id', '').strip() or None
        apartment.nuki_web_token = request.form.get('nuki_web_token', '').strip() or None
        apartment.nuki_web_base_url = request.form.get('nuki_web_base_url', '').strip() or 'https://api.nuki.io'
        apartment.nuki_unlock_action = request.form.get('nuki_unlock_action', 'unlock')

        apartment.whatsapp_number = request.form.get('whatsapp_number', '').strip() or None
        apartment.whatsapp_default_message = request.form.get('whatsapp_default_message', '').strip() or None

        db.session.commit()
        flash('Smart access settings updated successfully!', 'success')
        return redirect(url_for('routes.admin_smart_access'))

    return render_template('admin_smart_access.html', apartment=apartment)


@bp.route('/admin/trust-badges', methods=['GET', 'POST'])
@login_required
def admin_trust_badges():
    if not current_user.is_admin:
        abort(403)

    apartment = Apartment.query.first()
    if not apartment:
        apartment = Apartment(price_per_night=130.00)
        db.session.add(apartment)
        db.session.commit()

    if request.method == 'POST':
        apartment.booking_property_id = request.form.get('booking_property_id', '').strip() or None
        apartment.airbnb_listing_id = request.form.get('airbnb_listing_id', '').strip() or None
        apartment.google_place_id = request.form.get('google_place_id', '').strip() or None
        apartment.tripadvisor_location_id = request.form.get('tripadvisor_location_id', '').strip() or None
        apartment.vrbo_listing_id = request.form.get('vrbo_listing_id', '').strip() or None

        for i in [1, 2, 3]:
            setattr(apartment, f'custom_badge_{i}_image', request.form.get(f'custom_badge_{i}_image', '').strip() or None)
            setattr(apartment, f'custom_badge_{i}_link', request.form.get(f'custom_badge_{i}_link', '').strip() or None)
            setattr(apartment, f'custom_badge_{i}_alt', request.form.get(f'custom_badge_{i}_alt', '').strip() or None)

        apartment.show_reviews_in_footer = bool(request.form.get('show_reviews_in_footer'))
        apartment.show_reviews_on_homepage = bool(request.form.get('show_reviews_on_homepage'))
        apartment.show_reviews_on_booking = bool(request.form.get('show_reviews_on_booking'))

        apartment.booking_widget_js = request.form.get('booking_widget_js', '').strip() or None
        apartment.airbnb_widget_js = request.form.get('airbnb_widget_js', '').strip() or None
        apartment.google_widget_js = request.form.get('google_widget_js', '').strip() or None
        apartment.trustpilot_widget_js = request.form.get('trustpilot_widget_js', '').strip() or None

        db.session.commit()
        flash(_('Trust Badges & Widgets settings saved!'), 'success')
        return redirect(url_for('routes.admin_trust_badges'))

    return render_template('admin_trust_badges.html', apartment=apartment)


# ── Coupons ──────────────────────────────────────────────────────────────────


@bp.route('/admin/coupons/create', methods=['POST'])
@login_required
def admin_create_coupon():
    if not current_user.is_admin:
        abort(403)

    code = request.form.get('coupon_code', '').strip().upper()
    discount_type = request.form.get('discount_type', 'percentage')
    try:
        discount_value = float(request.form.get('discount_value', 0))
    except ValueError:
        flash('Discount value must be a valid number.', 'danger')
        return redirect(url_for('routes.admin_pricing'))

    if not code:
        flash('Voucher string code field cannot be empty.', 'danger')
        return redirect(url_for('routes.admin_pricing'))

    existing = Coupon.query.filter_by(code=code).first()
    if existing:
        flash('A coupon voucher with this exact string code already exists!', 'warning')
        return redirect(url_for('routes.admin_pricing'))

    new_coupon = Coupon(code=code, discount_type=discount_type, discount_value=discount_value, active=True)
    db.session.add(new_coupon)
    db.session.commit()

    flash(f'Promotional code "{code}" has been published successfully!', 'success')
    return redirect(url_for('routes.admin_pricing'))


@bp.route('/admin/coupons/<int:coupon_id>/delete', methods=['POST'])
@login_required
def admin_delete_coupon(coupon_id):
    if not current_user.is_admin:
        abort(403)

    coupon = Coupon.query.get_or_404(coupon_id)
    db.session.delete(coupon)
    db.session.commit()
    flash('Voucher deleted.', 'success')
    return redirect(url_for('routes.admin_pricing'))


# ── Reservations ─────────────────────────────────────────────────────────────


@bp.route('/admin/reservations/<int:res_id>/confirm', methods=['POST'])
@login_required
def admin_confirm_reservation(res_id):
    if not current_user.is_admin:
        abort(403)

    res = Reservation.query.get_or_404(res_id)
    res.status = 'confirmed'
    db.session.commit()
    flash(f'Reservation #{res_id} confirmed.', 'success')
    return redirect(url_for('routes.admin_dashboard'))


@bp.route('/admin/reservations/<int:res_id>/cancel', methods=['POST'])
@login_required
def admin_cancel_reservation(res_id):
    if not current_user.is_admin:
        abort(403)

    res = Reservation.query.get_or_404(res_id)
    res.status = 'cancelled'
    db.session.commit()
    flash(f'Reservation #{res_id} cancelled.', 'success')
    return redirect(url_for('routes.admin_dashboard'))


@bp.route('/admin/cancel-booking/<token>', methods=['GET'])
@login_required
def admin_cancel_via_token(token):
    if not current_user.is_admin:
        abort(403)

    reservation = Reservation.query.filter_by(cancel_token=token).first_or_404()
    reservation.status = 'cancelled'
    db.session.commit()
    flash('Booking cancelled (admin).', 'success')
    return redirect(url_for('routes.admin_dashboard'))


# ── Feeds ────────────────────────────────────────────────────────────────────


@bp.route('/admin/feeds/add', methods=['GET', 'POST'])
@login_required
def add_feed():
    if not current_user.is_admin:
        abort(403)

    form = ICalFeedForm()
    if form.validate_on_submit():
        feed = ICalFeed(
            name=form.name.data,
            ical_url=form.ical_url.data,
            platform=form.platform.data,
            active=form.active.data
        )
        db.session.add(feed)
        db.session.commit()
        flash('iCal feed added.', 'success')
        return redirect(url_for('routes.admin_dashboard'))

    return render_template('admin_feed_form.html', form=form, edit=False)


@bp.route('/admin/feeds/<int:feed_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_feed(feed_id):
    if not current_user.is_admin:
        abort(403)

    feed = ICalFeed.query.get_or_404(feed_id)
    form = ICalFeedForm(obj=feed)
    if form.validate_on_submit():
        form.populate_obj(feed)
        db.session.commit()
        flash('iCal feed updated.', 'success')
        return redirect(url_for('routes.admin_dashboard'))

    return render_template('admin_feed_form.html', form=form, edit=True, feed=feed)


@bp.route('/admin/feeds/<int:feed_id>/delete', methods=['POST'])
@login_required
def delete_feed(feed_id):
    if not current_user.is_admin:
        abort(403)

    feed = ICalFeed.query.get_or_404(feed_id)
    db.session.delete(feed)
    db.session.commit()
    flash('iCal feed deleted.', 'success')
    return redirect(url_for('routes.admin_dashboard'))


@bp.route('/admin/feeds/sync', methods=['POST'])
@login_required
def sync_feeds_now():
    if not current_user.is_admin:
        abort(403)

    from app.services.ical_sync import sync_all_feeds
    added, cancelled, errors = sync_all_feeds()
    flash(f'Sync complete: {added} added, {cancelled} cancelled, {errors} errors.', 'info')
    return redirect(url_for('routes.admin_dashboard'))


# ── Testimonials ─────────────────────────────────────────────────────────────


@bp.route('/admin/testimonials')
@login_required
def admin_testimonials():
    if not current_user.is_admin:
        abort(403)

    testimonials = Testimonial.query.order_by(Testimonial.created_at.desc()).all()
    return render_template('admin_testimonials.html', testimonials=testimonials)


@bp.route('/admin/testimonials/<int:testimonial_id>/publish', methods=['POST'])
@login_required
def admin_toggle_testimonial_publish(testimonial_id):
    if not current_user.is_admin:
        abort(403)
    t = Testimonial.query.get_or_404(testimonial_id)
    t.is_published = not t.is_published
    db.session.commit()
    flash('Testimonial updated.', 'success')
    return redirect(url_for('routes.admin_testimonials'))


@bp.route('/admin/testimonials/<int:testimonial_id>/feature', methods=['POST'])
@login_required
def admin_toggle_testimonial_feature(testimonial_id):
    if not current_user.is_admin:
        abort(403)
    t = Testimonial.query.get_or_404(testimonial_id)
    t.is_featured = not t.is_featured
    db.session.commit()
    flash('Testimonial featured status updated.', 'success')
    return redirect(url_for('routes.admin_testimonials'))


@bp.route('/admin/testimonials/<int:testimonial_id>/delete', methods=['POST'])
@login_required
def admin_delete_testimonial(testimonial_id):
    if not current_user.is_admin:
        abort(403)
    t = Testimonial.query.get_or_404(testimonial_id)
    db.session.delete(t)
    db.session.commit()
    flash('Testimonial deleted.', 'success')
    return redirect(url_for('routes.admin_testimonials'))


# ── Access Management ────────────────────────────────────────────────────────


@bp.route('/admin/access/generate-link', methods=['POST'])
@login_required
def admin_generate_access_link():
    if not current_user.is_admin:
        abort(403)
    res_id = request.form.get('reservation_id', type=int)
    res = Reservation.query.get_or_404(res_id)

    res.access_token = secrets.token_urlsafe(32)
    res.access_token_created = datetime.utcnow()
    db.session.commit()

    access_url = url_for('routes.guest_access', token=res.access_token, _external=True)
    flash(f'Access link generated: {access_url}', 'success')
    return redirect(url_for('routes.admin_dashboard'))


@bp.route('/admin/access/send-link', methods=['POST'])
@login_required
def admin_send_access_link():
    if not current_user.is_admin:
        abort(403)
    res_id = request.form.get('reservation_id', type=int)
    res = Reservation.query.get_or_404(res_id)

    if not res.access_token:
        res.access_token = secrets.token_urlsafe(32)
        res.access_token_created = datetime.utcnow()
        db.session.commit()

    access_url = url_for('routes.guest_access', token=res.access_token, _external=True)
    checkin_url = url_for('routes.guest_self_checkin', token=res.checkin_token, _external=True) if res.checkin_token else '#'

    subject = f"Accesso all'appartamento — {res.guest_name}"
    html = render_template('email_access_link.html', reservation=res, access_url=access_url, checkin_url=checkin_url)

    brevo_api_key = current_app.config.get('MAIL_PASSWORD')
    payload = {
        "sender": {"name": "Lotto235 Garbatella", "email": "lotto235roma@gmail.com"},
        "to": [{"email": res.guest_email}],
        "subject": subject,
        "htmlContent": html
    }

    import requests, json
    try:
        r = requests.post(
            "https://api.brevo.com/v3/smtp/email",
            headers={"accept": "application/json", "content-type": "application/json", "api-key": brevo_api_key},
            data=json.dumps(payload)
        )
        if r.status_code in [200, 201, 202]:
            flash('Access link sent to guest email.', 'success')
        else:
            flash(f'Failed to send email ({r.status_code}).', 'danger')
    except Exception as e:
        flash(f'Error sending email: {e}', 'danger')

    return redirect(url_for('routes.admin_dashboard'))


@bp.route('/admin/access/regenerate-token', methods=['POST'])
@login_required
def admin_regenerate_access_token():
    if not current_user.is_admin:
        abort(403)
    res_id = request.form.get('reservation_id', type=int)
    res = Reservation.query.get_or_404(res_id)
    res.access_token = secrets.token_urlsafe(32)
    res.access_token_created = datetime.utcnow()
    db.session.commit()
    flash('Access token regenerated.', 'success')
    return redirect(url_for('routes.admin_dashboard'))


# ── Smart Lock Tests ────────────────────────────────────────────────────────


@bp.route('/admin/smart-access/test-gate', methods=['POST'])
@login_required
def admin_test_gate():
    if not current_user.is_admin:
        abort(403)
    from app.services.smart_lock import trigger_gate_open
    apartment = get_apartment()
    if not apartment:
        flash('No apartment configured.', 'danger')
        return redirect(url_for('routes.admin_smart_access'))
    ok, msg = trigger_gate_open(apartment)
    flash(msg, 'success' if ok else 'danger')
    return redirect(url_for('routes.admin_smart_access'))


@bp.route('/admin/smart-access/test-door', methods=['POST'])
@login_required
def admin_test_door():
    if not current_user.is_admin:
        abort(403)
    from app.services.smart_lock import trigger_door_unlock
    apartment = get_apartment()
    if not apartment:
        flash('No apartment configured.', 'danger')
        return redirect(url_for('routes.admin_smart_access'))
    ok, msg = trigger_door_unlock(apartment)
    flash(msg, 'success' if ok else 'danger')
    return redirect(url_for('routes.admin_smart_access'))


# ── Guest Communication ──────────────────────────────────────────────────────


@bp.route('/admin/communication/guest-message/<int:reservation_id>')
@login_required
def admin_guest_message(reservation_id):
    if not current_user.is_admin:
        abort(403)

    res = Reservation.query.get_or_404(reservation_id)
    apt = Apartment.query.first()

    if not res.checkin_token:
        res.checkin_token = secrets.token_urlsafe(32)
    if not res.access_token:
        res.access_token = secrets.token_urlsafe(32)
    db.session.commit()

    checkin_url = url_for('routes.guest_self_checkin', token=res.checkin_token, _external=True)
    access_url = url_for('routes.guest_access', token=res.access_token, _external=True)
    portal_url = url_for('routes.guest_portal', token=res.checkin_token, _external=True)

    apt_name = apt.name if apt else 'Lotto 235 Garbatella'
    checkin_message = f"""Ciao {res.guest_name},\n\nGrazie per aver prenotato presso {apt_name}!\n\nPer completare il check-in online (obbligatorio per legge italiana), clicca qui:\n{checkin_url}\n\nIl link è valido dal {res.check_in.strftime('%d/%m/%Y')} al {res.check_out.strftime('%d/%m/%Y')}.\n\nDurante il soggiorno potrai aprire il cancello e la porta dell'appartamento da questo link:\n{access_url}\n\nOppure usa il portale unico per tutto:\n{portal_url}\n\nA presto!\n{apt_name}"""

    whatsapp_message = f"""Ciao {res.guest_name}! 👋\n\nGrazie per aver prenotato da {apt_name}!\n\n🔑 *Check-in online (obbligatorio)*:\n{checkin_url}\n\n🚪 *Apri cancello e porta* (valido durante il soggiorno):\n{access_url}\n\n📱 *Portale unico* (check-in + accessi):\n{portal_url}\n\nDisponibile dal {res.check_in.strftime('%d/%m/%Y')} al {res.check_out.strftime('%d/%m/%Y')}.\n\nA presto!"""

    airbnb_message = f"""Hi {res.guest_name},\n\nThanks for booking at {apt_name}!\n\n🔑 *Online Check-in (required by Italian law)*:\n{checkin_url}\n\n🚪 *Gate & Door Access* (valid during your stay):\n{access_url}\n\n📱 *All-in-one Portal*:\n{portal_url}\n\nAvailable from {res.check_in.strftime('%b %d')} to {res.check_out.strftime('%b %d, %Y')}.\n\nSee you soon!"""

    return jsonify({
        'success': True,
        'reservation_id': res.id,
        'guest_name': res.guest_name,
        'checkin_url': checkin_url,
        'access_url': access_url,
        'portal_url': portal_url,
        'templates': {
            'standard': checkin_message,
            'whatsapp': whatsapp_message,
            'airbnb': airbnb_message,
        }
    })

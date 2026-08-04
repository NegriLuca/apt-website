import json
import secrets
from datetime import date, datetime, timedelta

from flask import Response, abort, current_app, flash, jsonify, redirect, render_template, request, url_for
from flask_babel import gettext as _
from flask_login import current_user, login_required, login_user, logout_user

from app import db, limiter
from app.forms import ICalFeedForm, LoginForm
from app.models import Apartment, AuditLog, Coupon, ICalFeed, Notification, Reservation, Testimonial, User
from app.routes import bp
from app.routes.helpers import create_balance_payment_session, get_apartment

# ── Auth ─────────────────────────────────────────────────────────────────────


def admin_audit_log(
    action: str, entity_type: str | None = None, entity_id: int | None = None, details: str | None = None
) -> None:
    log = AuditLog(
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        admin_user=current_user.username,
        details=details,
        ip_address=request.remote_addr,
    )
    db.session.add(log)
    db.session.commit()


def push_notification(title: str, message: str, category: str = 'info', link: str | None = None) -> None:
    notif = Notification(title=title, message=message, category=category, link=link)
    db.session.add(notif)
    db.session.commit()


@bp.route('/login', methods=['GET', 'POST'])
@limiter.limit('10 per minute')
def login() -> Response | str:
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
def logout() -> Response | str:
    logout_user()
    flash('You have been logged out.', 'info')
    return redirect(url_for('routes.home'))


# ── Admin Dashboard ──────────────────────────────────────────────────────────


@bp.route('/admin')
@login_required
def admin_dashboard() -> Response | str:
    if not current_user.is_admin:
        abort(403)

    today = date.today()
    month_start = today.replace(day=1)
    year_start = today.replace(month=1, day=1)

    reservations = Reservation.query.order_by(Reservation.check_in.desc()).all()

    confirmed = [r for r in reservations if r.status == 'confirmed']
    cancelled = [r for r in reservations if r.status == 'cancelled']
    pending = [r for r in reservations if r.status == 'pending']

    monthly_confirmed = [r for r in confirmed if r.check_in >= month_start]
    monthly_revenue = sum(r.total_price for r in monthly_confirmed)

    yearly_confirmed = [r for r in confirmed if r.check_in >= year_start]
    yearly_revenue = sum(r.total_price for r in yearly_confirmed)

    source_counts = {}
    for r in confirmed:
        src = r.source or 'direct'
        source_counts[src] = source_counts.get(src, 0) + 1

    today_checkins = [r for r in confirmed if r.check_in == today]
    today_checkouts = [r for r in confirmed if r.check_out == today]
    upcoming = [r for r in confirmed if r.check_in > today][:5]
    in_house = [r for r in confirmed if r.check_in <= today <= r.check_out]

    pending_questura = Reservation.query.filter(
        Reservation.questura_status.in_([None, 'pending']),
        Reservation.status == 'confirmed',
        Reservation.check_in <= today,
    ).count()

    occupancy_days = sum(r.nights for r in monthly_confirmed)
    month_days = (today.replace(month=today.month % 12 + 1, day=1) - timedelta(days=1)).day if today.month < 12 else 31
    occupancy_rate = round((occupancy_days / month_days) * 100, 1) if month_days else 0

    dashboard_data = {
        'total': len(reservations),
        'confirmed': len(confirmed),
        'cancelled': len(cancelled),
        'pending': len(pending),
        'monthly_revenue': monthly_revenue,
        'yearly_revenue': yearly_revenue,
        'monthly_bookings': len(monthly_confirmed),
        'occupancy_rate': occupancy_rate,
        'source_counts': source_counts,
        'today_checkins': len(today_checkins),
        'today_checkouts': len(today_checkouts),
        'in_house': len(in_house),
        'pending_questura': pending_questura,
    }

    now = datetime.utcnow()
    return render_template(
        'admin_dashboard.html',
        reservations=reservations,
        data=dashboard_data,
        today=today,
        now=now,
        upcoming=upcoming,
        in_house=in_house,
    )


@bp.route('/admin/calendar')
@login_required
def admin_calendar() -> Response | str:
    if not current_user.is_admin:
        abort(403)
    return render_template('admin_calendar.html')


@bp.route('/admin/pricing', methods=['GET', 'POST'])
@login_required
def admin_pricing() -> Response | str:
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
                admin_audit_log('update_price', 'Apartment', apartment.id, f'Price set to \u20ac{new_price}')
                flash('Nightly base rate updated successfully!', 'success')
            except ValueError:
                flash('Invalid price format entered.', 'danger')
            return redirect(url_for('routes.admin_pricing'))

    all_coupons = Coupon.query.all()
    return render_template('admin_pricing.html', apartment=apartment, coupons=all_coupons)


@bp.route('/admin/smart-access', methods=['GET', 'POST'])
@login_required
def admin_smart_access() -> Response | str:
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
        apartment.shelly_relay_channel = request.form.get('shelly_relay_channel', type=int) or 0

        apartment.nuki_enabled = bool(request.form.get('nuki_enabled'))
        apartment.nuki_smartlock_id = request.form.get('nuki_smartlock_id', '').strip() or None
        apartment.nuki_web_token = request.form.get('nuki_web_token', '').strip() or None
        apartment.nuki_web_base_url = request.form.get('nuki_web_base_url', '').strip() or 'https://api.nuki.io'
        apartment.nuki_unlock_action = request.form.get('nuki_unlock_action', 'unlock')

        apartment.whatsapp_number = request.form.get('whatsapp_number', '').strip() or None
        apartment.whatsapp_default_message = request.form.get('whatsapp_default_message', '').strip() or None

        db.session.commit()
        admin_audit_log('update_smart_access', 'Apartment', apartment.id)
        flash('Smart access settings updated successfully!', 'success')
        return redirect(url_for('routes.admin_smart_access'))

    return render_template('admin_smart_access.html', apartment=apartment)


@bp.route('/admin/trust-badges', methods=['GET', 'POST'])
@login_required
def admin_trust_badges() -> Response | str:
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
            setattr(
                apartment, f'custom_badge_{i}_image', request.form.get(f'custom_badge_{i}_image', '').strip() or None
            )
            setattr(apartment, f'custom_badge_{i}_link', request.form.get(f'custom_badge_{i}_link', '').strip() or None)
            setattr(apartment, f'custom_badge_{i}_alt', request.form.get(f'custom_badge_{i}_alt', '').strip() or None)

        apartment.show_reviews_in_footer = bool(request.form.get('show_reviews_in_footer'))
        apartment.show_reviews_on_homepage = bool(request.form.get('show_reviews_on_homepage'))
        apartment.show_reviews_on_booking = bool(request.form.get('show_reviews_on_booking'))
        apartment.show_payment_badges_in_footer = bool(request.form.get('show_payment_badges_in_footer'))
        apartment.show_payment_badges_on_checkout = bool(request.form.get('show_payment_badges_on_checkout'))

        apartment.booking_widget_js = request.form.get('booking_widget_js', '').strip() or None
        apartment.airbnb_widget_js = request.form.get('airbnb_widget_js', '').strip() or None
        apartment.google_widget_js = request.form.get('google_widget_js', '').strip() or None
        apartment.trustpilot_widget_js = request.form.get('trustpilot_widget_js', '').strip() or None

        db.session.commit()
        admin_audit_log('update_trust_badges', 'Apartment', apartment.id)
        flash(_('Trust Badges & Widgets settings saved!'), 'success')
        return redirect(url_for('routes.admin_trust_badges'))

    return render_template('admin_trust_badges.html', apartment=apartment)


# ── Coupons ──────────────────────────────────────────────────────────────────


@bp.route('/admin/coupons/create', methods=['POST'])
@login_required
def admin_create_coupon() -> Response | str:
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
def admin_delete_coupon(coupon_id: int) -> Response | str:
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
def admin_confirm_reservation(res_id: int) -> Response | str:
    if not current_user.is_admin:
        abort(403)

    res = Reservation.query.get_or_404(res_id)
    res.status = 'confirmed'
    db.session.commit()
    admin_audit_log('confirm_reservation', 'Reservation', res_id)
    flash(f'Reservation #{res_id} confirmed.', 'success')
    return redirect(url_for('routes.admin_dashboard'))


@bp.route('/admin/reservations/<int:res_id>/cancel', methods=['POST'])
@login_required
def admin_cancel_reservation(res_id: int) -> Response | str:
    if not current_user.is_admin:
        abort(403)

    res = Reservation.query.get_or_404(res_id)
    res.status = 'cancelled'
    db.session.commit()
    admin_audit_log('cancel_reservation', 'Reservation', res_id, 'Cancelled by admin')
    flash(f'Reservation #{res_id} cancelled.', 'info')
    return redirect(url_for('routes.admin_dashboard'))


@bp.route('/admin/cancel-booking/<token>', methods=['GET'])
@login_required
def admin_cancel_via_token(token: str) -> Response | str:
    if not current_user.is_admin:
        abort(403)

    reservation = Reservation.query.filter_by(cancel_token=token).first_or_404()
    reservation.status = 'cancelled'
    db.session.commit()
    flash('Booking cancelled (admin).', 'success')
    return redirect(url_for('routes.admin_dashboard'))


def _is_external_reservation(res: Reservation) -> bool:
    return res.source not in ('direct', 'stripe')


@bp.route('/admin/reservations/<int:res_id>/edit', methods=['POST'])
@login_required
def admin_edit_reservation(res_id: int) -> Response | str:
    if not current_user.is_admin:
        abort(403)

    res = Reservation.query.get_or_404(res_id)
    if not _is_external_reservation(res):
        flash('Only external (Airbnb/Booking/VRBO) reservations can be edited here.', 'warning')
        return redirect(url_for('routes.admin_dashboard'))

    try:
        check_in = date.fromisoformat(request.form.get('check_in', ''))
        check_out = date.fromisoformat(request.form.get('check_out', ''))
    except ValueError:
        flash('Invalid dates.', 'danger')
        return redirect(url_for('routes.admin_dashboard'))

    num_guests = request.form.get('num_guests', type=int) or res.num_guests

    if check_out <= check_in:
        flash('Check-out must be after check-in.', 'danger')
        return redirect(url_for('routes.admin_dashboard'))
    if (check_out - check_in).days > 28:
        flash('You can book a maximum of 28 nights.', 'danger')
        return redirect(url_for('routes.admin_dashboard'))
    if not (1 <= num_guests <= 4):
        flash('Guests must be between 1 and 4.', 'danger')
        return redirect(url_for('routes.admin_dashboard'))

    conflicts = Reservation.query.filter(
        Reservation.status != 'cancelled',
        Reservation.id != res_id,
        Reservation.check_in < check_out,
        Reservation.check_out > check_in,
    ).count()
    if conflicts:
        flash('Those dates overlap another reservation.', 'danger')
        return redirect(url_for('routes.admin_dashboard'))

    res.check_in = check_in
    res.check_out = check_out
    res.num_guests = num_guests
    res.num_adults = num_guests
    res.num_children = 0
    db.session.commit()
    admin_audit_log('edit_reservation', 'Reservation', res_id, f'{res.source} {check_in} → {check_out}')
    flash(f'Reservation #{res_id} updated.', 'success')
    return redirect(url_for('routes.admin_dashboard'))


@bp.route('/admin/reservations/<int:res_id>/delete', methods=['POST'])
@login_required
def admin_delete_reservation(res_id: int) -> Response | str:
    if not current_user.is_admin:
        abort(403)

    res = Reservation.query.get_or_404(res_id)

    from app.models import QuesturaLog, Ross1000Log

    QuesturaLog.query.filter_by(reservation_id=res_id).delete()
    Ross1000Log.query.filter_by(reservation_id=res_id).delete()
    db.session.delete(res)
    db.session.commit()
    admin_audit_log('delete_reservation', 'Reservation', res_id, f'Deleted {res.source} reservation')
    flash(f'Reservation #{res_id} deleted.', 'success')
    return redirect(url_for('routes.admin_dashboard'))


@bp.route('/admin/reservations/bulk-delete', methods=['POST'])
@login_required
def admin_bulk_delete_reservations() -> Response | str:
    if not current_user.is_admin:
        abort(403)

    from app.models import QuesturaLog, Ross1000Log

    selected_ids = request.form.getlist('reservation_ids', type=int)
    if not selected_ids:
        flash('No reservations selected.', 'warning')
        return redirect(url_for('routes.admin_dashboard'))

    reservations = Reservation.query.filter(Reservation.id.in_(selected_ids)).all()

    for res in reservations:
        QuesturaLog.query.filter_by(reservation_id=res.id).delete()
        Ross1000Log.query.filter_by(reservation_id=res.id).delete()
        db.session.delete(res)

    db.session.commit()
    admin_audit_log('bulk_delete_reservations', 'Reservation', None, f'Deleted {len(reservations)} reservations')
    flash(f'{len(reservations)} reservation(s) deleted.', 'success')
    return redirect(url_for('routes.admin_dashboard'))


# ── Feeds ────────────────────────────────────────────────────────────────────


@bp.route('/admin/feeds/add', methods=['GET', 'POST'])
@login_required
def add_feed() -> Response | str:
    if not current_user.is_admin:
        abort(403)

    form = ICalFeedForm()
    if form.validate_on_submit():
        feed = ICalFeed(
            name=form.name.data, ical_url=form.ical_url.data, platform=form.platform.data, active=form.active.data
        )
        db.session.add(feed)
        db.session.commit()
        flash('iCal feed added.', 'success')
        return redirect(url_for('routes.admin_dashboard'))

    return render_template('admin_feed_form.html', form=form, edit=False)


@bp.route('/admin/feeds/<int:feed_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_feed(feed_id: int) -> Response | str:
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
def delete_feed(feed_id: int) -> Response | str:
    if not current_user.is_admin:
        abort(403)

    feed = ICalFeed.query.get_or_404(feed_id)
    db.session.delete(feed)
    db.session.commit()
    flash('iCal feed deleted.', 'success')
    return redirect(url_for('routes.admin_dashboard'))


@bp.route('/admin/feeds/sync', methods=['POST'])
@login_required
def sync_feeds_now() -> Response | str:
    if not current_user.is_admin:
        abort(403)

    from app.services.ical_sync import sync_all_feeds

    added, cancelled, errors = sync_all_feeds()
    flash(f'Sync complete: {added} added, {cancelled} cancelled, {errors} errors.', 'info')
    return redirect(url_for('routes.admin_dashboard'))


# ── Testimonials ─────────────────────────────────────────────────────────────


@bp.route('/admin/testimonials')
@login_required
def admin_testimonials() -> Response | str:
    if not current_user.is_admin:
        abort(403)

    testimonials = Testimonial.query.order_by(Testimonial.created_at.desc()).all()
    stats = {
        'testimonials_total': Testimonial.query.count(),
        'testimonials_published': Testimonial.query.filter_by(is_published=True).count(),
        'testimonials_pending': Testimonial.query.filter_by(is_published=False).count(),
        'testimonials_featured': Testimonial.query.filter_by(is_featured=True).count(),
    }
    return render_template('admin_testimonials.html', testimonials=testimonials, stats=stats)


@bp.route('/admin/testimonials/<int:testimonial_id>/publish', methods=['POST'])
@login_required
def admin_toggle_testimonial_publish(testimonial_id: int) -> Response | str:
    if not current_user.is_admin:
        abort(403)
    t = Testimonial.query.get_or_404(testimonial_id)
    t.is_published = not t.is_published
    db.session.commit()
    flash('Testimonial updated.', 'success')
    return redirect(url_for('routes.admin_testimonials'))


@bp.route('/admin/testimonials/<int:testimonial_id>/feature', methods=['POST'])
@login_required
def admin_toggle_testimonial_feature(testimonial_id: int) -> Response | str:
    if not current_user.is_admin:
        abort(403)
    t = Testimonial.query.get_or_404(testimonial_id)
    t.is_featured = not t.is_featured
    db.session.commit()
    flash('Testimonial featured status updated.', 'success')
    return redirect(url_for('routes.admin_testimonials'))


@bp.route('/admin/testimonials/<int:testimonial_id>/delete', methods=['POST'])
@login_required
def admin_delete_testimonial(testimonial_id: int) -> Response | str:
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
def admin_generate_access_link() -> Response | str:
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
def admin_send_access_link() -> Response | str:
    if not current_user.is_admin:
        abort(403)
    res_id = request.form.get('reservation_id', type=int)
    res = Reservation.query.get_or_404(res_id)

    if not res.access_token:
        res.access_token = secrets.token_urlsafe(32)
        res.access_token_created = datetime.utcnow()
        db.session.commit()

    access_url = url_for('routes.guest_access', token=res.access_token, _external=True)
    checkin_url = (
        url_for('routes.guest_self_checkin', token=res.checkin_token, _external=True) if res.checkin_token else '#'
    )

    subject = f"Accesso all'appartamento \u2014 {res.guest_name}"
    html = render_template('email_access_link.html', reservation=res, access_url=access_url, checkin_url=checkin_url)

    brevo_api_key = current_app.config.get('MAIL_PASSWORD')
    payload = {
        'sender': {'name': 'Lotto235 Garbatella', 'email': 'lotto235roma@gmail.com'},
        'to': [{'email': res.guest_email}],
        'subject': subject,
        'htmlContent': html,
    }

    import requests

    try:
        r = requests.post(
            'https://api.brevo.com/v3/smtp/email',
            headers={'accept': 'application/json', 'content-type': 'application/json', 'api-key': brevo_api_key},
            data=json.dumps(payload),
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
def admin_regenerate_access_token() -> Response | str:
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
def admin_test_gate() -> Response | str:
    if not current_user.is_admin:
        abort(403)
    from app.services.smart_lock import SmartLockError, trigger_gate_open

    apartment = get_apartment()
    if not apartment:
        return jsonify({'success': False, 'error': 'No apartment configured.'}), 400
    try:
        ok, msg = trigger_gate_open(apartment)
    except SmartLockError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    return jsonify({'success': ok, 'message': msg if ok else 'Failed to open gate'})


@bp.route('/admin/smart-access/test-door', methods=['POST'])
@login_required
def admin_test_door() -> Response | str:
    if not current_user.is_admin:
        abort(403)
    from app.services.smart_lock import SmartLockError, trigger_door_unlock

    apartment = get_apartment()
    if not apartment:
        return jsonify({'success': False, 'error': 'No apartment configured.'}), 400
    try:
        ok, msg = trigger_door_unlock(apartment)
    except SmartLockError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    return jsonify({'success': ok, 'message': msg if ok else 'Failed to unlock door'})


# ── Bulk Operations ───────────────────────────────────────────────────────────


@bp.route('/admin/bulk-pricing', methods=['POST'])
@login_required
def admin_bulk_pricing() -> Response | str:
    if not current_user.is_admin:
        abort(403)
    apartment = get_apartment()
    price = request.form.get('bulk_price', type=float)
    if price and price > 0:
        apartment.price_per_night = price
        db.session.commit()
        admin_audit_log('bulk_update_price', 'Apartment', apartment.id, f'Bulk price set to \u20ac{price}')
        flash(f'Price updated to \u20ac{price:.2f} for all dates.', 'success')
    else:
        flash('Invalid price.', 'danger')
    return redirect(url_for('routes.admin_pricing'))


# ── Automated Review Requests ────────────────────────────────────────────────


@bp.route('/admin/send-review-request/<int:reservation_id>', methods=['POST'])
@login_required
def admin_send_review_request(reservation_id: int) -> Response | str:
    if not current_user.is_admin:
        abort(403)
    res = Reservation.query.get_or_404(reservation_id)
    if not res.guest_email:
        flash('No email on file for this reservation.', 'danger')
        return redirect(url_for('routes.admin_dashboard'))
    try:
        sender_email = 'lotto235roma@gmail.com'
        review_url = url_for('routes.submit_testimonial', _external=True)
        payload = {
            'sender': {'name': 'Lotto235 Garbatella', 'email': sender_email},
            'to': [{'email': res.guest_email}],
            'subject': 'How was your stay? Leave a review!',
            'htmlContent': render_template('email_review_request.html', reservation=res, review_url=review_url),
        }
        from app.routes.helpers import _send_brevo_email

        r = _send_brevo_email(payload)
        if r.status_code in [200, 201, 202]:
            admin_audit_log('send_review_request', 'Reservation', res.id, f'Review request sent to {res.guest_email}')
            push_notification('Review request sent', f'Sent to {res.guest_name} ({res.guest_email})', 'info')
            flash('Review request sent.', 'success')
        else:
            flash('Failed to send review request.', 'danger')
    except Exception as e:
        flash(f'Error: {e}', 'danger')
    return redirect(url_for('routes.admin_dashboard'))


@bp.route('/admin/send-review-requests-bulk', methods=['POST'])
@login_required
def admin_send_review_requests_bulk() -> Response | str:
    if not current_user.is_admin:
        abort(403)
    today = date.today()
    recent = Reservation.query.filter(
        Reservation.status == 'confirmed',
        Reservation.check_out <= today,
        Reservation.check_out >= today - timedelta(days=30),
        Reservation.guest_email.isnot(None),
    ).all()
    sent = 0
    for res in recent:
        try:
            review_url = url_for('routes.submit_testimonial', _external=True)
            payload = {
                'sender': {'name': 'Lotto235 Garbatella', 'email': 'lotto235roma@gmail.com'},
                'to': [{'email': res.guest_email}],
                'subject': 'How was your stay? Leave a review!',
                'htmlContent': render_template('email_review_request.html', reservation=res, review_url=review_url),
            }
            from app.routes.helpers import _send_brevo_email

            r = _send_brevo_email(payload)
            if r.status_code in [200, 201, 202]:
                sent += 1
        except Exception:
            pass
    flash(f'Review requests sent to {sent} guests.', 'success')
    return redirect(url_for('routes.admin_dashboard'))


# ── Guest Communication ──────────────────────────────────────────────────────


@bp.route('/admin/communication/guest-message/<int:reservation_id>')
@login_required
def admin_guest_message(reservation_id: int) -> Response | str:
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
    checkin_message = f"""Ciao {res.guest_name},\n\nGrazie per aver prenotato presso {apt_name}!\n\nPer completare il check-in online (obbligatorio per legge italiana), clicca qui:\n{checkin_url}\n\nIl link \u00e8 valido dal {res.check_in.strftime('%d/%m/%Y')} al {res.check_out.strftime('%d/%m/%Y')}.\n\nDurante il soggiorno potrai aprire il cancello e la porta dell'appartamento da questo link:\n{access_url}\n\nOppure usa il portale unico per tutto:\n{portal_url}\n\nA presto!\n{apt_name}"""

    whatsapp_message = f"""Ciao {res.guest_name}! \U0001f44b\n\nGrazie per aver prenotato da {apt_name}!\n\n\U0001f511 *Check-in online (obbligatorio)*:\n{checkin_url}\n\n\U0001f6aa *Apri cancello e porta* (valido durante il soggiorno):\n{access_url}\n\n\U0001f4f1 *Portale unico* (check-in + accessi):\n{portal_url}\n\nDisponibile dal {res.check_in.strftime('%d/%m/%Y')} al {res.check_out.strftime('%d/%m/%Y')}.\n\nA presto!"""

    airbnb_message = f"""Hi {res.guest_name},\n\nThanks for booking at {apt_name}!\n\n\U0001f511 *Online Check-in (required by Italian law)*:\n{checkin_url}\n\n\U0001f6aa *Gate & Door Access* (valid during your stay):\n{access_url}\n\n\U0001f4f1 *All-in-one Portal*:\n{portal_url}\n\nAvailable from {res.check_in.strftime('%b %d')} to {res.check_out.strftime('%b %d, %Y')}.\n\nSee you soon!"""

    return jsonify(
        {
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
            },
        }
    )


# ── Notifications ─────────────────────────────────────────────────────────────


@bp.route('/admin/notifications')
@login_required
def admin_notifications() -> Response | str:
    if not current_user.is_admin:
        abort(403)
    page = request.args.get('page', 1, type=int)
    notifications = Notification.query.order_by(Notification.created_at.desc()).paginate(
        page=page, per_page=30, error_out=False
    )
    return render_template('admin_notifications.html', notifications=notifications)


@bp.route('/admin/notifications/mark-read/<int:notif_id>', methods=['POST'])
@login_required
def admin_mark_notification_read(notif_id: int) -> Response | str:
    if not current_user.is_admin:
        abort(403)
    notif = Notification.query.get_or_404(notif_id)
    notif.is_read = True
    db.session.commit()
    return redirect(url_for('routes.admin_notifications'))


@bp.route('/admin/notifications/mark-all-read', methods=['POST'])
@login_required
def admin_mark_all_read() -> Response | str:
    if not current_user.is_admin:
        abort(403)
    Notification.query.filter_by(is_read=False).update({'is_read': True})
    db.session.commit()
    flash('All notifications marked as read.', 'success')
    return redirect(url_for('routes.admin_notifications'))


# ── iCal Feeds ────────────────────────────────────────────────────────────────


@bp.route('/admin/ical-feeds')
@login_required
def admin_ical_feeds() -> Response | str:
    if not current_user.is_admin:
        abort(403)
    feeds = ICalFeed.query.all()
    from app.services.ical_sync import get_blocked_dates

    blocked_dates = get_blocked_dates() or []
    return render_template('admin_ical_feeds.html', feeds=feeds, blocked_dates=blocked_dates)


@bp.route('/admin/ical-feeds/create', methods=['POST'])
@login_required
def admin_ical_create() -> Response | str:
    if not current_user.is_admin:
        abort(403)
    url = request.form.get('url', '').strip()
    source = request.form.get('source', '').strip()
    if url and source:
        feed = ICalFeed(url=url, source=source)
        db.session.add(feed)
        db.session.commit()
        admin_audit_log('create_ical_feed', 'ICalFeed', feed.id, f'Added {source} feed')
        flash(f'{source} feed added.', 'success')
    return redirect(url_for('routes.admin_ical_feeds'))


# ── Balance Payment (Deposit → Full) ──────────────────────────────────────────


@bp.route('/admin/charge-balance/<int:res_id>', methods=['POST'])
@login_required
def admin_charge_balance(res_id: int) -> Response | str:
    if not current_user.is_admin:
        abort(403)
    res = Reservation.query.get_or_404(res_id)
    if res.payment_status != 'deposit_paid':
        flash('Reservation does not have an outstanding balance.', 'warning')
        return redirect(url_for('routes.admin_dashboard'))

    remaining = round((res.total_price - (res.amount_paid or 0.0)), 2)
    if remaining <= 0:
        flash('No balance remaining.', 'info')
        return redirect(url_for('routes.admin_dashboard'))

    session_data = create_balance_payment_session(res)
    if not session_data:
        current_app.logger.error('Stripe balance charge failed for reservation #%s', res.id)
        flash('Failed to create payment link. Check Stripe configuration.', 'danger')
        return redirect(url_for('routes.admin_dashboard'))

    admin_audit_log('charge_balance', 'Reservation', res.id,
                    f'Created balance payment link for €{remaining:.2f}')
    flash(f'Payment link created. Share with guest: {session_data.url}', 'success')
    return redirect(session_data.url)


@bp.route('/payment/balance-success')
def balance_payment_success() -> Response | str:
    session_id = request.args.get('session_id')
    if not session_id:
        return redirect(url_for('routes.home'))

    stripe.api_key = current_app.config.get('STRIPE_SECRET_KEY')
    try:
        checkout_session = stripe.checkout.Session.retrieve(session_id)
    except stripe.error.StripeError:
        flash('Payment verification failed.', 'danger')
        return redirect(url_for('routes.admin_dashboard'))

    meta = checkout_session.get('metadata') or {}
    if meta.get('type') != 'balance_payment':
        return redirect(url_for('routes.home'))

    res_id = int(meta.get('reservation_id', 0))
    res = Reservation.query.get(res_id)
    if not res:
        return redirect(url_for('routes.home'))

    res.payment_status = 'paid'
    remaining = round((res.total_price - (res.amount_paid or 0.0)), 2)
    res.amount_paid = (res.amount_paid or 0.0) + remaining
    res.balance_payment_intent_id = checkout_session.get('payment_intent')
    db.session.commit()

    from app.routes.helpers import send_payment_verified_email
    try:
        send_payment_verified_email(res)
    except Exception as exc:
        current_app.logger.error('Balance payment email failed: %s', exc)

    flash('Balance payment received. Reservation is now fully paid.', 'success')
    return redirect(url_for('routes.booking_confirmed', reservation_id=res.id))


# ── Audit Log ──────────────────────────────────────────────────────────────────


@bp.route('/admin/audit-log')
@login_required
def admin_audit_log_view() -> Response | str:
    if not current_user.is_admin:
        abort(403)
    page = request.args.get('page', 1, type=int)
    search = request.args.get('search', '').strip()
    admin_filter = request.args.get('admin', '').strip()
    entity_filter = request.args.get('entity', '').strip()
    date_from = request.args.get('date_from', '').strip()

    query = AuditLog.query

    if search:
        query = query.filter(
            db.or_(
                AuditLog.action.ilike(f'%{search}%'),
                AuditLog.details.ilike(f'%{search}%'),
            )
        )
    if admin_filter:
        query = query.filter(AuditLog.admin_user == admin_filter)
    if entity_filter:
        query = query.filter(AuditLog.entity_type == entity_filter)
    if date_from:
        try:
            dt = datetime.strptime(date_from, '%Y-%m-%d')
            query = query.filter(AuditLog.created_at >= dt)
        except ValueError:
            pass

    logs = query.order_by(AuditLog.created_at.desc()).paginate(page=page, per_page=50, error_out=False)
    return render_template(
        'admin_audit_log.html',
        logs=logs,
        search=search,
        admin_filter=admin_filter,
        entity_filter=entity_filter,
        date_from=date_from,
    )

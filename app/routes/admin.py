from flask import render_template, redirect, url_for, flash, request, current_app, abort, jsonify
from flask_babel import gettext as _
from flask_login import login_user, logout_user, login_required, current_user
from app.routes import bp
from app.routes.helpers import get_apartment, calculate_dynamic_total, get_payment_summary
from app import db, limiter
from app.models import Apartment, Reservation, User, ICalFeed, Coupon, Testimonial, AuditLog, Notification, Message, CleaningTask
from app.forms import LoginForm, ICalFeedForm
from datetime import datetime, date, timedelta
from sqlalchemy.exc import IntegrityError
from functools import wraps
import stripe
import secrets
import json


# ── Auth ─────────────────────────────────────────────────────────────────────


def admin_audit_log(action, entity_type=None, entity_id=None, details=None):
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


def push_notification(title, message, category='info', link=None):
    notif = Notification(title=title, message=message, category=category, link=link)
    db.session.add(notif)
    db.session.commit()


@bp.route('/login', methods=['GET', 'POST'])
@limiter.limit("10 per minute")
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
        Reservation.check_in <= today
    ).count()

    unread_messages = Message.query.filter_by(is_read=False).count()

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
        'unread_messages': unread_messages,
    }

    now = datetime.utcnow()
    return render_template('admin_dashboard.html',
        reservations=reservations,
        data=dashboard_data,
        today=today,
        now=now,
        upcoming=upcoming,
        in_house=in_house,
    )


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
                admin_audit_log('update_price', 'Apartment', apartment.id, f'Price set to €{new_price}')
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
        admin_audit_log('update_smart_access', 'Apartment', apartment.id)
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
    admin_audit_log('confirm_reservation', 'Reservation', res_id)
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
    admin_audit_log('cancel_reservation', 'Reservation', res_id, f'Cancelled by admin')
    flash(f'Reservation #{res_id} cancelled.', 'info')
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
    stats = {
        'testimonials_total': Testimonial.query.count(),
        'testimonials_published': Testimonial.query.filter_by(is_published=True).count(),
        'testimonials_pending': Testimonial.query.filter_by(is_published=False).count(),
        'testimonials_featured': Testimonial.query.filter_by(is_featured=True).count(),
    }
    return render_template('admin_testimonials.html', testimonials=testimonials, stats=stats)


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


# ── Bulk Operations ───────────────────────────────────────────────────────────


@bp.route('/admin/bulk-pricing', methods=['POST'])
@login_required
def admin_bulk_pricing():
    if not current_user.is_admin:
        abort(403)
    apartment = get_apartment()
    price = request.form.get('bulk_price', type=float)
    if price and price > 0:
        apartment.price_per_night = price
        db.session.commit()
        admin_audit_log('bulk_update_price', 'Apartment', apartment.id, f'Bulk price set to €{price}')
        flash(f'Price updated to €{price:.2f} for all dates.', 'success')
    else:
        flash('Invalid price.', 'danger')
    return redirect(url_for('routes.admin_pricing'))


# ── Automated Review Requests ────────────────────────────────────────────────


@bp.route('/admin/send-review-request/<int:reservation_id>', methods=['POST'])
@login_required
def admin_send_review_request(reservation_id):
    if not current_user.is_admin:
        abort(403)
    res = Reservation.query.get_or_404(reservation_id)
    if not res.guest_email:
        flash('No email on file for this reservation.', 'danger')
        return redirect(url_for('routes.admin_dashboard'))
    try:
        sender_email = "lotto235roma@gmail.com"
        review_url = url_for('routes.submit_testimonial', _external=True)
        payload = {
            "sender": {"name": "Lotto235 Garbatella", "email": sender_email},
            "to": [{"email": res.guest_email}],
            "subject": "How was your stay? Leave a review!",
            "htmlContent": render_template('email_review_request.html', reservation=res, review_url=review_url)
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
def admin_send_review_requests_bulk():
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
                "sender": {"name": "Lotto235 Garbatella", "email": "lotto235roma@gmail.com"},
                "to": [{"email": res.guest_email}],
                "subject": "How was your stay? Leave a review!",
                "htmlContent": render_template('email_review_request.html', reservation=res, review_url=review_url)
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


# ── Notifications ─────────────────────────────────────────────────────────────


@bp.route('/admin/notifications')
@login_required
def admin_notifications():
    if not current_user.is_admin:
        abort(403)
    page = request.args.get('page', 1, type=int)
    notifications = Notification.query.order_by(Notification.created_at.desc()).paginate(page=page, per_page=30, error_out=False)
    return render_template('admin_notifications.html', notifications=notifications)


@bp.route('/admin/notifications/mark-read/<int:notif_id>', methods=['POST'])
@login_required
def admin_mark_notification_read(notif_id):
    if not current_user.is_admin:
        abort(403)
    notif = Notification.query.get_or_404(notif_id)
    notif.is_read = True
    db.session.commit()
    return redirect(url_for('routes.admin_notifications'))


@bp.route('/admin/notifications/mark-all-read', methods=['POST'])
@login_required
def admin_mark_all_read():
    if not current_user.is_admin:
        abort(403)
    Notification.query.filter_by(is_read=False).update({'is_read': True})
    db.session.commit()
    flash('All notifications marked as read.', 'success')
    return redirect(url_for('routes.admin_notifications'))


# ── Messages Inbox ────────────────────────────────────────────────────────────


@bp.route('/admin/messages')
@login_required
def admin_messages():
    if not current_user.is_admin:
        abort(403)
    page = request.args.get('page', 1, type=int)
    messages = Message.query.order_by(Message.created_at.desc()).paginate(page=page, per_page=30, error_out=False)
    return render_template('admin_messages.html', messages=messages)


@bp.route('/admin/messages/<int:msg_id>/read', methods=['POST'])
@login_required
def admin_mark_message_read(msg_id):
    if not current_user.is_admin:
        abort(403)
    msg = Message.query.get_or_404(msg_id)
    msg.is_read = True
    db.session.commit()
    return redirect(url_for('routes.admin_messages'))


@bp.route('/admin/messages/send', methods=['POST'])
@login_required
def admin_send_message():
    if not current_user.is_admin:
        abort(403)
    res_id = request.form.get('reservation_id', type=int)
    body = request.form.get('body', '').strip()
    if not body:
        flash('Message body is required.', 'danger')
        return redirect(url_for('routes.admin_messages'))
    res = Reservation.query.get(res_id) if res_id else None
    msg = Message(
        reservation_id=res_id,
        guest_name=res.guest_name if res else request.form.get('guest_name', 'Guest'),
        guest_email=res.guest_email if res else None,
        subject=request.form.get('subject', ''),
        body=body,
        direction='outgoing',
    )
    db.session.add(msg)
    db.session.commit()
    admin_audit_log('send_message', 'Message', msg.id, f'Sent message to {msg.guest_name}')
    flash('Message sent.', 'success')
    return redirect(url_for('routes.admin_messages'))


# ── Cleaning Tasks ────────────────────────────────────────────────────────────


@bp.route('/admin/cleaning')
@login_required
def admin_cleaning():
    if not current_user.is_admin:
        abort(403)
    today = date.today()
    week_end = today + timedelta(days=7)
    tasks = CleaningTask.query.filter(
        CleaningTask.scheduled_date >= today,
        CleaningTask.scheduled_date <= week_end,
    ).order_by(CleaningTask.scheduled_date).all()
    pending_count = CleaningTask.query.filter_by(status='pending').count()
    completed_count = CleaningTask.query.filter_by(status='completed').count()
    upcoming_checkouts = Reservation.query.filter(
        Reservation.status == 'confirmed',
        Reservation.check_out >= today,
        Reservation.check_out <= week_end,
    ).order_by(Reservation.check_out).all()
    return render_template('admin_cleaning.html',
        tasks=tasks, pending_count=pending_count,
        completed_count=completed_count,
        upcoming_checkouts=upcoming_checkouts, today=today)


@bp.route('/admin/cleaning/create', methods=['POST'])
@login_required
def admin_create_cleaning_task():
    if not current_user.is_admin:
        abort(403)
    res_id = request.form.get('reservation_id', type=int)
    scheduled = request.form.get('scheduled_date')
    title = request.form.get('title', 'Turnover cleaning')
    task = CleaningTask(
        reservation_id=res_id or None,
        title=title,
        scheduled_date=datetime.strptime(scheduled, '%Y-%m-%d').date() if scheduled else date.today(),
        assigned_to=request.form.get('assigned_to', '').strip() or None,
        notes=request.form.get('notes', '').strip() or None,
    )
    db.session.add(task)
    db.session.commit()
    admin_audit_log('create_cleaning_task', 'CleaningTask', task.id, f'Created: {title}')
    flash('Cleaning task created.', 'success')
    return redirect(url_for('routes.admin_cleaning'))


@bp.route('/admin/cleaning/<int:task_id>/complete', methods=['POST'])
@login_required
def admin_complete_cleaning_task(task_id):
    if not current_user.is_admin:
        abort(403)
    task = CleaningTask.query.get_or_404(task_id)
    task.status = 'completed'
    task.completed_at = datetime.utcnow()
    db.session.commit()
    flash('Task marked as completed.', 'success')
    return redirect(url_for('routes.admin_cleaning'))


@bp.route('/admin/cleaning/<int:task_id>/delete', methods=['POST'])
@login_required
def admin_delete_cleaning_task(task_id):
    if not current_user.is_admin:
        abort(403)
    task = CleaningTask.query.get_or_404(task_id)
    db.session.delete(task)
    db.session.commit()
    flash('Task deleted.', 'success')
    return redirect(url_for('routes.admin_cleaning'))


@bp.route('/admin/cleaning/auto-create', methods=['POST'])
@login_required
def admin_auto_create_cleaning():
    if not current_user.is_admin:
        abort(403)
    today = date.today()
    week_end = today + timedelta(days=7)
    checkouts = Reservation.query.filter(
        Reservation.status == 'confirmed',
        Reservation.check_out >= today,
        Reservation.check_out <= week_end,
    ).all()
    created = 0
    for res in checkouts:
        existing = CleaningTask.query.filter_by(reservation_id=res.id, scheduled_date=res.check_out).first()
        if not existing:
            task = CleaningTask(
                reservation_id=res.id,
                title=f'Turnover cleaning — #{res.id} {res.guest_name}',
                scheduled_date=res.check_out,
            )
            db.session.add(task)
            created += 1
    db.session.commit()
    flash(f'{created} cleaning tasks auto-created from checkouts.', 'success')
    return redirect(url_for('routes.admin_cleaning'))


# ── iCal Feeds ────────────────────────────────────────────────────────────────


@bp.route('/admin/ical-feeds')
@login_required
def admin_ical_feeds():
    if not current_user.is_admin:
        abort(403)
    feeds = ICalFeed.query.all()
    from app.services.ical_sync import get_blocked_dates
    blocked_dates = get_blocked_dates() or []
    return render_template('admin_ical_feeds.html', feeds=feeds, blocked_dates=blocked_dates)


@bp.route('/admin/ical-feeds/create', methods=['POST'])
@login_required
def admin_ical_create():
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


# ── Audit Log ──────────────────────────────────────────────────────────────────


@bp.route('/admin/audit-log')
@login_required
def admin_audit_log_view():
    if not current_user.is_admin:
        abort(403)
    page = request.args.get('page', 1, type=int)
    logs = AuditLog.query.order_by(AuditLog.created_at.desc()).paginate(page=page, per_page=50, error_out=False)
    return render_template('admin_audit_log.html', logs=logs)

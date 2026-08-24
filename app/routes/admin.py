import calendar
import json
import secrets
from datetime import UTC, date, datetime, timedelta

from flask import Response, abort, current_app, flash, jsonify, redirect, render_template, request, url_for
from flask_babel import gettext as _
from flask_login import current_user, login_required, login_user, logout_user

from app import db, limiter
from app.forms import ICalFeedForm, LoginForm
from app.models import (
    Apartment,
    AuditLog,
    CleaningAccess,
    Coupon,
    ICalFeed,
    Notification,
    Reservation,
    Testimonial,
    User,
)
from app.routes import bp
from app.routes.helpers import create_balance_payment_session, create_tourist_tax_payment_session, get_apartment

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


def _add_months(d: date, months: int) -> date:
    """Return the date `months` months after `d`, clamping the day to the target month."""
    total = d.month - 1 + months
    year = d.year + total // 12
    month = total % 12 + 1
    day = min(d.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def _occupancy_rate(reservations, window_start: date, window_end: date) -> float:
    """Occupancy rate = % of nights in [window_start, window_end) covered by a booking.

    Only the portion of each stay that falls inside the window counts, so stays
    starting before/after the window (or spanning its edges) don't skew the
    number. Cancelled reservations are ignored.
    """
    total_nights = (window_end - window_start).days

    occupied = set()
    for r in reservations:
        if r.status != 'confirmed':
            continue
        overlap_start = max(r.check_in, window_start)
        overlap_end = min(r.check_out, window_end)
        if overlap_end > overlap_start:
            day = overlap_start
            while day < overlap_end:
                occupied.add(day)
                day += timedelta(days=1)

    return round(len(occupied) / total_nights * 100, 1) if total_nights else 0


@bp.route('/admin')
@login_required
def admin_dashboard() -> Response | str:
    if not current_user.is_admin:
        abort(403)

    today = date.today()
    month_start = today.replace(day=1)
    year_start = today.replace(month=1, day=1)
    occupancy_end = _add_months(today, 3)

    # Cancelled reservations are excluded from the ledger and all operational
    # lists (kept in the DB for audit/compliance, just never displayed).
    reservations = (
        Reservation.query.filter(Reservation.status != 'cancelled')
        .order_by(Reservation.check_in.desc())
        .all()
    )

    confirmed = [r for r in reservations if r.status == 'confirmed']
    cancelled_count = Reservation.query.filter_by(status='cancelled').count()
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

    occupancy_rate = _occupancy_rate(reservations, today, occupancy_end)

    dashboard_data = {
        'total': len(reservations),
        'confirmed': len(confirmed),
        'cancelled': cancelled_count,
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


@bp.route('/admin/earnings', methods=['GET', 'POST'])
@login_required
def admin_earnings() -> Response | str:
    if not current_user.is_admin:
        abort(403)

    from app.models import Earning

    result = None
    error = None
    sample = request.args.get('sample') == '1'
    stored = Earning.query.order_by(Earning.payout_date.desc(), Earning.start_date.desc()).all()

    # compute stored totals for GET view
    stored_totals = None
    if stored:
        from collections import defaultdict as _dd
        _gross = sum(e.gross_earnings for e in stored)
        _amount = sum(e.amount for e in stored)
        _service = sum(e.service_fee for e in stored)
        _with = sum(e.withholding for e in stored)
        _net = sum(e.net for e in stored)
        _nights = sum(e.nights or 0 for e in stored)
        stored_totals = dict(count=len(stored), gross=_gross, amount=_amount, service=_service, withholding=_with, net=_net, nights=_nights,
                             avg_net_night=(_net/_nights if _nights else 0), avg_gross_night=(_gross/_nights if _nights else 0))

    if request.method == 'POST':
        # handle sync button separately
        if request.form.get('action') == 'sync':
            synced = 0
            for e in stored:
                if not e.reservation_id:
                    # try external_uid exact
                    res = Reservation.query.filter_by(external_uid=e.confirmation_code).first()
                    if not res and e.guest_name and e.start_date:
                        # fallback: guest token prefix + dates (fuzzy)
                        res = Reservation.query.filter(
                            Reservation.check_in == e.start_date,
                            Reservation.check_out == e.end_date,
                        ).first()
                        # also try guest name contains
                        if not res:
                            res = Reservation.query.filter(
                                Reservation.guest_name.ilike(f"%{e.guest_name.split()[0]}%"),
                                Reservation.check_in == e.start_date,
                            ).first()
                    if res:
                        e.reservation_id = res.id
                        synced += 1
            db.session.commit()
            flash(f'Synced {synced} earnings to reservations.', 'success' if synced else 'info')
            return redirect(url_for('routes.admin_earnings'))

        csv_text = request.form.get('csv_text', '').strip()
        f = request.files.get('csv_file')
        raw = None
        filename = None
        if f and f.filename:
            try:
                raw = f.read()
                filename = f.filename
            except Exception as e:
                error = f'Failed to read file: {e}'
        elif csv_text:
            raw = csv_text.encode('utf-8')
            filename = 'pasted.csv'
        else:
            error = 'Please upload a CSV file or paste CSV text.'

        if raw is not None and not error:
            try:
                from app.services.airbnb_earnings import parse_earnings_csv
                result = parse_earnings_csv(raw)
                result['filename'] = filename
                if result['totals']['count'] == 0:
                    error = 'No reservations found — check the CSV format (Airbnb Earnings export).'
                    result = None
                else:
                    # store to DB + auto-sync to Reservation
                    do_store = request.form.get('store') != '0'
                    if do_store:
                        stored_new = 0
                        synced_new = 0
                        for entry in result['per_code']:
                            code = entry['code']
                            # platform auto-detect: Airbnb codes are HMS* etc., Booking are numeric — for now airbnb
                            platform = 'airbnb'
                            # try find existing
                            earn = Earning.query.filter_by(platform=platform, confirmation_code=code).first()
                            if not earn:
                                earn = Earning(platform=platform, confirmation_code=code)
                                db.session.add(earn)
                                stored_new += 1
                            earn.guest_name = entry.get('guest')
                            earn.listing = (entry.get('reservation') or {}).get('listing') or entry.get('listing')
                            earn.start_date = entry.get('start')
                            earn.end_date = entry.get('end')
                            earn.payout_date = (entry.get('reservation') or {}).get('payout_date') or entry.get('start')
                            earn.booking_date = (entry.get('reservation') or {}).get('booking_date')  # may be None
                            # booking_date from raw if available
                            try:
                                from app.services.airbnb_earnings import _parse_date as _pd
                                earn.booking_date = _pd((entry.get('reservation') or {}).get('raw', {}).get('Booking date', ''))
                            except Exception:
                                pass
                            earn.nights = entry.get('nights')
                            earn.currency = (entry.get('reservation') or {}).get('currency', 'EUR') or 'EUR'
                            earn.amount = entry.get('amount', 0) or 0
                            earn.service_fee = entry.get('service', 0) or 0
                            earn.cleaning_fee = entry.get('cleaning', 0) or 0
                            earn.gross_earnings = entry.get('gross', 0) or 0
                            earn.airbnb_tax = entry.get('airbnb_tax', 0) or 0
                            earn.withholding = entry.get('withholding', 0) or 0
                            earn.net = entry.get('net', 0) or 0
                            # JSON-safe raw (dates → ISO strings)
                            def _j(v):
                                from datetime import date as _d, datetime as _dt
                                if isinstance(v, (_d, _dt)):
                                    return v.isoformat()
                                if isinstance(v, dict):
                                    return {k: _j(x) for k, x in v.items()}
                                if isinstance(v, list):
                                    return [_j(x) for x in v]
                                return v
                            earn.raw_json = _j(entry)
                            # auto-sync to reservation
                            if not earn.reservation_id:
                                res = Reservation.query.filter_by(external_uid=code).first()
                                if not res and earn.guest_name and earn.start_date:
                                    res = Reservation.query.filter(
                                        Reservation.check_in == earn.start_date,
                                        Reservation.check_out == earn.end_date,
                                    ).first()
                                    if not res:
                                        # looser: guest first name + start
                                        try:
                                            first = earn.guest_name.split()[0]
                                            res = Reservation.query.filter(
                                                Reservation.guest_name.ilike(f"%{first}%"),
                                                Reservation.check_in == earn.start_date,
                                            ).first()
                                        except Exception:
                                            res = None
                                if res:
                                    earn.reservation_id = res.id
                                    synced_new += 1
                        db.session.commit()
                        flash(f'Stored {len(result["per_code"])} earnings ({stored_new} new) and auto-synced {synced_new} to reservations.', 'success')
                        # refresh stored
                        stored = Earning.query.order_by(Earning.payout_date.desc(), Earning.start_date.desc()).all()
                        # recompute stored totals after store
                        _gross = sum(e.gross_earnings for e in stored)
                        _amount = sum(e.amount for e in stored)
                        _service = sum(e.service_fee for e in stored)
                        _with = sum(e.withholding for e in stored)
                        _net = sum(e.net for e in stored)
                        _nights = sum(e.nights or 0 for e in stored)
                        stored_totals = dict(count=len(stored), gross=_gross, amount=_amount, service=_service, withholding=_with, net=_net, nights=_nights,
                                             avg_net_night=(_net/_nights if _nights else 0), avg_gross_night=(_gross/_nights if _nights else 0))
            except Exception as e:
                current_app.logger.exception('Earnings parse failed')
                error = f'Failed to parse CSV: {e}'

    return render_template('admin_earnings.html', result=result, error=error, sample=sample, stored=stored, stored_totals=stored_totals)


@bp.route('/admin/smart-access')
@login_required
def admin_smart_access() -> Response | str:
    if not current_user.is_admin:
        abort(403)

    apartment = Apartment.query.first()
    if not apartment:
        apartment = Apartment(price_per_night=130.00)
        db.session.add(apartment)
        db.session.commit()

    gate_configured = bool(apartment.shelly_enabled)
    door_configured = bool(apartment.nuki_enabled)
    return render_template(
        'admin_smart_access.html',
        apartment=apartment,
        gate_configured=gate_configured,
        door_configured=door_configured,
    )


@bp.route('/admin/smart-access/preview')
@login_required
def admin_smart_access_preview() -> Response | str:
    if not current_user.is_admin:
        abort(403)

    apartment = get_apartment()
    gate_configured = bool(apartment and apartment.shelly_enabled)
    door_configured = bool(apartment and apartment.nuki_enabled)
    show_door_button = door_configured and (apartment.nuki_show_door_button is not False)

    from app.services.wifi_qr import wifi_qr_data_uri

    wifi_qr = wifi_qr_data_uri(apartment)

    sample = Reservation(
        id=9999,
        guest_name='Sample Guest',
        check_in=date.today(),
        check_out=date.today() + timedelta(days=3),
        keypad_code='123456' if door_configured else None,
        checkin_token='preview',
        access_token='preview',
    )
    return render_template(
        'guest_access.html',
        reservation=sample,
        apartment=apartment,
        gate_configured=gate_configured,
        door_configured=door_configured,
        show_door_button=show_door_button,
        wifi_qr=wifi_qr,
        preview=True,
    )


@bp.route('/admin/wifi', methods=['GET', 'POST'])
@login_required
def admin_wifi() -> Response | str:
    if not current_user.is_admin:
        abort(403)

    apartment = Apartment.query.first()
    if not apartment:
        apartment = Apartment(name='Lotto 235 Garbatella', price_per_night=130.00)
        db.session.add(apartment)
        db.session.commit()

    if request.method == 'POST':
        apartment.wifi_ssid = request.form.get('wifi_ssid', '').strip()
        apartment.wifi_password = request.form.get('wifi_password', '').strip()
        apartment.wifi_security = request.form.get('wifi_security', 'WPA').strip().upper()
        apartment.wifi_band = request.form.get('wifi_band', '').strip()
        apartment.wifi_hidden = request.form.get('wifi_hidden') == '1'
        db.session.commit()
        admin_audit_log('update_wifi', 'Apartment', apartment.id, f"Wi-Fi '{apartment.wifi_ssid}' configured")
        flash(_('Guest Wi-Fi settings saved!'), 'success')
        return redirect(url_for('routes.admin_wifi'))

    from app.services.wifi_qr import wifi_qr_data_uri

    qr_data_uri = wifi_qr_data_uri(apartment)
    preview_data = {
        'ssid': apartment.wifi_ssid,
        'password': apartment.wifi_password,
        'security': apartment.wifi_security,
        'band': apartment.wifi_band,
        'hidden': apartment.wifi_hidden,
    }
    return render_template('admin_wifi.html', apartment=apartment, qr_data_uri=qr_data_uri, preview_data=preview_data)


@bp.route('/admin/wifi/qr.png')
@login_required
def admin_wifi_qr_png() -> Response | str:
    if not current_user.is_admin:
        abort(403)

    apartment = Apartment.query.first()
    scale = request.args.get('scale', 10, type=int) or 10

    from app.services.wifi_qr import wifi_qr_bytes

    png = wifi_qr_bytes(apartment, scale=scale)
    if not png:
        abort(404)

    return Response(
        png,
        mimetype='image/png',
        headers={'Content-Disposition': 'attachment; filename="lotto235-wifi-qr.png"'},
    )


@bp.route('/admin/wifi/print')
@login_required
def admin_wifi_print() -> Response | str:
    if not current_user.is_admin:
        abort(403)

    apartment = Apartment.query.first()

    from app.services.wifi_qr import wifi_qr_data_uri

    qr_data_uri = wifi_qr_data_uri(apartment)
    if not qr_data_uri:
        flash(_('Configure the guest Wi-Fi before printing.'), 'warning')
        return redirect(url_for('routes.admin_wifi'))

    return render_template('admin_wifi_print.html', apartment=apartment, qr_data_uri=qr_data_uri)


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

        apartment.shelly_enabled = bool(request.form.get('shelly_enabled'))
        apartment.shelly_host = request.form.get('shelly_host', '').strip() or None
        apartment.shelly_relay_channel = request.form.get('shelly_relay_channel', type=int) or 0

        apartment.nuki_enabled = bool(request.form.get('nuki_enabled'))
        apartment.nuki_show_door_button = 'nuki_show_door_button' in request.form
        apartment.nuki_smartlock_id = request.form.get('nuki_smartlock_id', '').strip() or None
        apartment.nuki_web_token = request.form.get('nuki_web_token', '').strip() or None
        apartment.nuki_web_base_url = request.form.get('nuki_web_base_url', '').strip() or 'https://api.nuki.io'
        apartment.nuki_unlock_action = request.form.get('nuki_unlock_action', 'unlock')

        apartment.whatsapp_number = request.form.get('whatsapp_number', '').strip() or None
        apartment.whatsapp_default_message = request.form.get('whatsapp_default_message', '').strip() or None

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
    from app.services.smart_lock import revoke_reservation_keypad

    revoke_reservation_keypad(res)
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
    from app.services.smart_lock import revoke_reservation_keypad

    revoke_reservation_keypad(reservation)
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

    if current_app.config.get('MAIL_SUPPRESS_SEND') or current_app.config.get('TESTING'):
        current_app.logger.info('Email suppressed (TESTING): access link for #%s', res.id)
        flash('Email suppressed (test mode) — not sent to Brevo.', 'info')
        return redirect(url_for('routes.admin_dashboard'))

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


def _rome_zone():
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo('Europe/Rome')
    except Exception:
        from datetime import timedelta, timezone

        m = date.today().month
        return timezone(timedelta(hours=2 if 3 <= m <= 10 else 1))


def access_window_utc(reservation):
    """Return (start, end) UTC datetimes for the smart access window.

    Delegates to Reservation.get_access_window_utc() so custom HH:MM overrides
    are respected (default 13:00→13:00). Keeps legacy fallback for old DB rows.
    """
    if hasattr(reservation, 'get_access_window_utc'):
        try:
            return reservation.get_access_window_utc()
        except Exception:
            pass
    from datetime import datetime as _dt

    tz = _rome_zone()
    start = _dt(reservation.check_in.year, reservation.check_in.month, reservation.check_in.day, 13, 0, tzinfo=tz)
    end = _dt(reservation.check_out.year, reservation.check_out.month, reservation.check_out.day, 13, 0, tzinfo=tz)
    return start.astimezone(UTC), end.astimezone(UTC)


@bp.route('/admin/access/generate-keypad-code', methods=['POST'])
@login_required
def admin_generate_keypad_code() -> Response | str:
    if not current_user.is_admin:
        abort(403)
    res = Reservation.query.get_or_404(request.form.get('reservation_id', type=int))

    from app.services.smart_lock import SmartLockError, get_nuki_service

    apt = get_apartment()
    if not apt:
        flash('No apartment configured.', 'danger')
        return redirect(url_for('routes.admin_dashboard'))

    svc = get_nuki_service(apt)
    if not svc.is_configured():
        flash('Nuki door not configured. Set NUKI_SMARTLOCK_ID / NUKI_WEB_TOKEN or the Smart Access page.', 'danger')
        return redirect(url_for('routes.admin_dashboard'))

    try:
        start_utc, end_utc = access_window_utc(res)
        name = f'Res{res.id} {res.guest_name}'.strip()[:20]
        # If a code already exists, try to keep same PIN on regenerate
        old_code = (res.keypad_code or '').strip()
        if old_code and old_code.isdigit() and len(old_code) == 6:
            try:
                svc.revoke_keypad_code(res.keypad_auth_id)
            except SmartLockError:
                pass
            import time as _time
            _time.sleep(1)
            # Try to recreate with same number and new window
            import requests
            payload = {
                'name': name,
                'type': 13,
                'code': int(old_code),
                'smartlockIds': [int(apt.nuki_smartlock_id)],
                'allowedFromDate': start_utc.strftime('%Y-%m-%dT%H:%M:%S.000Z'),
                'allowedUntilDate': end_utc.strftime('%Y-%m-%dT%H:%M:%S.000Z'),
                'allowedWeekDays': 127,
                'allowedFromTime': 0,
                'allowedUntilTime': 0,
            }
            try:
                resp = requests.put(f'{svc.base_url}/smartlock/auth', headers=svc._get_headers(), json=payload, timeout=15)
                resp.raise_for_status()
                res.keypad_code = old_code
                res.keypad_created_at = datetime.utcnow()
                res.keypad_auth_id = svc.find_keypad_auth_id(old_code)
                db.session.commit()
                window_str = res.access_window_display() if hasattr(res, 'access_window_display') else f"13:00 {res.check_in.strftime('%d/%m/%Y')} → 13:00 {res.check_out.strftime('%d/%m/%Y')} (Rome)"
                flash(f'Keypad code {old_code} re-created for {res.guest_name} (same PIN). Valid {window_str}.', 'success')
                if not res.keypad_auth_id:
                    flash('Note: the code was accepted but not yet confirmed on the Nuki device. It will be revocable once it syncs.', 'warning')
                return redirect(url_for('routes.admin_dashboard'))
            except Exception as e:
                current_app.logger.warning('Same-code recreate failed for res #%s code %s: %s — falling back to new code', res.id, old_code, e)
                # fall through to generate new code
        # No old code or same-code path failed — generate fresh
        if res.keypad_auth_id:
            try:
                svc.revoke_keypad_code(res.keypad_auth_id)
            except SmartLockError:
                pass
        code = svc.create_keypad_code(name, start_utc, end_utc)
        res.keypad_code = code
        res.keypad_created_at = datetime.utcnow()
        res.keypad_auth_id = svc.find_keypad_auth_id(code)
        db.session.commit()
        window_str = res.access_window_display() if hasattr(res, 'access_window_display') else f"13:00 {res.check_in.strftime('%d/%m/%Y')} → 13:00 {res.check_out.strftime('%d/%m/%Y')} (Rome)"
        flash(
            f'Keypad code {code} created for {res.guest_name}. '
            f'Valid {window_str}.',
            'success',
        )
        if not res.keypad_auth_id:
            flash('Note: the code was accepted but not yet confirmed on the Nuki device. It will be revocable once it syncs.', 'warning')
    except SmartLockError as e:
        flash(f'Failed to create keypad code: {e}', 'danger')

    return redirect(url_for('routes.admin_dashboard'))


@bp.route('/admin/access/revoke-keypad-code', methods=['POST'])
@login_required
def admin_revoke_keypad_code() -> Response | str:
    if not current_user.is_admin:
        abort(403)
    res = Reservation.query.get_or_404(request.form.get('reservation_id', type=int))

    from app.services.smart_lock import SmartLockError, get_nuki_service

    apt = get_apartment()
    if not apt:
        flash('No apartment configured.', 'danger')
        return redirect(url_for('routes.admin_dashboard'))

    auth_id = res.keypad_auth_id
    if auth_id:
        try:
            svc = get_nuki_service(apt)
            svc.revoke_keypad_code(auth_id)
        except SmartLockError as e:
            flash(f'Failed to revoke keypad code on Nuki: {e}', 'danger')

    res.keypad_code = None
    res.keypad_auth_id = None
    res.keypad_created_at = None
    db.session.commit()
    flash(f'Keypad code revoked for {res.guest_name}.', 'success')
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

    # Auto-generate a Nuki keypad code for the guest's stay window when
    # opening the guest-message page, if one doesn't exist yet.
    keypad_status = 'existing' if res.keypad_code else 'skipped'
    if not res.keypad_code:
        from app.services.smart_lock import SmartLockError, get_nuki_service

        try:
            svc = get_nuki_service(apt) if apt else None
            if svc and svc.is_configured():
                start_utc, end_utc = access_window_utc(res)
                name = f'Res{res.id} {res.guest_name}'.strip()[:20]
                code = svc.create_keypad_code(name, start_utc, end_utc)
                res.keypad_code = code
                res.keypad_created_at = datetime.utcnow()
                res.keypad_auth_id = svc.find_keypad_auth_id(code)
                db.session.commit()
                keypad_status = 'created'
                if not res.keypad_auth_id:
                    keypad_status = 'created_pending_auth'
            else:
                keypad_status = 'not_configured'
        except SmartLockError as e:
            keypad_status = f'error: {e}'
    checkin_url = url_for('routes.guest_self_checkin', token=res.checkin_token, _external=True)
    access_url = url_for('routes.guest_access', token=res.access_token, _external=True)
    guide_url = url_for('routes.guest_checkin_guide', token=res.access_token, _external=True)

    apt_name = apt.name if apt else 'Lotto 235 Garbatella'
    keypad_block = f"\n\U0001f511 *Codice di accesso keypad*: {res.keypad_code}\n" if res.keypad_code else ""
    guide_block = f"\n\U0001f5fa *Come raggiungere l'appartamento* — guida al self check-in:\n{guide_url}\n"
    guide_block_en = f"\n\U0001f5fa *Self Check-in Guide* (how to reach the apartment):\n{guide_url}\n"

    from app.services.tourist_tax import TouristTaxService

    tax_service = TouristTaxService(apt)
    tax_amount = tax_service.calculate_tax(res) if apt else 0.0
    tax_link = url_for('routes.guest_tax_link', token=res.checkin_token, _external=True)
    city_tax_enabled = bool(res.guest_city_tax_enabled)
    if city_tax_enabled and tax_amount > 0 and not res.tourist_tax_paid:
        tax_block = f"""
\U0001f4b3 *Tassa di soggiorno* (€{tax_amount:.2f}) — pagala online:
{tax_link}"""
        tax_block_en = f"""
\U0001f4b3 *City Tax* (€{tax_amount:.2f}) — pay it online:
{tax_link}"""
    else:
        tax_block = ""
        tax_block_en = ""

    # Greeting label: if the guest name has been edited to a real name, use it;
    # otherwise fall back to the platform code embedded in the auto-generated
    # dashboard name (e.g. "Airbnb Guest (HMB4R8NMCZ)" → "HMB4R8NMCZ").
    import re as _re

    _name = (res.guest_name or '').strip()
    _auto = _re.fullmatch(r'(?:Airbnb|Booking|VRBO) Guest \(([^)]+)\)', _name)
    if _auto:
        guest_label = _auto.group(1)
    elif _name:
        guest_label = _name
    else:
        guest_label = res.external_uid or f'#{res.id}'

    checkin_message = f"""Ciao {guest_label},\n\nGrazie per aver prenotato presso {apt_name}!\n\nPer completare il check-in online (obbligatorio per legge italiana), clicca qui:\n{checkin_url}\n\nIl link \u00e8 valido dal {res.check_in.strftime('%d/%m/%Y')} al {res.check_out.strftime('%d/%m/%Y')}.\n\nDurante il soggiorno potrai aprire il cancello e la porta dell'appartamento da questo link:\n{access_url}{keypad_block}{tax_block}{guide_block}\n\nA presto!\n{apt_name}"""

    whatsapp_message = f"""Ciao {guest_label}! \U0001f44b\n\nGrazie per aver prenotato da {apt_name}!\n\n\U0001f511 *Check-in online (obbligatorio)*:\n{checkin_url}\n\n\U0001f6aa *Apri cancello e porta* (valido durante il soggiorno):\n{access_url}{keypad_block}{tax_block}\n\n\U0001f5fa *Come raggiungere l'appartamento* — guida al self check-in:\n{guide_url}\n\nDisponibile dal {res.check_in.strftime('%d/%m/%Y')} al {res.check_out.strftime('%d/%m/%Y')}.\n\nA presto!"""

    airbnb_message = f"""Hi {guest_label},\n\nThanks for booking at {apt_name}!\n\n\U0001f511 *Online Check-in (required by Italian law)*:\n{checkin_url}\n\n\U0001f6aa *Gate & Door Access* (valid during your stay):\n{access_url}{keypad_block}{tax_block_en}\n\n\U0001f5fa *Self Check-in Guide* (how to reach the apartment):\n{guide_url}\n\nAvailable from {res.check_in.strftime('%b %d')} to {res.check_out.strftime('%b %d, %Y')}.\n\nSee you soon!"""

    food_url = url_for('routes.food_recommendations', _external=True)
    attractions_url = url_for('routes.attractions', _external=True)
    house_rules_url = url_for('routes.house_rules', _external=True)

    message_it = f"""Benvenuto a {apt_name} ({guest_label}),

Grazie per aver scelto il nostro appartamento.

\U0001f6cd Soggiorno: dal {res.check_in.strftime('%d/%m/%Y')} al {res.check_out.strftime('%d/%m/%Y')} ({res.nights} notti)
\U0001f465 Ospiti: {res.num_guests}

\U0001f4cb CHECK-IN ONLINE (obbligatorio per legge):
{checkin_url}

\U0001f6aa APRI CANCELLO E PORTA (durante il soggiorno):
{access_url}{keypad_block}{tax_block}

\U0001f5fa COME RAGGIUNGERE L'APPARTAMENTO — guida al self check-in:
{guide_url}

\U0001f371 CIBO E BEVANDE — i nostri consigli:
{food_url}

\U0001f3d9 ATTRAZIONI — cosa vedere a Roma:
{attractions_url}

\U0001f4dc REGOLE DELLA CASA:
{house_rules_url}

A presto,
{apt_name}"""

    message_en = f"""Welcome to {apt_name} ({guest_label}),

Thank you for choosing our apartment.

\U0001f6cd Stay: from {res.check_in.strftime('%b %d, %Y')} to {res.check_out.strftime('%b %d, %Y')} ({res.nights} nights)
\U0001f465 Guests: {res.num_guests}

\U0001f4cb ONLINE CHECK-IN (required by Italian law):
{checkin_url}

\U0001f6aa OPEN GATE & DOOR (during your stay):
{access_url}{keypad_block}{tax_block_en}

\U0001f5fa SELF CHECK-IN GUIDE — how to reach the apartment:
{guide_url}

\U0001f371 FOOD & DRINKS — our recommendations:
{food_url}

\U0001f3d9 ATTRACTIONS — what to see in Rome:
{attractions_url}

\U0001f4dc HOUSE RULES:
{house_rules_url}

See you soon,
{apt_name}"""

    return render_template(
        'admin_guest_message.html',
        reservation=res,
        message_it=message_it,
        message_en=message_en,
        keypad_status=keypad_status,
        tax_amount=tax_amount,
        city_tax_enabled=city_tax_enabled,
    )


@bp.route('/admin/communication/guest-message/<int:reservation_id>/pay-tax', methods=['POST'])
@login_required
def admin_pay_tax(reservation_id: int) -> Response | str:
    """Create a Stripe checkout link to collect the city tax for a reservation."""
    if not current_user.is_admin:
        abort(403)

    res = Reservation.query.get_or_404(reservation_id)
    apt = get_apartment()

    # Compute the tax amount from the current reservation data so the link
    # always reflects the right number of guests/dates (the stored amount may
    # be stale or unset).
    from app.services.tourist_tax import TouristTaxService

    tax_service = TouristTaxService(apt)
    tax_amount = tax_service.calculate_tax(res) if apt else 0.0
    res.tourist_tax_amount = tax_amount
    db.session.commit()

    session_data = create_tourist_tax_payment_session(res)
    if not session_data:
        current_app.logger.error('Stripe city-tax charge failed for reservation #%s', res.id)
        flash('Failed to create the payment link. Check Stripe configuration or tax amount.', 'danger')
        return redirect(url_for('routes.admin_guest_message', reservation_id=reservation_id))

    admin_audit_log('city_tax_payment', 'Reservation', res.id, f'Created city tax payment link for €{tax_amount:.2f}')
    return redirect(session_data.url)


@bp.route('/admin/communication/guest-message/<int:reservation_id>/tax-cash', methods=['POST'])
@login_required
def admin_tax_cash(reservation_id: int) -> Response | str:
    """Mark the city tax as paid in cash (no Stripe involved)."""
    if not current_user.is_admin:
        abort(403)

    res = Reservation.query.get_or_404(reservation_id)
    res.tourist_tax_paid = True
    db.session.commit()
    admin_audit_log('city_tax_cash', 'Reservation', res.id, 'Marked city tax as paid in cash')
    flash('City tax marked as paid (cash).', 'success')
    return redirect(url_for('routes.admin_guest_message', reservation_id=reservation_id))


@bp.route('/admin/communication/guest-message/<int:reservation_id>/tax-unpaid', methods=['POST'])
@login_required
def admin_tax_unpaid(reservation_id: int) -> Response | str:
    """Undo a mistaken city-tax payment (cash or card)."""
    if not current_user.is_admin:
        abort(403)

    res = Reservation.query.get_or_404(reservation_id)
    res.tourist_tax_paid = False
    db.session.commit()
    admin_audit_log('city_tax_unpaid', 'Reservation', res.id, 'Marked city tax as unpaid')
    flash('City tax marked as unpaid.', 'success')
    return redirect(url_for('routes.admin_guest_message', reservation_id=reservation_id))


@bp.route('/payment/tourist-tax-success')
def tourist_tax_payment_success() -> Response | str:
    """Landing page after a Stripe city-tax checkout completes."""
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
    if meta.get('type') != 'tourist_tax':
        return redirect(url_for('routes.home'))

    res_id = int(meta.get('reservation_id', 0))
    res = Reservation.query.get(res_id)
    if res:
        res.tourist_tax_paid = True
        db.session.commit()

    flash('City tax payment received. Thank you!', 'success')
    return redirect(url_for('routes.admin_dashboard'))


@bp.route('/admin/communication/guest-message/<int:reservation_id>/update', methods=['POST'])
@login_required
def admin_guest_message_update(reservation_id: int) -> Response | str:
    if not current_user.is_admin:
        abort(403)

    res = Reservation.query.get_or_404(reservation_id)
    guest_name = request.form.get('guest_name', '').strip()
    num_guests = request.form.get('num_guests', type=int)
    guest_city_tax_enabled = request.form.get('guest_city_tax_enabled') == 'on'

    # HH:MM inputs — hours only, dates stay fixed to reservation
    def _clean_hhmm(raw: str | None) -> str | None:
        s = (raw or '').strip()
        if not s:
            return None
        try:
            h_str, m_str = s.split(':')
            h, m = int(h_str), int(m_str)
            if 0 <= h <= 23 and 0 <= m <= 59:
                return f"{h:02d}:{m:02d}"
        except Exception:
            pass
        return None

    raw_in = request.form.get('access_checkin_time', '').strip()
    raw_out = request.form.get('access_checkout_time', '').strip()
    # None means "use default 13:00" (NULL in DB); empty field resets to default
    new_in = _clean_hhmm(raw_in) if raw_in else None
    new_out = _clean_hhmm(raw_out) if raw_out else None
    # validate: if user typed something invalid, reject
    if raw_in and new_in is None:
        flash('Invalid check-in time (use HH:MM).', 'danger')
        return redirect(url_for('routes.admin_guest_message', reservation_id=reservation_id))
    if raw_out and new_out is None:
        flash('Invalid check-out time (use HH:MM).', 'danger')
        return redirect(url_for('routes.admin_guest_message', reservation_id=reservation_id))

    old_in = getattr(res, 'access_checkin_time', None)
    old_out = getattr(res, 'access_checkout_time', None)

    if guest_name:
        res.guest_name = guest_name
    if num_guests and 1 <= num_guests <= 4:
        res.num_guests = num_guests
        res.num_adults = num_guests
        res.num_children = 0
    res.guest_city_tax_enabled = guest_city_tax_enabled
    res.access_checkin_time = new_in
    res.access_checkout_time = new_out
    db.session.commit()

    window_changed = (old_in != new_in or old_out != new_out)

    # If window hours changed and a keypad code exists, re-sync it to the new window
    if window_changed and res.keypad_code:
        from app.services.smart_lock import SmartLockError, get_nuki_service
        apt = get_apartment()
        try:
            svc = get_nuki_service(apt) if apt else None
            if svc and svc.is_configured() and res.keypad_auth_id:
                start_utc, end_utc = access_window_utc(res)
                name = f'Res{res.id} {res.guest_name}'.strip()[:20]
                # Preferred: in-place update (avoids 409 race from async delete+create)
                try:
                    svc.update_keypad_code_window(res.keypad_auth_id, name, start_utc, end_utc, code=res.keypad_code)
                    flash(f'Access window updated to {res.access_window_display()}. Keypad {res.keypad_code} re-synced (same code).', 'success')
                except SmartLockError as upd_err:
                    msg = str(upd_err)
                    # Fallback: pure retry of in-place update after wait (keeps same PIN, no delete)
                    # Delete+recreate races with Nuki async and causes 409 again, so we just retry the POST update.
                    current_app.logger.warning('Keypad in-place update failed for res #%s: %s — retrying update', res.id, msg)
                    if '409' in msg or 'Conflict' in msg or '400' in msg or '429' in msg:
                        import time as _time
                        # Nuki Web → device sync is async; backoff before retrying same POST update
                        _time.sleep(5)
                        try:
                            svc.update_keypad_code_window(res.keypad_auth_id, name, start_utc, end_utc, code=res.keypad_code)
                            flash(f'Access window updated to {res.access_window_display()}. Keypad {res.keypad_code} re-synced after retry (same code).', 'success')
                            return redirect(url_for('routes.admin_guest_message', reservation_id=reservation_id))
                        except Exception as e2:
                            current_app.logger.warning('Retry update still failed for res #%s: %s', res.id, e2)
                            msg = str(e2)
                            # Final fallback: only if retry still 409, do revoke+recreate same code with poll
                            if '409' in msg or 'Conflict' in msg:
                                try:
                                    svc.revoke_keypad_code(res.keypad_auth_id)
                                except SmartLockError:
                                    pass
                                for _ in range(6):
                                    _time.sleep(3)
                                    try:
                                        if not svc.find_keypad_auth_id(res.keypad_code, attempts=1):
                                            break
                                    except Exception:
                                        pass
                                import requests
                                payload = {
                                    'name': name,
                                    'type': 13,
                                    'code': int(res.keypad_code),
                                    'smartlockIds': [int(apt.nuki_smartlock_id)],
                                    'allowedFromDate': start_utc.strftime('%Y-%m-%dT%H:%M:%S.000Z'),
                                    'allowedUntilDate': end_utc.strftime('%Y-%m-%dT%H:%M:%S.000Z'),
                                    'allowedWeekDays': 127,
                                    'allowedFromTime': 0,
                                    'allowedUntilTime': 0,
                                }
                                try:
                                    resp = requests.put(f'{svc.base_url}/smartlock/auth', headers=svc._get_headers(), json=payload, timeout=15)
                                    resp.raise_for_status()
                                    res.keypad_auth_id = svc.find_keypad_auth_id(res.keypad_code)
                                    res.keypad_created_at = datetime.utcnow()
                                    db.session.commit()
                                    flash(f'Access window updated to {res.access_window_display()}. Keypad {res.keypad_code} re-created after 409.', 'success')
                                    return redirect(url_for('routes.admin_guest_message', reservation_id=reservation_id))
                                except Exception as e3:
                                    e2 = e3
                            # Last resort: new random PIN
                            try:
                                try:
                                    svc.revoke_keypad_code(res.keypad_auth_id)
                                except Exception:
                                    pass
                                new_code = svc.create_keypad_code(name, start_utc, end_utc)
                                res.keypad_code = new_code
                                res.keypad_auth_id = svc.find_keypad_auth_id(new_code)
                                res.keypad_created_at = datetime.utcnow()
                                db.session.commit()
                                flash(f'Access window updated to {res.access_window_display()}. Old PIN conflicted (409), new keypad {new_code} generated — share it with the guest.', 'warning')
                            except Exception as e3:
                                raise SmartLockError(f'Update 409 and recreate failed: {e2}; new code also failed: {e3}')
                    else:
                        raise
            elif svc and svc.is_configured():
                # No auth_id yet (not synced) — just note; next revoke/create will use new window
                flash(f'Access window updated to {res.access_window_display()}. Keypad will use new window on next sync.', 'info')
            else:
                flash(f'Access window updated to {res.access_window_display()}. Generate a new keypad code to apply it to the door.', 'warning')
        except Exception as e:
            current_app.logger.warning('Keypad re-sync failed after window change for res #%s: %s', res.id, e)
            flash(f'Access window saved ({res.access_window_display()}), but keypad re-sync failed: {e}. Revoke and regenerate the code.', 'warning')
        admin_audit_log('update_access_window', 'Reservation', res.id,
                        f'{old_in or "13:00"}→{new_in or "13:00"} , {old_out or "13:00"}→{new_out or "13:00"}')
        return redirect(url_for('routes.admin_guest_message', reservation_id=reservation_id))

    # Even if window didn't change, detect orphaned keypad (deleted on Nuki but still in DB) and offer fix
    if not window_changed and res.keypad_code:
        from app.services.smart_lock import SmartLockError, get_nuki_service
        apt = get_apartment()
        svc = get_nuki_service(apt) if apt and apt.nuki_enabled else None
        if svc and svc.is_configured() and res.keypad_auth_id:
            try:
                found = svc.find_keypad_auth_id(res.keypad_code, attempts=1)
                if not found:
                    # Auth missing on Nuki (e.g. revoked by previous 409 flow) — clear stale id so UI shows "generate"
                    current_app.logger.warning('Orphaned keypad detected for res #%s (code %s auth %s not on Nuki) — clearing', res.id, res.keypad_code, res.keypad_auth_id)
                    res.keypad_auth_id = None
                    db.session.commit()
                    flash(f'Keypad {res.keypad_code} was deleted from Nuki (409 race). Window {res.access_window_display()} is saved. Click “Re-generate keypad” below or use Dashboard → 🔢 to recreate it with the new window.', 'warning')
                    admin_audit_log('detect_orphaned_keypad', 'Reservation', res.id, f'Code {res.keypad_code} orphaned, cleared auth_id')
                    return redirect(url_for('routes.admin_guest_message', reservation_id=reservation_id))
            except Exception:
                pass

    admin_audit_log('edit_reservation', 'Reservation', res.id, 'Updated guest message details')
    flash('Reservation details updated.', 'success')
    return redirect(url_for('routes.admin_guest_message', reservation_id=reservation_id))


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


# ── Cleaning Access ──────────────────────────────────────────────────────────


def _cleaning_gap_windows() -> list[dict]:
    """Return upcoming reservations with a computed cleaning window:
    check-out 13:00 -> next check-in 13:00 (Europe/Rome). Used to pre-fill
    the cleaning-access create form."""
    from datetime import datetime as _dt

    upcoming = (
        Reservation.query.filter(Reservation.status.in_(['pending', 'confirmed']))
        .order_by(Reservation.check_in.asc())
        .all()
    )
    windows = []
    for i, res in enumerate(upcoming):
        nxt = next((o for o in upcoming[i + 1 :] if o.check_in >= res.check_out), None)
        start = _dt(res.check_out.year, res.check_out.month, res.check_out.day, 13, 0)
        end = (
            _dt(nxt.check_in.year, nxt.check_in.month, nxt.check_in.day, 13, 0)
            if nxt
            else start + timedelta(hours=6)
        )
        windows.append(
            {
                'id': res.id,
                'guest': res.guest_name,
                'check_in': res.check_in.isoformat(),
                'check_out': res.check_out.isoformat(),
                'starts_at': start.isoformat(),
                'ends_at': end.isoformat(),
                'label': f'Cleaning {res.check_out.strftime("%d/%m")} — {res.guest_name}',
            }
        )
    return windows


def _parse_local_dt(raw: str) -> datetime | None:
    """Parse a ``datetime-local`` input into a naive Europe/Rome datetime."""
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _local_naive_to_utc(dt: datetime):
    """Convert a naive Europe/Rome datetime to aware UTC."""
    return dt.replace(tzinfo=_rome_zone()).astimezone(UTC)


@bp.route('/admin/cleaning-access')
@login_required
def admin_cleaning_access() -> Response | str:
    if not current_user.is_admin:
        abort(403)

    apartment = get_apartment()
    items = CleaningAccess.query.order_by(CleaningAccess.created_at.desc()).all()
    gaps = _cleaning_gap_windows()
    nuki_configured = bool(apartment and apartment.nuki_enabled)
    return render_template(
        'admin_cleaning_access.html',
        items=items,
        gaps=gaps,
        nuki_configured=nuki_configured,
    )


@bp.route('/admin/cleaning-access/create', methods=['POST'])
@login_required
def admin_cleaning_access_create() -> Response | str:
    if not current_user.is_admin:
        abort(403)

    label = request.form.get('label', '').strip()
    message = request.form.get('message', '').strip() or None
    starts_at = _parse_local_dt(request.form.get('starts_at', ''))
    ends_at = _parse_local_dt(request.form.get('ends_at', ''))
    reservation_id = request.form.get('reservation_id', type=int) or None

    if not label:
        flash('A label is required (e.g. "Cleaning 24/08").', 'danger')
        return redirect(url_for('routes.admin_cleaning_access'))
    if starts_at and ends_at and ends_at <= starts_at:
        flash('The end time must be after the start time.', 'danger')
        return redirect(url_for('routes.admin_cleaning_access'))

    item = CleaningAccess(
        label=label,
        message=message,
        starts_at=starts_at,
        ends_at=ends_at,
        reservation_id=reservation_id,
        auto_generated=bool(reservation_id),
        active=True,
    )
    item.generate_token()
    db.session.add(item)
    db.session.commit()
    admin_audit_log('create_cleaning_access', 'CleaningAccess', item.id, label)
    flash(f'Cleaning access "{label}" created.', 'success')
    return redirect(url_for('routes.admin_cleaning_access'))


@bp.route('/admin/cleaning-access/<int:item_id>/toggle', methods=['POST'])
@login_required
def admin_cleaning_access_toggle(item_id: int) -> Response | str:
    if not current_user.is_admin:
        abort(403)
    item = CleaningAccess.query.get_or_404(item_id)
    item.active = not item.active
    db.session.commit()
    admin_audit_log('toggle_cleaning_access', 'CleaningAccess', item.id, f'active={item.active}')
    flash(f'"{item.label}" is now {"active" if item.active else "disabled"}.', 'success')
    return redirect(url_for('routes.admin_cleaning_access'))


@bp.route('/admin/cleaning-access/<int:item_id>/generate-keypad', methods=['POST'])
@login_required
def admin_cleaning_access_generate_keypad(item_id: int) -> Response | str:
    if not current_user.is_admin:
        abort(403)
    item = CleaningAccess.query.get_or_404(item_id)

    from app.services.smart_lock import SmartLockError, get_nuki_service

    apartment = get_apartment()
    if not apartment:
        flash('No apartment configured.', 'danger')
        return redirect(url_for('routes.admin_cleaning_access'))

    svc = get_nuki_service(apartment)
    if not svc.is_configured():
        flash('Nuki door not configured. Set it up on the Smart Access page.', 'danger')
        return redirect(url_for('routes.admin_cleaning_access'))

    if not item.starts_at or not item.ends_at:
        flash('Set the start and end times before generating a keypad code.', 'danger')
        return redirect(url_for('routes.admin_cleaning_access'))

    try:
        start_utc = _local_naive_to_utc(item.starts_at)
        end_utc = _local_naive_to_utc(item.ends_at)
        name = f'Cleaning {item.label}'[:20]
        code = svc.create_keypad_code(name, start_utc, end_utc)
        item.keypad_code = code
        item.keypad_created_at = datetime.utcnow()
        item.keypad_auth_id = svc.find_keypad_auth_id(code)
        db.session.commit()
        flash(
            f'Keypad code {code} created for "{item.label}". Valid until '
            f'{item.ends_at.strftime("%d/%m/%Y %H:%M")} (Rome).',
            'success',
        )
        if not item.keypad_auth_id:
            flash('Note: accepted but not yet confirmed on the Nuki device; it will be revocable once it syncs.', 'warning')
    except SmartLockError as e:
        flash(f'Failed to create keypad code: {e}', 'danger')

    return redirect(url_for('routes.admin_cleaning_access'))


@bp.route('/admin/cleaning-access/<int:item_id>/revoke-keypad', methods=['POST'])
@login_required
def admin_cleaning_access_revoke_keypad(item_id: int) -> Response | str:
    if not current_user.is_admin:
        abort(403)
    item = CleaningAccess.query.get_or_404(item_id)

    from app.services.smart_lock import SmartLockError, get_nuki_service

    if item.keypad_auth_id:
        apartment = get_apartment()
        try:
            svc = get_nuki_service(apartment) if apartment else None
            if svc and svc.is_configured():
                svc.revoke_keypad_code(item.keypad_auth_id)
        except SmartLockError as e:
            flash(f'Failed to revoke keypad code on Nuki: {e}', 'danger')

    item.keypad_code = None
    item.keypad_auth_id = None
    item.keypad_created_at = None
    db.session.commit()
    flash(f'Keypad code revoked for "{item.label}".', 'success')
    return redirect(url_for('routes.admin_cleaning_access'))


@bp.route('/admin/cleaning-access/<int:item_id>/delete', methods=['POST'])
@login_required
def admin_cleaning_access_delete(item_id: int) -> Response | str:
    if not current_user.is_admin:
        abort(403)
    item = CleaningAccess.query.get_or_404(item_id)

    from app.services.smart_lock import SmartLockError, get_nuki_service

    if item.keypad_auth_id:
        apartment = get_apartment()
        try:
            svc = get_nuki_service(apartment) if apartment else None
            if svc and svc.is_configured():
                svc.revoke_keypad_code(item.keypad_auth_id)
        except SmartLockError:
            pass

    admin_audit_log('delete_cleaning_access', 'CleaningAccess', item.id, item.label)
    db.session.delete(item)
    db.session.commit()
    flash(f'Cleaning access "{item.label}" deleted.', 'success')
    return redirect(url_for('routes.admin_cleaning_access'))


@bp.route('/admin/cleaning-access/<int:item_id>/preview')
@login_required
def admin_cleaning_access_preview(item_id: int) -> Response | str:
    if not current_user.is_admin:
        abort(403)
    item = CleaningAccess.query.get_or_404(item_id)
    apartment = get_apartment()
    gate_configured = bool(apartment and apartment.shelly_enabled)
    return render_template(
        'cleaning_access.html',
        item=item,
        apartment=apartment,
        gate_configured=gate_configured,
        preview=True,
    )


# ── MRZ Extractor Test ────────────────────────────────────────────────────────


@bp.route('/admin/mrz-extractor', methods=['GET', 'POST'])
@login_required
def admin_mrz_extractor() -> Response | str:
    if not current_user.is_admin:
        abort(403)

    result = None
    error = None
    preview_uri = None

    if request.method == 'POST':
        file = request.files.get('document_image')
        if not file or not file.filename:
            error = _('Please choose an image or PDF to upload.')
        elif file.content_length and file.content_length > 8 * 1024 * 1024:
            error = _('File too large (max 8 MB).')
        else:
            data = file.read()
            if len(data) > 8 * 1024 * 1024:
                error = _('File too large (max 8 MB).')
            elif not data:
                error = _('The uploaded file is empty.')
            else:
                from app.services.mrz_extractor import extract_mrz, preview_data_uri

                result = extract_mrz(data)
                if result.ok:
                    preview_uri = preview_data_uri(data)

    return render_template(
        'admin_mrz_extractor.html',
        result=result,
        error=error,
        preview_uri=preview_uri,
    )

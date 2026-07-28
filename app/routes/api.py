from datetime import date, datetime

from flask import abort, flash, jsonify, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required

from app import db, limiter
from app.models import Coupon, Reservation
from app.routes import bp
from app.routes.helpers import calculate_dynamic_total, get_apartment, is_available
from app.services.smart_lock import trigger_door_unlock, trigger_gate_open

# ── API Endpoints ────────────────────────────────────────────────────────────


@bp.route('/api/validate-coupon')
def validate_coupon():
    code = request.args.get('code', '').strip().upper()
    if not code:
        return jsonify({'valid': False, 'message': 'No code provided.'})

    booking_data = session.get('booking_data')
    if not booking_data:
        return jsonify({'valid': False, 'message': 'No active booking session.'})

    coupon = Coupon.query.filter_by(code=code, active=True).first()
    if not coupon:
        return jsonify({'valid': False, 'message': 'Invalid or expired coupon code.'})

    check_in = date.fromisoformat(booking_data['check_in'])
    check_out = date.fromisoformat(booking_data['check_out'])
    num_guests = booking_data['num_guests']
    apartment = get_apartment()
    base_price = calculate_dynamic_total(
        check_in, check_out, num_guests=num_guests, base_rate=apartment.price_per_night if apartment else 130.0
    )

    if coupon.discount_type == 'percentage':
        discount_amount = round(base_price * coupon.discount_value / 100, 2)
        new_total = round(base_price - discount_amount, 2)
    elif coupon.discount_type == 'fixed':
        discount_amount = coupon.discount_value
        new_total = max(0, base_price - discount_amount)
    else:
        return jsonify({'valid': False, 'message': 'Unknown discount type.'})

    return jsonify(
        {
            'valid': True,
            'code': coupon.code,
            'discount_type': coupon.discount_type,
            'discount_value': coupon.discount_value,
            'discount_amount': discount_amount,
            'original_price': base_price,
            'new_total': new_total,
            'message': f'Coupon applied! You saved €{discount_amount:.2f}',
        }
    )


@bp.route('/api/admin/calendar-reservations')
@login_required
def api_calendar_reservations():
    if not current_user.is_admin:
        abort(403)

    reservations = Reservation.query.filter(Reservation.status != 'cancelled').all()
    events = []
    for r in reservations:
        events.append(
            {
                'title': f'{r.guest_name} ({r.num_guests} guests)',
                'start': r.check_in.isoformat(),
                'end': r.check_out.isoformat(),
                'backgroundColor': '#28a745' if r.status == 'confirmed' else '#ffc107',
                'borderColor': '#28a745' if r.status == 'confirmed' else '#ffc107',
                'allDay': True,
            }
        )

    return jsonify(events)


@bp.route('/api/calculate-price')
def api_calculate_price():
    check_in_str = request.args.get('check_in')
    check_out_str = request.args.get('check_out')
    guests = request.args.get('guests', 2, type=int)

    if not check_in_str or not check_out_str:
        return jsonify({'error': 'Missing dates'}), 400

    try:
        check_in = date.fromisoformat(check_in_str)
        check_out = date.fromisoformat(check_out_str)
    except (ValueError, TypeError):
        return jsonify({'error': 'Invalid date format (use YYYY-MM-DD)'}), 400

    if check_out <= check_in:
        return jsonify({'error': 'Check-out must be after check-in'}), 400

    if not is_available(check_in, check_out):
        return jsonify({'error': 'Dates not available', 'available': False}), 409

    apartment = get_apartment()
    base_rate = apartment.price_per_night if apartment else 130.0
    total = calculate_dynamic_total(check_in, check_out, num_guests=guests, base_rate=base_rate)

    return jsonify(
        {
            'available': True,
            'check_in': check_in_str,
            'check_out': check_out_str,
            'nights': (check_out - check_in).days,
            'guests': guests,
            'base_rate': base_rate,
            'total': total,
            'currency': 'EUR',
        }
    )


# ── Smart Lock APIs ──────────────────────────────────────────────────────────


@bp.route('/api/access/gate/open', methods=['POST'])
@limiter.limit('10 per minute')
def api_gate_open():
    token = request.headers.get('X-Access-Token') or request.form.get('token')
    if not token:
        return jsonify({'ok': False, 'error': 'Missing token'}), 401

    res = Reservation.query.filter_by(access_token=token).first()
    if not res:
        return jsonify({'ok': False, 'error': 'Invalid token'}), 403

    today = date.today()
    if not (res.check_in <= today <= res.check_out):
        return jsonify({'ok': False, 'error': 'Access not allowed outside stay dates'}), 403

    apt = get_apartment()
    ok, msg = trigger_gate_open(apt)
    return jsonify({'ok': ok, 'message': msg})


@bp.route('/api/access/door/open', methods=['POST'])
@limiter.limit('10 per minute')
def api_door_open():
    token = request.headers.get('X-Access-Token') or request.form.get('token')
    if not token:
        return jsonify({'ok': False, 'error': 'Missing token'}), 401

    res = Reservation.query.filter_by(access_token=token).first()
    if not res:
        return jsonify({'ok': False, 'error': 'Invalid token'}), 403

    today = date.today()
    if not (res.check_in <= today <= res.check_out):
        return jsonify({'ok': False, 'error': 'Access not allowed outside stay dates'}), 403

    apt = get_apartment()
    ok, msg = trigger_door_unlock(apt)
    return jsonify({'ok': ok, 'message': msg})


# ── Guest Pages (token-based) ────────────────────────────────────────────────


@bp.route('/checkin/<token>', methods=['GET', 'POST'])
def guest_self_checkin(token):
    reservation = Reservation.query.filter_by(checkin_token=token).first_or_404()

    if reservation.checkin_token_used and reservation.checkin_completed_at:
        return render_template('guest_self_checkin.html', reservation=reservation, already_completed=True)

    if request.method == 'POST':
        reservation.guest_surname = request.form.get('surname', '').strip()
        reservation.guest_first_name = request.form.get('first_name', '').strip()
        birth_date_str = request.form.get('birth_date', '').strip()
        if birth_date_str:
            try:
                reservation.guest_birth_date = date.fromisoformat(birth_date_str)
            except ValueError:
                flash('Invalid birth date format.', 'danger')
                return render_template('guest_self_checkin.html', reservation=reservation)
        reservation.guest_birth_place = request.form.get('birth_place', '').strip()
        reservation.guest_nationality = request.form.get('nationality', '').strip()
        reservation.guest_document_type = request.form.get('document_type', '').strip()
        reservation.guest_document_number = request.form.get('document_number', '').strip()
        doc_expiry_str = request.form.get('document_expiry', '').strip()
        if doc_expiry_str:
            try:
                reservation.guest_document_expiry = date.fromisoformat(doc_expiry_str)
            except ValueError:
                flash('Invalid document expiry date.', 'danger')
                return render_template('guest_self_checkin.html', reservation=reservation)
        reservation.guest_document_country = request.form.get('document_country', '').strip()
        reservation.guest_gender = request.form.get('gender', '').strip()

        reservation.checkin_completed_at = datetime.utcnow()
        reservation.checkin_token_used = True
        db.session.commit()

        flash('Check-in completed successfully!', 'success')
        return redirect(url_for('routes.guest_portal', token=reservation.checkin_token))

    return render_template('guest_self_checkin.html', reservation=reservation)


@bp.route('/access/<token>')
def guest_access(token):
    reservation = Reservation.query.filter_by(access_token=token).first_or_404()
    apartment = get_apartment()
    gate_configured = bool(apartment and apartment.shelly_enabled)
    door_configured = bool(apartment and apartment.nuki_enabled)
    return render_template('guest_access.html',
        reservation=reservation,
        apartment=apartment,
        gate_configured=gate_configured,
        door_configured=door_configured,
    )


@bp.route('/portal/<token>')
def guest_portal(token):
    reservation = Reservation.query.filter_by(checkin_token=token).first_or_404()
    apartment = get_apartment()
    today = date.today()

    show_checkin = not (reservation.checkin_token_used and reservation.checkin_completed_at)
    in_stay = reservation.check_in <= today <= reservation.check_out
    show_access = bool(reservation.access_token) and in_stay

    access_token = reservation.access_token
    gate_configured = bool(apartment and apartment.shelly_enabled) if apartment else False
    door_configured = bool(apartment and apartment.nuki_enabled) if apartment else False

    return render_template('guest_portal.html',
        reservation=reservation,
        apartment=apartment,
        show_checkin=show_checkin,
        show_access=show_access,
        access_token=access_token,
        gate_configured=gate_configured,
        door_configured=door_configured,
    )

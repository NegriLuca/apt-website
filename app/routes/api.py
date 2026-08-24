from datetime import date, datetime, timezone

from flask import abort, current_app, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app import csrf, db, limiter
from app.models import CleaningAccess, Coupon, Reservation
from app.routes import bp
from app.routes.helpers import calculate_dynamic_total, get_apartment, is_available
from app.services.email_service import send_admin_checkin_notification
from app.services.smart_lock import SmartLockError, get_nuki_service, get_shelly_service

# ── API Endpoints ────────────────────────────────────────────────────────────


def _abort_cancelled():
    """Return 404 for token links of cancelled reservations (indistinguishable
    from a missing/never-sent link)."""
    abort(404)


@bp.route('/api/validate-coupon')
def validate_coupon():
    code = request.args.get('code', '').strip().upper()
    if not code:
        return jsonify({'valid': False, 'message': 'No code provided.'})

    subtotal = request.args.get('subtotal', type=float)
    if subtotal is None or subtotal <= 0:
        return jsonify({'valid': False, 'message': 'Please select a date range first.'})

    coupon = Coupon.query.filter_by(code=code, active=True).first()
    if not coupon:
        return jsonify({'valid': False, 'message': 'Invalid or expired coupon code.'})

    if coupon.discount_type == 'percentage':
        discount_amount = round(subtotal * coupon.discount_value / 100, 2)
        new_total = round(subtotal - discount_amount, 2)
    elif coupon.discount_type == 'fixed':
        discount_amount = min(subtotal, coupon.discount_value)
        new_total = max(0, subtotal - coupon.discount_value)
    else:
        return jsonify({'valid': False, 'message': 'Unknown discount type.'})

    return jsonify(
        {
            'valid': True,
            'code': coupon.code,
            'discount_type': coupon.discount_type,
            'discount_value': coupon.discount_value,
            'discount_amount': discount_amount,
            'original_price': subtotal,
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
        if r.is_block:
            color = '#6c757d'
            title = f'{r.guest_name} ({r.source}, block)'
        else:
            color = '#28a745' if r.status == 'confirmed' else '#ffc107'
            method_label = (r.payment_method or 'n/a').upper()
            title = f'{r.guest_name} ({r.num_guests} guests, {method_label})'
        events.append(
            {
                'id': r.id,
                'title': title,
                'start': r.check_in.isoformat(),
                'end': r.check_out.isoformat(),
                'backgroundColor': color,
                'borderColor': color,
                'allDay': True,
                'extendedProps': {
                    'source': r.source or 'direct',
                    'status': r.status,
                    'payment_status': r.payment_status or 'unpaid',
                    'payment_method': r.payment_method or 'n/a',
                    'total': f'€{r.total_price:.2f}' if r.total_price else '€0',
                },
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
@csrf.exempt
def api_gate_open():
    token = request.headers.get('X-Access-Token') or request.form.get('token')
    if not token:
        return jsonify({'ok': False, 'error': 'Missing token'}), 401

    res = Reservation.query.filter_by(access_token=token).first()
    if not res:
        return jsonify({'ok': False, 'error': 'Invalid token'}), 403

    if not res.is_access_valid():
        reason = res.access_window_display() if hasattr(res, 'access_window_display') else 'Access valid from 13:00 on check-in day to 13:00 on check-out day (Rome time).'
        return jsonify({'ok': False, 'error': f'Access not allowed outside stay dates. Valid {reason}.'}), 403

    apt = get_apartment()
    if not apt:
        return jsonify({'ok': False, 'error': 'No apartment configured.'}), 400

    svc = get_shelly_service(apt)
    current_app.logger.warning(
        'gate open: cloud_mode=%s server_set=%s key_set=%s device_id=%r host=%r channel=%s',
        svc.in_cloud_mode, bool(svc.cloud_server), bool(svc.cloud_key),
        svc.cloud_device_id, apt.shelly_host, svc.channel,
    )
    try:
        ok, msg = svc.pulse_relay()
    except SmartLockError as e:
        return jsonify({'ok': False, 'error': str(e)}), 400
    return jsonify({'ok': ok, 'message': msg})


@bp.route('/api/access/door/open', methods=['POST'])
@limiter.limit('10 per minute')
@csrf.exempt
def api_door_open():
    token = request.headers.get('X-Access-Token') or request.form.get('token')
    if not token:
        return jsonify({'ok': False, 'error': 'Missing token'}), 401

    res = Reservation.query.filter_by(access_token=token).first()
    if not res:
        return jsonify({'ok': False, 'error': 'Invalid token'}), 403

    if not res.is_access_valid():
        reason = res.access_window_display() if hasattr(res, 'access_window_display') else 'Access valid from 13:00 on check-in day to 13:00 on check-out day (Rome time).'
        return jsonify({'ok': False, 'error': f'Access not allowed outside stay dates. Valid {reason}.'}), 403

    apt = get_apartment()
    if not apt:
        return jsonify({'ok': False, 'error': 'No apartment configured.'}), 400

    svc = get_nuki_service(apt)
    try:
        ok, msg = svc.unlock()
    except SmartLockError as e:
        return jsonify({'ok': False, 'error': str(e)}), 400
    return jsonify({'ok': ok, 'message': msg})


# ── Guest Pages (token-based) ────────────────────────────────────────────────


@bp.route('/checkin/<token>', methods=['GET', 'POST'])
def guest_self_checkin(token):
    reservation = Reservation.query.filter_by(checkin_token=token).first_or_404()

    if reservation.status == 'cancelled':
        return _abort_cancelled()

    if reservation.checkin_token_used and reservation.checkin_completed_at:
        return render_template('guest_self_checkin.html', reservation=reservation, already_completed=True)

    if request.method == 'POST':
        REQUIRED_GUEST_FIELDS = [
            'surname', 'first_name', 'birth_date', 'birth_place',
            'nationality', 'gender', 'document_type', 'document_number',
            'document_expiry', 'document_country',
        ]

        def _guest_incomplete(data):
            return [f for f in REQUIRED_GUEST_FIELDS if not data.get(f)]

        def _guest_data(prefix: str):
            birth_date_str = request.form.get(f'{prefix}birth_date', '').strip()
            doc_expiry_str = request.form.get(f'{prefix}document_expiry', '').strip()
            try:
                birth_date = date.fromisoformat(birth_date_str) if birth_date_str else None
            except ValueError:
                return None
            try:
                doc_expiry = date.fromisoformat(doc_expiry_str) if doc_expiry_str else None
            except ValueError:
                return None
            return {
                'surname': request.form.get(f'{prefix}surname', '').strip(),
                'first_name': request.form.get(f'{prefix}first_name', '').strip(),
                'birth_date': birth_date,
                'birth_place': request.form.get(f'{prefix}birth_place', '').strip(),
                'nationality': request.form.get(f'{prefix}nationality', '').strip(),
                'gender': request.form.get(f'{prefix}gender', '').strip(),
                'document_type': request.form.get(f'{prefix}document_type', '').strip(),
                'document_number': request.form.get(f'{prefix}document_number', '').strip(),
                'document_expiry': doc_expiry,
                'document_country': request.form.get(f'{prefix}document_country', '').strip(),
            }

        def _reject(message):
            flash(message, 'danger')
            return render_template('guest_self_checkin.html', reservation=reservation)

        main = _guest_data('guest_0_')
        if main is None:
            return _reject('Invalid birth date format.')
        if main.get('document_expiry') is None:
            return _reject('Invalid document expiry date.')
        missing_main = _guest_incomplete(main)
        if missing_main:
            return _reject('Please fill in all required guest details before completing check-in.')

        companions = []
        for g in range(1, reservation.num_guests):
            data = _guest_data(f'guest_{g}_')
            if data is None:
                return _reject('Invalid birth date format.')
            if data.get('document_expiry') is None:
                return _reject('Invalid document expiry date.')
            missing = _guest_incomplete(data)
            if missing:
                return _reject('Please fill in all required guest details before completing check-in.')
            companions.append({k: (v.isoformat() if hasattr(v, 'isoformat') else v) for k, v in data.items()})
        reservation.companions = companions or None

        reservation.guest_surname = main['surname']
        reservation.guest_first_name = main['first_name']
        reservation.guest_birth_date = main['birth_date']
        reservation.guest_birth_place = main['birth_place']
        reservation.guest_nationality = main['nationality']
        reservation.guest_document_type = main['document_type']
        reservation.guest_document_number = main['document_number']
        reservation.guest_document_expiry = main['document_expiry']
        reservation.guest_document_country = main['document_country']
        reservation.guest_gender = main['gender']

        reservation.checkin_completed_at = datetime.utcnow()
        reservation.checkin_token_used = True
        db.session.commit()

        try:
            send_admin_checkin_notification(reservation)
        except Exception:
            current_app.logger.exception('Admin check-in notification failed')

        flash('Check-in completed successfully!', 'success')
        return redirect(url_for('routes.guest_portal', token=reservation.checkin_token))

    tax_amount = 0.0
    city_tax_enabled = bool(reservation.guest_city_tax_enabled)
    if reservation.status == 'confirmed' and city_tax_enabled:
        apartment = get_apartment()
        if apartment:
            from app.services.tourist_tax import TouristTaxService
            tax_amount = TouristTaxService(apartment).calculate_tax(reservation)
    return render_template(
        'guest_self_checkin.html',
        reservation=reservation,
        tax_amount=tax_amount,
        city_tax_enabled=city_tax_enabled,
    )


@bp.route('/checkin/<token>/pay-tax', methods=['POST'])
def guest_pay_tax(token):
    reservation = Reservation.query.filter_by(checkin_token=token).first_or_404()
    return _guest_pay_tax(reservation)


@bp.route('/checkin/<token>/tax-link')
def guest_tax_link(token):
    """GET link (for embedding in messages) that creates the Stripe session."""
    reservation = Reservation.query.filter_by(checkin_token=token).first_or_404()
    return _guest_pay_tax(reservation)


def _guest_pay_tax(reservation):
    if reservation.status == 'cancelled':
        return _abort_cancelled()

    apartment = get_apartment()
    if not apartment:
        flash('City tax payment is not available right now.', 'danger')
        return redirect(url_for('routes.guest_self_checkin', token=reservation.checkin_token))

    if not reservation.guest_city_tax_enabled:
        flash('Online city tax payment is not enabled for this reservation.', 'info')
        return redirect(url_for('routes.guest_self_checkin', token=reservation.checkin_token))

    from app.routes.helpers import create_tourist_tax_payment_session
    from app.services.tourist_tax import TouristTaxService

    tax_service = TouristTaxService(apartment)
    reservation.tourist_tax_amount = tax_service.calculate_tax(reservation)
    db.session.commit()

    session_data = create_tourist_tax_payment_session(reservation)
    if not session_data:
        flash('Failed to create the payment link. Check Stripe configuration or tax amount.', 'danger')
        return redirect(url_for('routes.guest_self_checkin', token=reservation.checkin_token))
    return redirect(session_data.url)


@bp.route('/access/<token>')
def guest_access(token):
    reservation = Reservation.query.filter_by(access_token=token).first_or_404()
    if reservation.status == 'cancelled':
        return _abort_cancelled()
    if not reservation.is_access_valid():
        window = reservation.access_window_display() if hasattr(reservation, 'access_window_display') else '13:00 check-in → 13:00 check-out (Rome)'
        return render_template('guest_access_denied.html',
            reservation=reservation,
            reason=f'Access valid {window}. Your current access window has ended or not yet started.',
        ), 403
    apartment = get_apartment()
    gate_configured = bool(apartment and apartment.shelly_enabled)
    door_configured = bool(apartment and apartment.nuki_enabled)
    show_door_button = door_configured and (apartment.nuki_show_door_button is not False)

    from app.services.wifi_qr import wifi_qr_data_uri

    wifi_qr = wifi_qr_data_uri(apartment)
    return render_template('guest_access.html',
        reservation=reservation,
        apartment=apartment,
        gate_configured=gate_configured,
        door_configured=door_configured,
        show_door_button=show_door_button,
        wifi_qr=wifi_qr,
    )


# ── Cleaning Access (token-based shareable link) ─────────────────────────────


@bp.route('/cleaning-access/<token>')
def cleaning_access_page(token):
    item = CleaningAccess.query.filter_by(token=token).first_or_404()
    if not item.is_now_active():
        return render_template('cleaning_access_denied.html', item=item), 403
    apartment = get_apartment()
    gate_configured = bool(apartment and apartment.shelly_enabled)
    return render_template(
        'cleaning_access.html',
        item=item,
        apartment=apartment,
        gate_configured=gate_configured,
        preview=False,
    )


@bp.route('/api/cleaning-access/gate/open', methods=['POST'])
@limiter.limit('10 per minute')
@csrf.exempt
def api_cleaning_gate_open():
    token = request.headers.get('X-Access-Token') or request.form.get('token')
    item = CleaningAccess.query.filter_by(token=token).first() if token else None
    if not item:
        return jsonify({'ok': False, 'error': 'Invalid token'}), 403
    if not item.is_now_active():
        return jsonify({'ok': False, 'error': 'This cleaning access link is disabled.'}), 403

    apt = get_apartment()
    if not apt:
        return jsonify({'ok': False, 'error': 'No apartment configured.'}), 400
    try:
        ok, msg = get_shelly_service(apt).pulse_relay()
    except SmartLockError as e:
        return jsonify({'ok': False, 'error': str(e)}), 400
    return jsonify({'ok': ok, 'message': msg})


@bp.route('/checkin-guide/<token>')
def guest_checkin_guide(token):
    """Self check-in guide page, personalised with the guest's keypad code.

    Uses the reservation's access token (same link family as the guest access
    page) so the host can share it before check-in. Not restricted to the stay
    window on purpose — guests should read it while travelling to the flat.
    The gate button on the page is only enabled during the stay window.
    """
    reservation = Reservation.query.filter_by(access_token=token).first_or_404()
    if reservation.status == 'cancelled':
        return _abort_cancelled()
    apartment = get_apartment()

    # Use reservation's configured window (HH:MM overrides, default 13:00)
    try:
        window_start, window_end = reservation.get_access_window()
    except Exception:
        try:
            from zoneinfo import ZoneInfo
            rome = ZoneInfo('Europe/Rome')
        except Exception:
            rome = timezone.utc
        window_start = datetime(reservation.check_in.year, reservation.check_in.month, reservation.check_in.day, 13, 0, tzinfo=rome)
        window_end = datetime(reservation.check_out.year, reservation.check_out.month, reservation.check_out.day, 13, 0, tzinfo=rome)

    return render_template(
        'checkin_guide.html',
        reservation=reservation,
        apartment=apartment,
        access_token=reservation.access_token,
        window_start_iso=window_start.astimezone(timezone.utc).isoformat(),
        window_end_iso=window_end.astimezone(timezone.utc).isoformat(),
    )


@bp.route('/portal/<token>')
def guest_portal(token):
    from datetime import date

    reservation = Reservation.query.filter_by(checkin_token=token).first_or_404()
    if reservation.status == 'cancelled':
        return _abort_cancelled()
    apartment = get_apartment()
    today = date.today()

    show_checkin = not (reservation.checkin_token_used and reservation.checkin_completed_at)
    show_access = bool(reservation.access_token) and reservation.is_access_valid()

    access_token = reservation.access_token
    gate_configured = bool(apartment and apartment.shelly_enabled) if apartment else False
    door_configured = bool(apartment and apartment.nuki_enabled) if apartment else False
    show_door_button = door_configured and (apartment.nuki_show_door_button is not False)

    from app.services.tourist_tax import TouristTaxService

    city_tax_enabled = bool(reservation.guest_city_tax_enabled)
    tax_amount = 0.0
    if city_tax_enabled and reservation.status == 'confirmed':
        tax_amount = TouristTaxService(apartment).calculate_tax(reservation)

    from app.services.wifi_qr import wifi_qr_data_uri

    wifi_qr = wifi_qr_data_uri(apartment)
    return render_template('guest_portal.html',
        reservation=reservation,
        apartment=apartment,
        show_checkin=show_checkin,
        show_access=show_access,
        access_token=access_token,
        gate_configured=gate_configured,
        door_configured=door_configured,
        show_door_button=show_door_button,
        access_preview=False,
        today=today,
        city_tax_enabled=city_tax_enabled,
        tax_amount=tax_amount,
        wifi_qr=wifi_qr,
    )

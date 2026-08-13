import secrets
from datetime import date, timedelta

import stripe
from flask import abort, current_app, flash, redirect, render_template, request, session, url_for
from flask_babel import gettext as _

from app import csrf, db
from app.forms import ReservationForm
from app.models import Coupon, Reservation
from app.routes import bp
from app.routes.helpers import (
    _send_confirmation_emails,
    apply_full_payment_discount,
    calculate_city_tax,
    calculate_dynamic_total,
    calculate_refund_percentage,
    get_apartment,
    is_available,
    send_cancellation_emails,
    send_payment_verified_email,
    send_pending_payment_email,
)
from app.services.tourist_tax import get_tax_service


@bp.route('/reserve', methods=['GET', 'POST'])
def reserve():
    apartment = get_apartment()
    form = ReservationForm()

    if not apartment:
        return render_template('reservation.html', form=form, apartment=None, disabled_dates=[])

    reservations = Reservation.query.filter(Reservation.status != 'cancelled').all()

    disabled_dates = []
    checkin_blocked = []
    for r in reservations:
        checkin_blocked.append(r.check_in.isoformat())
        current = r.check_in + timedelta(days=1)
        last_night = r.check_out - timedelta(days=1)
        while current <= last_night:
            disabled_dates.append(current.isoformat())
            current += timedelta(days=1)

    if form.validate_on_submit():
        check_in = form.check_in.data
        check_out = form.check_out.data

        if check_out <= check_in:
            flash('Check-out must be after check-in.', 'danger')
            return redirect(request.url)

        nights = (check_out - check_in).days
        if nights > 28:
            flash('You can book a maximum of 28 nights.', 'danger')
            return redirect(request.url)

        session.pop('pending_reservation', None)

        if not is_available(check_in, check_out):
            flash('Selected dates are not available.', 'danger')
            return redirect(request.url)

        coupon_code = request.form.get('applied_coupon_code', '').strip().upper()
        num_adults = form.num_adults.data
        num_children = form.num_children.data
        num_guests = num_adults + num_children

        max_guests = apartment.max_guests or 4
        if num_guests > max_guests:
            flash(f'Maximum {max_guests} guests allowed.', 'danger')
            return redirect(request.url)

        base_total = calculate_dynamic_total(
            check_in, check_out, num_guests=num_guests, base_rate=apartment.price_per_night
        )
        final_total = base_total
        validated_code = None

        if coupon_code:
            coupon = Coupon.query.filter_by(code=coupon_code, active=True).first()
            if coupon:
                final_total = coupon.apply_discount(base_total)
                validated_code = coupon.code

        session['pending_reservation'] = {
            'guest_name': form.guest_name.data,
            'guest_email': form.guest_email.data,
            'check_in': check_in.isoformat(),
            'check_out': check_out.isoformat(),
            'num_guests': num_guests,
            'num_adults': num_adults,
            'num_children': num_children,
            'base_total': base_total,
            'total_price': final_total,
            'coupon_code': validated_code,
        }
        return redirect(url_for('routes.checkout'))

    return render_template(
        'reservation.html',
        form=form,
        apartment=apartment,
        disabled_dates=disabled_dates,
        checkin_blocked=checkin_blocked,
    )


@bp.route('/checkout')
def checkout():
    pending = session.get('pending_reservation')
    if not pending:
        flash(_('Please fill in the booking form first.'), 'warning')
        return redirect(url_for('routes.reserve'))

    apartment = get_apartment()
    check_in = date.fromisoformat(pending['check_in'])
    check_out = date.fromisoformat(pending['check_out'])
    nights = (check_out - check_in).days

    base_rate = apartment.price_per_night if apartment else 0
    num_guests = pending.get('num_guests', 2)
    num_adults = pending.get('num_adults', num_guests)
    num_children = pending.get('num_children', 0)
    calculated_base = calculate_dynamic_total(check_in, check_out, num_guests=num_guests, base_rate=base_rate)

    base_total = pending.get('base_total', calculated_base)
    total_price = pending.get('total_price', base_total)

    extra_guests = max(0, num_guests - 2)
    guest_surcharge_per_night = extra_guests * 15.0
    guest_surcharge_total = guest_surcharge_per_night * nights
    discount_pct = 10 if nights >= 7 else 0

    city_tax = calculate_city_tax(check_in, check_out, num_adults, apartment)
    city_tax_rate = get_tax_service(apartment).rate if apartment else 6.0
    stay_cost = round(total_price - city_tax, 2)
    full_pay_total = apply_full_payment_discount(total_price)
    full_pay_savings = round(total_price - full_pay_total, 2)
    deposit_total = round(total_price * 0.3, 2)

    stripe_pub = current_app.config.get('STRIPE_PUBLISHABLE_KEY', '')

    return render_template(
        'checkout.html',
        pending=pending,
        apartment=apartment,
        nights=nights,
        base_rate=base_rate,
        base_total=base_total,
        total=total_price,
        city_tax=city_tax,
        city_tax_rate=city_tax_rate,
        stay_cost=stay_cost,
        full_pay_discount_pct=5,
        full_pay_total=full_pay_total,
        full_pay_savings=full_pay_savings,
        deposit_total=deposit_total,
        num_guests=num_guests,
        num_adults=num_adults,
        num_children=num_children,
        extra_guests=extra_guests,
        guest_surcharge_per_night=guest_surcharge_per_night,
        guest_surcharge_total=guest_surcharge_total,
        discount_pct=discount_pct,
        stripe_publishable_key=stripe_pub,
        check_in=check_in,
        check_out=check_out,
    )


@bp.route('/process-payment', methods=['POST'])
def process_payment():
    method = request.form.get('payment_method')

    if method == 'stripe':
        return create_checkout_session()

    elif method == 'wire_transfer':
        pending = session.get('pending_reservation')
        if not pending:
            flash(_('Session expired. Please try again.'), 'danger')
            return redirect(url_for('routes.reserve'))

        apartment = get_apartment()
        check_in_dt = date.fromisoformat(pending['check_in'])
        check_out_dt = date.fromisoformat(pending['check_out'])

        base_rate = apartment.price_per_night if apartment else 0
        num_guests = int(pending['num_guests'])
        num_adults = int(pending.get('num_adults', num_guests))
        num_children = int(pending.get('num_children', 0))
        fallback_total = calculate_dynamic_total(check_in_dt, check_out_dt, num_guests=num_guests, base_rate=base_rate)
        full_total = pending.get('total_price', fallback_total)
        total_price = apply_full_payment_discount(full_total)
        tourist_tax_amount = calculate_city_tax(check_in_dt, check_out_dt, num_adults, apartment)

        new_reservation = Reservation(
            guest_name=pending['guest_name'],
            guest_email=pending['guest_email'],
            check_in=check_in_dt,
            check_out=check_out_dt,
            num_guests=num_guests,
            num_adults=num_adults,
            num_children=num_children,
            status='pending',
            source='direct',
            total_price=total_price,
            coupon_code=pending.get('coupon_code'),
            payment_status='unpaid',
            payment_method='wire_transfer',
            cancel_token=secrets.token_urlsafe(32),
            tourist_tax_amount=tourist_tax_amount,
        )
        if not new_reservation.access_token:
            new_reservation.generate_access_token()

        db.session.add(new_reservation)
        db.session.commit()

        send_pending_payment_email(new_reservation)

        session['completed_wire_res_id'] = new_reservation.id
        session['completed_wire_total'] = new_reservation.total_price

        session.pop('pending_reservation', None)
        return redirect(url_for('routes.wire_transfer_instructions'))

    return redirect(url_for('routes.checkout'))


@bp.route('/checkout/wire-transfer')
def wire_transfer_instructions():
    reservation_id = session.get('completed_wire_res_id')
    total = session.get('completed_wire_total')

    if not reservation_id:
        return redirect(url_for('routes.home'))

    session.pop('completed_wire_res_id', None)
    session.pop('completed_wire_total', None)

    return render_template('wire_transfer.html', reservation_id=reservation_id, total=total)


@bp.route('/checkout/create-session', methods=['POST'])
def create_checkout_session():
    pending = session.get('pending_reservation')
    if not pending:
        flash(_('Session expired. Please try again.'), 'danger')
        return redirect(url_for('routes.reserve'))

    apartment = get_apartment()
    check_in_dt = date.fromisoformat(pending['check_in'])
    check_out_dt = date.fromisoformat(pending['check_out'])

    base_rate = apartment.price_per_night if apartment else 0
    num_guests = int(pending['num_guests'])
    num_adults = int(pending.get('num_adults', num_guests))
    num_children = int(pending.get('num_children', 0))
    fallback_total = calculate_dynamic_total(check_in_dt, check_out_dt, num_guests=num_guests, base_rate=base_rate)

    stripe_amount = request.form.get('stripe_amount', 'full')
    full_total = pending.get('total_price', fallback_total)
    is_deposit = stripe_amount == 'deposit'
    if is_deposit:
        amount_to_charge = round(full_total * 0.3, 2)
        invoice_total = full_total
    else:
        amount_to_charge = apply_full_payment_discount(full_total)
        invoice_total = amount_to_charge

    stripe.api_key = current_app.config.get('STRIPE_SECRET_KEY')
    if not stripe.api_key:
        abort(500, 'Stripe secret key is not configured.')

    checkout_session = stripe.checkout.Session.create(
        line_items=[
            {
                'price_data': {
                    'currency': 'eur',
                    'product_data': {'name': 'Apartment Booking'},
                    'unit_amount': int(amount_to_charge * 100),
                },
                'quantity': 1,
            }
        ],
        mode='payment',
        success_url=url_for('routes.payment_success', _external=True) + '?session_id={CHECKOUT_SESSION_ID}',
        cancel_url=url_for('routes.checkout', _external=True),
        customer_email=pending.get('guest_email'),
        metadata={
            'guest_name': pending.get('guest_name', 'Guest'),
            'guest_email': pending.get('guest_email', ''),
            'check_in': pending['check_in'],
            'check_out': pending['check_out'],
            'num_guests': str(num_guests),
            'num_adults': str(num_adults),
            'num_children': str(num_children),
            'total_price': str(invoice_total),
            'amount_paid': str(amount_to_charge),
            'is_deposit': str(is_deposit),
            'coupon_code': pending.get('coupon_code', ''),
        },
    )

    session['pending_stripe_session'] = {
        'guest_name': pending.get('guest_name', 'Guest'),
        'guest_email': pending.get('guest_email', ''),
        'check_in': pending['check_in'],
        'check_out': pending['check_out'],
        'num_guests': str(num_guests),
        'num_adults': str(num_adults),
        'num_children': str(num_children),
        'total_price': str(invoice_total),
        'amount_paid': str(amount_to_charge),
        'is_deposit': str(is_deposit),
        'coupon_code': pending.get('coupon_code', ''),
    }

    return redirect(checkout_session.url, 303)


@bp.route('/payment/success')
def payment_success():
    session_id = request.args.get('session_id')
    if not session_id:
        return redirect(url_for('routes.home'))

    stripe.api_key = current_app.config.get('STRIPE_SECRET_KEY')
    try:
        checkout_session = stripe.checkout.Session.retrieve(session_id, expand=['line_items'])
    except Exception:
        flash(_('Payment verification failed. Please contact support.'), 'danger')
        return redirect(url_for('routes.home'))

    reservation = _create_reservation_from_stripe(checkout_session)
    session.pop('pending_reservation_id', None)
    session.pop('booking_data', None)

    return redirect(url_for('routes.booking_confirmed', reservation_id=reservation.id))


def _create_reservation_from_stripe(cs):
    data = cs.to_dict() if hasattr(cs, 'to_dict') else cs
    pi_id = data.get('payment_intent') or f'stripe_session_{data.get("id")}'

    existing = Reservation.query.filter_by(stripe_payment_intent_id=pi_id).first()
    if existing:
        return existing

    meta = data.get('metadata') or {}
    guest_name = meta.get('guest_name', 'Guest')
    guest_email = data.get('customer_email') or meta.get('guest_email') or 'info@myapartment.com'

    try:
        check_in = date.fromisoformat(meta.get('check_in'))
        check_out = date.fromisoformat(meta.get('check_out'))
    except (TypeError, ValueError):
        pending = session.get('pending_reservation') or {}
        check_in = date.fromisoformat(pending.get('check_in', date.today().isoformat()))
        check_out = date.fromisoformat(pending.get('check_out', (date.today() + timedelta(days=1)).isoformat()))

    try:
        total_price = float(meta.get('total_price'))
    except (TypeError, ValueError):
        apartment = get_apartment()
        num_guests = int(meta.get('num_guests', 2))
        total_price = (
            calculate_dynamic_total(check_in, check_out, num_guests=num_guests, base_rate=apartment.price_per_night)
            if apartment
            else 0
        )

    is_deposit = meta.get('is_deposit', 'false').lower() == 'true'

    try:
        amount_paid = float(meta.get('amount_paid', total_price))
    except (TypeError, ValueError):
        amount_paid = total_price

    num_guests = int(meta.get('num_guests', 1))
    num_adults = int(meta.get('num_adults', num_guests))
    num_children = int(meta.get('num_children', 0))

    try:
        tourist_tax_amount = calculate_city_tax(check_in, check_out, num_adults, get_apartment())
    except Exception:
        tourist_tax_amount = 0.0

    reservation = Reservation(
        guest_name=guest_name,
        guest_email=guest_email,
        check_in=check_in,
        check_out=check_out,
        num_guests=num_guests,
        num_adults=num_adults,
        num_children=num_children,
        status='confirmed',
        source='direct',
        cancel_token=secrets.token_urlsafe(32),
        total_price=total_price,
        amount_paid=amount_paid,
        coupon_code=meta.get('coupon_code') if meta.get('coupon_code') else None,
        payment_status='deposit_paid' if is_deposit else ('paid' if data.get('payment_status') == 'paid' else 'unpaid'),
        payment_method='stripe',
        stripe_payment_intent_id=pi_id,
        tourist_tax_amount=tourist_tax_amount,
    )
    if not reservation.access_token:
        reservation.generate_access_token()

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
    endpoint_secret = current_app.config.get('STRIPE_WEBHOOK_SECRET')

    stripe.api_key = current_app.config.get('STRIPE_SECRET_KEY')

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, endpoint_secret)
    except (ValueError, stripe.error.SignatureVerificationError):
        return 'Invalid signature', 400

    if event['type'] == 'checkout.session.completed':
        session_obj = event['data']['object']
        meta = session_obj.get('metadata') or {}
        if meta.get('type') == 'tourist_tax':
            res_id = int(meta.get('reservation_id', 0))
            res = db.session.get(Reservation, res_id)
            if res:
                res.tourist_tax_paid = True
                db.session.commit()
                current_app.logger.info('City tax marked paid via webhook for reservation #%s', res_id)
        elif meta.get('type') == 'balance_payment':
            res_id = int(meta.get('reservation_id', 0))
            res = db.session.get(Reservation, res_id)
            if res and res.payment_status == 'deposit_paid':
                res.payment_status = 'paid'
                remaining = round((res.total_price - (res.amount_paid or 0.0)), 2)
                res.amount_paid = (res.amount_paid or 0.0) + remaining
                res.balance_payment_intent_id = session_obj.get('payment_intent')
                db.session.commit()
                send_payment_verified_email(res)
        else:
            reservation = _create_reservation_from_stripe(session_obj)
            send_payment_verified_email(reservation)

    return '', 200


@bp.route('/booking/confirmed/<int:reservation_id>')
def booking_confirmed(reservation_id):
    reservation = Reservation.query.get_or_404(reservation_id)
    return render_template('booking_confirmed.html', reservation=reservation)


@bp.route('/cancel/<token>')
def cancel_reservation(token):
    reservation = Reservation.query.filter_by(cancel_token=token).first_or_404()
    today = date.today()

    if reservation.status == 'cancelled':
        return render_template(
            'cancellation_result.html', success=False, message='This reservation has already been cancelled.'
        )

    if reservation.status != 'confirmed':
        return render_template(
            'cancellation_result.html', success=False, message='This reservation cannot be cancelled.'
        )

    if today >= reservation.check_in:
        return render_template(
            'cancellation_result.html', success=False, message='Cancellation is no longer possible after check-in.'
        )

    refund_percentage = calculate_refund_percentage(reservation.check_in)
    amount_eligible = reservation.amount_paid or reservation.total_price
    refund_amount = round(amount_eligible * refund_percentage, 2)

    refund_failed_warning = False

    if reservation.stripe_payment_intent_id and refund_amount > 0:
        try:
            stripe.api_key = current_app.config['STRIPE_SECRET_KEY']
            stripe.Refund.create(
                payment_intent=reservation.stripe_payment_intent_id,
                amount=int(refund_amount * 100),
                reason='requested_by_customer',
            )
        except stripe.error.StripeError as e:
            current_app.logger.error(f'Stripe refund transaction failed: {str(e)}')
            refund_failed_warning = True

    from app.services.smart_lock import revoke_reservation_keypad

    revoke_reservation_keypad(reservation)
    reservation.status = 'cancelled'
    db.session.commit()

    send_cancellation_emails(reservation, refund_failed_warning, refund_percentage, refund_amount)

    if refund_percentage == 1.0:
        refund_text = 'A full refund (100%) has been issued back to your payment card.'
    elif refund_percentage == 0.5:
        refund_text = 'A partial refund (50%) has been issued back to your payment card.'
    else:
        refund_text = 'No refund is available per our cancellation policy (cancelled within 7 days of check-in).'

    if refund_failed_warning:
        refund_text += ' However, there was an issue processing your automatic refund. We will review it manually.'

    final_ui_message = 'Your reservation has been cancelled successfully. ' + refund_text

    return render_template('cancellation_result.html', success=True, message=final_ui_message)


@bp.route('/review-and-pay')
def review_and_pay():
    booking_data = session.get('booking_data')
    if not booking_data:
        return redirect(url_for('routes.home'))
    total_to_charge = booking_data['total_price']
    return render_template('review_and_pay.html', booking=booking_data, total=total_to_charge)

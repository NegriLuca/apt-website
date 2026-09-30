import json
import secrets
from datetime import date, timedelta

from flask import Response, abort, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app import db
from app.models import Apartment, ComplianceConfig, QuesturaLog, Reservation, Ross1000Log
from app.routes import bp
from app.routes.helpers import get_apartment

# ── Compliance Dashboard ─────────────────────────────────────────────────────


@bp.route('/admin/compliance')
@login_required
def compliance_dashboard() -> Response | str:
    if not current_user.is_admin:
        abort(403)

    today = date.today()

    questura_pending = Reservation.query.filter(
        Reservation.questura_status.in_([None, 'pending']),
        Reservation.status == 'confirmed',
        Reservation.check_in <= today,
    ).count()

    questura_rejected = Reservation.query.filter_by(questura_status='rejected').count()
    questura_accepted = Reservation.query.filter_by(questura_status='accepted').count()

    upcoming = Reservation.query.filter(
        Reservation.status == 'confirmed',
        Reservation.check_in >= today,
        Reservation.check_in <= today + timedelta(days=7),
    ).all()

    needing_data = [r for r in upcoming if not r.questura_ready()]

    apt = Apartment.query.first()
    from app.services.tourist_tax import get_tax_service

    tax_service = get_tax_service(apt) if apt else None
    current_month_tax = 0
    if tax_service:
        report = tax_service.generate_detailed_report(today.year, today.month)
        current_month_tax = report['total_tax']

    config_keys = [
        'questura_wsdl_url',
        'questura_username',
        'questura_password',
        'questura_ws_key',
        'questura_cert_path',
        'questura_cert_password',
        'questura_protocol_number',
        'ross1000_username',
        'ross1000_password',
        'ross1000_structure_code',
    ]
    config_status = {k: bool(ComplianceConfig.get(k)) for k in config_keys}

    ross1000_pending = Reservation.query.filter(
        Reservation.ross1000_status.in_([None, 'pending']),
        Reservation.status == 'confirmed',
        Reservation.check_in <= today,
    ).count()
    ross1000_rejected = Reservation.query.filter_by(ross1000_status='rejected').count()
    ross1000_accepted = Reservation.query.filter_by(ross1000_status='accepted').count()

    return render_template(
        'admin_compliance.html',
        questura_pending=questura_pending,
        questura_rejected=questura_rejected,
        questura_accepted=questura_accepted,
        ross1000_pending=ross1000_pending,
        ross1000_rejected=ross1000_rejected,
        ross1000_accepted=ross1000_accepted,
        needing_data=needing_data,
        current_month_tax=current_month_tax,
        config_status=config_status,
        today=today,
    )


# ── Questura ─────────────────────────────────────────────────────────────────


@bp.route('/admin/compliance/questura')
@login_required
def questura_list() -> Response | str:
    if not current_user.is_admin:
        abort(403)

    status_filter = request.args.get('status', 'all')
    page = request.args.get('page', 1, type=int)
    q = request.args.get('q', '').strip()
    form_filter = request.args.get('form', 'all')  # all | compiled | not_compiled | ready | not_ready

    query = Reservation.query.filter(Reservation.status != 'cancelled').order_by(Reservation.check_in.desc())
    if status_filter == 'pending':
        query = query.filter(Reservation.questura_status.in_([None, 'pending']))
    elif status_filter == 'None':
        query = query.filter(Reservation.questura_status.is_(None))
    elif status_filter != 'all':
        query = query.filter(Reservation.questura_status == status_filter)

    if q:
        like = f'%{q}%'
        query = query.filter(
            db.or_(
                Reservation.guest_name.ilike(like),
                Reservation.guest_email.ilike(like),
                Reservation.guest_surname.ilike(like),
                Reservation.guest_first_name.ilike(like),
                Reservation.guest_document_number.ilike(like),
                Reservation.external_uid.ilike(like),
            )
        )

    if form_filter == 'compiled':
        query = query.filter(Reservation.checkin_completed_at.isnot(None))
    elif form_filter == 'not_compiled':
        query = query.filter(Reservation.checkin_completed_at.is_(None))
    elif form_filter == 'ready':
        query = query.filter(
            Reservation.guest_surname.isnot(None),
            Reservation.guest_first_name.isnot(None),
            Reservation.guest_birth_date.isnot(None),
            Reservation.guest_document_number.isnot(None),
        )
    elif form_filter == 'not_ready':
        query = query.filter(
            db.or_(
                Reservation.guest_surname.is_(None),
                Reservation.guest_first_name.is_(None),
                Reservation.guest_birth_date.is_(None),
                Reservation.guest_document_number.is_(None),
            )
        )

    reservations = query.paginate(page=page, per_page=25, error_out=False)
    return render_template('admin_questura.html', reservations=reservations, status_filter=status_filter, q=q, form_filter=form_filter)


@bp.route('/admin/compliance/questura/<int:res_id>/guest-data')
@login_required
def questura_guest_data(res_id: int) -> Response | str:
    if not current_user.is_admin:
        abort(403)
    reservation = Reservation.query.get_or_404(res_id)
    return render_template('admin_guest_data.html', reservation=reservation)


@bp.route('/admin/compliance/questura/submit', methods=['POST'])
@login_required
def questura_submit() -> Response | str:
    if not current_user.is_admin:
        abort(403)

    res_id = request.form.get('reservation_id', type=int)
    res = Reservation.query.get_or_404(res_id)

    from app.services.questura import get_questura_service

    service = get_questura_service()
    result = service.submit_reservation(res)
    ok = bool(result.get('success'))
    msg = result.get('message') or result.get('error') or ('Submitted.' if ok else 'Failed.')
    flash(msg, 'success' if ok else 'danger')
    return redirect(url_for('routes.questura_list'))


@bp.route('/admin/compliance/questura/run-daily', methods=['POST'])
@login_required
def questura_run_daily() -> Response | str:
    if not current_user.is_admin:
        abort(403)

    from app.tasks.compliance import run_daily_questura

    run_daily_questura()
    flash('Daily Questura run complete.', 'success')
    return redirect(url_for('routes.questura_list'))


@bp.route('/admin/compliance/questura/logs')
@login_required
def questura_logs() -> Response | str:
    if not current_user.is_admin:
        abort(403)

    page = request.args.get('page', 1, type=int)
    logs = QuesturaLog.query.order_by(QuesturaLog.created_at.desc()).paginate(page=page, per_page=50, error_out=False)
    return render_template('admin_questura_logs.html', logs=logs)


# ── Tourist Tax ──────────────────────────────────────────────────────────────


@bp.route('/admin/compliance/tourist-tax')
@login_required
def tourist_tax() -> Response | str:
    if not current_user.is_admin:
        abort(403)

    apt = get_apartment()
    year = request.args.get('year', date.today().year, type=int)
    month = request.args.get('month', date.today().month, type=int)
    paid_only = request.args.get('paid_only') == '1'

    from app.services.tourist_tax import get_tax_service

    service = get_tax_service(apt)
    report = service.generate_detailed_report(year, month)
    if paid_only:
        report['reservations'] = [r for r in report['reservations'] if r.get('tax_paid')]
        report['total_tax'] = sum(r.get('tax', 0) for r in report['reservations'])
        report['total_reservations'] = len(report['reservations'])
        report['total_nights'] = sum(r.get('nights', 0) for r in report['reservations'])
        report['total_guests'] = sum(r.get('guests', 0) for r in report['reservations'])

    return render_template('admin_tourist_tax.html', apt=apt, report=report, year=year, month=month, paid_only=paid_only)


@bp.route('/admin/compliance/tourist-tax/paid')
@login_required
def tourist_tax_paid() -> Response | str:
    """Dedicated paid-only report: only reservations with tourist_tax_paid=True."""
    if not current_user.is_admin:
        abort(403)
    apt = get_apartment()
    year = request.args.get('year', date.today().year, type=int)
    month = request.args.get('month', date.today().month, type=int)
    from app.services.tourist_tax import get_tax_service
    service = get_tax_service(apt)
    report = service.generate_detailed_report(year, month)
    # Keep only paid
    paid_rows = [r for r in report['reservations'] if r.get('tax_paid')]
    report['reservations'] = paid_rows
    report['total_tax'] = sum(r.get('tax', 0) for r in paid_rows)
    report['total_reservations'] = len(paid_rows)
    report['total_nights'] = sum(r.get('nights', 0) for r in paid_rows)
    report['total_guests'] = sum(r.get('guests', 0) for r in paid_rows)
    return render_template('admin_tourist_tax.html', apt=apt, report=report, year=year, month=month, paid_only=True)


@bp.route('/admin/compliance/tourist-tax/save-config', methods=['POST'])
@login_required
def tourist_tax_save_config() -> Response | str:
    if not current_user.is_admin:
        abort(403)

    apt = get_apartment()
    apt.cin_code = request.form.get('cin_code', '').strip() or None
    apt.cir_code = request.form.get('cir_code', '').strip() or None
    apt.tourist_tax_category = request.form.get('tourist_tax_category', 'CAV')
    apt.tourist_tax_rate = request.form.get('tourist_tax_rate', type=float, default=3.50)
    apt.max_guests = request.form.get('max_guests', type=int, default=4)
    db.session.commit()
    flash('Property configuration saved.', 'success')
    return redirect(url_for('routes.tourist_tax'))


@bp.route('/admin/compliance/tourist-tax/export')
@login_required
def tourist_tax_export() -> Response | str:
    if not current_user.is_admin:
        abort(403)

    year = request.args.get('year', type=int) or date.today().year
    month = request.args.get('month', type=int) or date.today().month
    paid_only = request.args.get('paid_only') == '1'

    from app.services.tourist_tax import get_tax_service

    service = get_tax_service(get_apartment())
    csv_data = service.export_monthly_csv(year, month)
    # For paid-only export, filter CSV rows to paid only
    if paid_only:
        import csv, io
        from app.models import Reservation
        apt = get_apartment()
        from app.services.tourist_tax import get_tax_service as _gts
        tax_service = _gts(apt)
        start = date(year, month, 1)
        if month == 12:
            end = date(year + 1, 1, 1)
        else:
            end = date(year, month + 1, 1)
        paid_res = Reservation.query.filter(
            Reservation.status == 'confirmed',
            Reservation.tourist_tax_paid.is_(True),
            Reservation.tourist_tax_excluded != True,
            Reservation.check_in >= start,
            Reservation.check_in < end,
        ).order_by(Reservation.check_in).all()
        output = io.StringIO()
        writer = csv.writer(output, delimiter=';', quoting=csv.QUOTE_MINIMAL)
        writer.writerow(tax_service._get_csv_headers())
        total = 0.0
        for r in paid_res:
            row = tax_service.calculate_for_reservation(r)
            writer.writerow(tax_service._row_to_csv(row))
            total += row.total_tax
        writer.writerow(['', '', '', '', '', '', '', f'{total:.2f}', '', '', ''])
        csv_data = output.getvalue()

    suffix = '_paid' if paid_only else ''
    return (
        csv_data,
        200,
        {
            'Content-Type': 'text/csv; charset=utf-8',
            'Content-Disposition': f'attachment; filename="tourist_tax{suffix}_{year:04d}-{month:02d}.csv"',
        },
    )


@bp.route('/admin/compliance/tourist-tax/generate-report', methods=['POST'])
@login_required
def tourist_tax_generate() -> Response | str:
    if not current_user.is_admin:
        abort(403)

    year = request.form.get('year', type=int) or date.today().year
    month = request.form.get('month', type=int) or date.today().month

    from app.services.tourist_tax import get_tax_service

    service = get_tax_service(get_apartment())
    service.export_monthly_csv(year, month)

    flash(f'Tourist tax report generated for {month:02d}/{year}.', 'success')
    return redirect(url_for('routes.tourist_tax', year=year, month=month))


@bp.route('/admin/compliance/tourist-tax/update/<int:reservation_id>', methods=['POST'])
@login_required
def tourist_tax_update_reservation(reservation_id: int) -> Response | str:
    if not current_user.is_admin:
        abort(403)

    res = Reservation.query.get_or_404(reservation_id)

    nights = request.form.get('nights', type=int)
    guests = request.form.get('num_guests', type=int)
    tax_override = request.form.get('tourist_tax_amount', type=float)
    tax_paid = request.form.get('tourist_tax_paid') == '1'

    if nights is not None and nights > 0:
        res.check_out = res.check_in + timedelta(days=nights)

    if guests is not None and guests > 0:
        res.num_guests = guests
        res.num_adults = guests
        res.num_children = 0

    if tax_override is not None:
        res.tourist_tax_amount = max(0.0, tax_override)

    res.tourist_tax_paid = tax_paid
    db.session.commit()

    flash(f'Reservation #{reservation_id} updated.', 'success')
    return redirect(url_for('routes.tourist_tax', year=request.form.get('year'), month=request.form.get('month')))


@bp.route('/admin/compliance/tourist-tax/toggle-exclude/<int:reservation_id>', methods=['POST'])
@login_required
def tourist_tax_toggle_exclude(reservation_id: int) -> Response | str:
    if not current_user.is_admin:
        abort(403)

    res = Reservation.query.get_or_404(reservation_id)
    res.tourist_tax_excluded = not res.tourist_tax_excluded
    db.session.commit()

    status = 'excluded from' if res.tourist_tax_excluded else 'included in'
    flash(f'Reservation #{reservation_id} {status} tourist tax.', 'success')
    return redirect(url_for('routes.tourist_tax', year=request.form.get('year'), month=request.form.get('month')))


# ── Compliance Config ────────────────────────────────────────────────────────

CONFIG_FIELDS = {
    'questura_wsdl_url': 'Questura / AlloggiatiWeb endpoint URL',
    'questura_protocol_number': 'Questura protocol number (Protocollo)',
    'questura_username': 'AlloggiatiWeb username (Utente)',
    'questura_password': 'AlloggiatiWeb password',
    'questura_ws_key': 'AlloggiatiWeb WsKey (generated on the portal)',
    'questura_cert_path': 'Legacy client certificate path',
    'questura_cert_password': 'Legacy client certificate password',
    'tourist_tax_category': 'Tourist tax property category',
    'tourist_tax_rate': 'Tourist tax custom rate (EUR)',
    'roma_tax_office_email': 'Rome tax office email',
    'ross1000_username': 'ROSS1000 username',
    'ross1000_password': 'ROSS1000 password',
    'ross1000_structure_code': 'ROSS1000 structure code',
}


@bp.route('/admin/compliance/config', methods=['GET', 'POST'])
@login_required
def compliance_config() -> Response | str:
    if not current_user.is_admin:
        abort(403)

    if request.method == 'POST':
        saved = 0
        for key, description in CONFIG_FIELDS.items():
            if key in request.form:
                value = request.form.get(key, '').strip()
                ComplianceConfig.set(key, value or None, description)
                saved += 1
        flash(f'Saved {saved} configuration value(s).', 'success' if saved else 'warning')
        return redirect(url_for('routes.compliance_config'))

    configs = ComplianceConfig.query.order_by(ComplianceConfig.key).all()
    masked = [
        {
            'key': c.key,
            'value': '***' if c.value_encrypted else '',
            'description': c.description,
            'updated_at': c.updated_at,
        }
        for c in configs
    ]

    return render_template('admin_compliance_config.html', configs=masked, ComplianceConfig=ComplianceConfig)


@bp.route('/admin/compliance/config/set', methods=['POST'])
@login_required
def config_set() -> Response | str:
    if not current_user.is_admin:
        abort(403)

    key = request.form.get('key')
    value = request.form.get('value')
    description = request.form.get('description')

    if key and value:
        ComplianceConfig.set(key, value, description)
        flash(f'Configuration "{key}" updated', 'success')
    else:
        flash('Key and value are required.', 'danger')

    return redirect(url_for('routes.compliance_config'))


# ── ROSS1000 (Regione Lazio) ─────────────────────────────────────────────────


@bp.route('/admin/compliance/ross1000')
@login_required
def ross1000_list():
    if not current_user.is_admin:
        abort(403)

    status_filter = request.args.get('status', 'all')
    page = request.args.get('page', 1, type=int)

    query = Reservation.query.filter(Reservation.status != 'cancelled').order_by(Reservation.check_in.desc())
    if status_filter != 'all':
        query = query.filter(Reservation.ross1000_status == status_filter)

    reservations = query.paginate(page=page, per_page=25, error_out=False)
    return render_template('admin_ross1000.html', reservations=reservations, status_filter=status_filter)


@bp.route('/admin/compliance/ross1000/submit', methods=['POST'])
@login_required
def ross1000_submit():
    if not current_user.is_admin:
        abort(403)

    data = request.get_json(silent=True) or {}
    res_ids = data.get('reservation_ids', [])
    res_id = request.form.get('reservation_id', type=int)

    if res_id:
        res_ids = [res_id]

    if not res_ids:
        flash('No reservations selected.', 'danger')
        return redirect(url_for('routes.ross1000_list'))

    from app.services.ross1000 import get_ross1000_service

    service = get_ross1000_service()
    results = []

    for rid in res_ids:
        res = Reservation.query.get(rid)
        if not res:
            results.append({'reservation_id': rid, 'success': False, 'error': 'Not found'})
            continue
        result = service.submit_reservation(res)
        results.append({'reservation_id': rid, **result})

    if request.is_json:
        return {'success': all(r.get('success') for r in results), 'results': results}

    success_count = sum(1 for r in results if r.get('success'))
    flash(f'ROSS1000: {success_count}/{len(results)} submitted successfully.', 'success' if success_count else 'danger')
    return redirect(url_for('routes.ross1000_list'))


@bp.route('/admin/compliance/ross1000/test', methods=['POST'])
@login_required
def ross1000_test():
    if not current_user.is_admin:
        abort(403)

    try:
        from app.services.ross1000 import get_ross1000_service

        service = get_ross1000_service()
        result = service.test_connection()

        if result.get('success'):
            flash('ROSS1000 connection successful!', 'success')
        else:
            flash(f'ROSS1000 connection failed: {result.get("error", "Unknown error")}', 'danger')
    except Exception as e:
        current_app.logger.exception('ROSS1000 test failed')
        flash(f'ROSS1000 test error: {e}', 'danger')

    return redirect(url_for('routes.compliance_dashboard'))


@bp.route('/admin/compliance/ross1000/logs')
@login_required
def ross1000_logs():
    if not current_user.is_admin:
        abort(403)

    page = request.args.get('page', 1, type=int)
    logs = Ross1000Log.query.order_by(Ross1000Log.created_at.desc()).paginate(page=page, per_page=50, error_out=False)
    return render_template('admin_ross1000_logs.html', logs=logs)


# ── Check-in Links ───────────────────────────────────────────────────────────


@bp.route('/admin/compliance/send-checkin-link', methods=['POST'])
@login_required
def send_checkin_link() -> Response | str:
    if not current_user.is_admin:
        abort(403)

    res_id = request.form.get('reservation_id', type=int)
    res = Reservation.query.get_or_404(res_id)

    if not res.checkin_token:
        res.checkin_token = secrets.token_urlsafe(32)
        db.session.commit()

    checkin_url = url_for('routes.guest_self_checkin', token=res.checkin_token, _external=True)

    if current_app.config.get('MAIL_SUPPRESS_SEND') or current_app.config.get('TESTING'):
        current_app.logger.info('Email suppressed (TESTING): check-in link for #%s', res.id)
        flash('Email suppressed (test mode) — not sent.', 'info')
        return redirect(url_for('routes.compliance_dashboard'))

    try:
        from app import mail
        from flask_mail import Message
        sender_addr = current_app.config.get('MAIL_DEFAULT_SENDER') or current_app.config.get('MAIL_USERNAME') or 'lotto235roma@gmail.com'
        msg = Message(
            subject='Check-in Link — Lotto 235 Garbatella',
            recipients=[res.guest_email],
            html=render_template('email_checkin_link.html', reservation=res, checkin_url=checkin_url),
            sender=('Lotto235 Garbatella', sender_addr),
        )
        mail.send(msg)
        flash('Check-in link sent via Gmail SMTP.', 'success')
    except Exception as e:
        current_app.logger.error('Check-in link SMTP failed: %s', e)
        flash(f'Error: {e}', 'danger')

    return redirect(url_for('routes.compliance_dashboard'))


@bp.route('/admin/compliance/regenerate-checkin-token', methods=['POST'])
@login_required
def regenerate_checkin_token() -> Response | str:
    if not current_user.is_admin:
        abort(403)

    res_id = request.form.get('reservation_id', type=int)
    res = Reservation.query.get_or_404(res_id)
    res.checkin_token = secrets.token_urlsafe(32)
    res.checkin_token_used = False
    db.session.commit()
    flash('Check-in token regenerated.', 'success')
    return redirect(url_for('routes.compliance_dashboard'))

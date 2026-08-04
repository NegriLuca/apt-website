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

    query = Reservation.query.order_by(Reservation.check_in.desc())
    if status_filter != 'all':
        query = query.filter(Reservation.questura_status == status_filter)

    reservations = query.paginate(page=page, per_page=25, error_out=False)
    return render_template('admin_questura.html', reservations=reservations, status_filter=status_filter)


@bp.route('/admin/compliance/questura/submit', methods=['POST'])
@login_required
def questura_submit() -> Response | str:
    if not current_user.is_admin:
        abort(403)

    res_id = request.form.get('reservation_id', type=int)
    res = Reservation.query.get_or_404(res_id)

    from app.services.questura import get_questura_service

    service = get_questura_service()
    ok, msg = service.submit_reservation(res)
    flash(msg or ('Submitted.' if ok else 'Failed.'), 'success' if ok else 'danger')
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

    from app.services.tourist_tax import get_tax_service

    service = get_tax_service(apt)
    report = service.generate_detailed_report(year, month)

    return render_template('admin_tourist_tax.html', apt=apt, report=report, year=year, month=month)


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

    from app.services.tourist_tax import get_tax_service

    service = get_tax_service(get_apartment())
    csv_data = service.export_monthly_csv(year, month)

    return (
        csv_data,
        200,
        {
            'Content-Type': 'text/csv; charset=utf-8',
            'Content-Disposition': f'attachment; filename="tourist_tax_{year:04d}-{month:02d}.csv"',
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


@bp.route('/admin/compliance/config')
@login_required
def compliance_config() -> Response | str:
    if not current_user.is_admin:
        abort(403)

    if request.method == 'POST':
        key = request.form.get('key')
        value = request.form.get('value')
        description = request.form.get('description')

        if key and value:
            ComplianceConfig.set(key, value, description)
            flash(f'Configuration "{key}" updated', 'success')
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

    query = Reservation.query.order_by(Reservation.check_in.desc())
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

    brevo_api_key = current_app.config.get('MAIL_PASSWORD')
    payload = {
        'sender': {'name': 'Lotto235 Garbatella', 'email': 'lotto235roma@gmail.com'},
        'to': [{'email': res.guest_email}],
        'subject': 'Check-in Link \u2014 Lotto 235 Garbatella',
        'htmlContent': render_template('email_checkin_link.html', reservation=res, checkin_url=checkin_url),
    }

    import json

    import requests

    try:
        r = requests.post(
            'https://api.brevo.com/v3/smtp/email',
            headers={'accept': 'application/json', 'content-type': 'application/json', 'api-key': brevo_api_key},
            data=json.dumps(payload),
        )
        if r.status_code in [200, 201, 202]:
            flash('Check-in link sent.', 'success')
        else:
            flash(f'Failed ({r.status_code}).', 'danger')
    except Exception as e:
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

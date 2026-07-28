from flask import render_template, redirect, url_for, flash, request, current_app, abort, jsonify
from flask_babel import gettext as _
from flask_login import login_required, current_user
from app.routes import bp
from app.routes.helpers import get_apartment
from app import db
from app.models import Apartment, Reservation, ComplianceConfig, QuesturaLog
from datetime import date, datetime, timedelta
import secrets


# ── Compliance Dashboard ─────────────────────────────────────────────────────


@bp.route('/admin/compliance')
@login_required
def compliance_dashboard():
    if not current_user.is_admin:
        abort(403)

    today = date.today()

    questura_pending = Reservation.query.filter(
        Reservation.questura_status.in_([None, 'pending']),
        Reservation.status == 'confirmed',
        Reservation.check_in <= today
    ).count()

    questura_rejected = Reservation.query.filter_by(questura_status='rejected').count()
    questura_accepted = Reservation.query.filter_by(questura_status='accepted').count()

    upcoming = Reservation.query.filter(
        Reservation.status == 'confirmed',
        Reservation.check_in >= today,
        Reservation.check_in <= today + timedelta(days=7)
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
        'questura_wsdl_url', 'questura_username', 'questura_password',
        'questura_cert_path', 'questura_cert_password', 'questura_protocol_number'
    ]
    config_status = {k: bool(ComplianceConfig.get(k)) for k in config_keys}

    return render_template('admin_compliance.html',
        questura_pending=questura_pending,
        questura_rejected=questura_rejected,
        questura_accepted=questura_accepted,
        needing_data=needing_data,
        current_month_tax=current_month_tax,
        config_status=config_status,
        today=today
    )


# ── Questura ─────────────────────────────────────────────────────────────────


@bp.route('/admin/compliance/questura')
@login_required
def questura_list():
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
def questura_submit():
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
def questura_run_daily():
    if not current_user.is_admin:
        abort(403)

    from app.tasks.compliance import run_daily_questura
    results = run_daily_questura()
    flash(f'Daily Questura run complete.', 'success')
    return redirect(url_for('routes.questura_list'))


@bp.route('/admin/compliance/questura/logs')
@login_required
def questura_logs():
    if not current_user.is_admin:
        abort(403)

    page = request.args.get('page', 1, type=int)
    logs = QuesturaLog.query.order_by(QuesturaLog.created_at.desc()).paginate(
        page=page, per_page=50, error_out=False)
    return render_template('admin_questura_logs.html', logs=logs)


# ── Tourist Tax ──────────────────────────────────────────────────────────────


@bp.route('/admin/compliance/tourist-tax')
@login_required
def tourist_tax():
    if not current_user.is_admin:
        abort(403)

    apt = get_apartment()
    year = request.args.get('year', date.today().year, type=int)
    month = request.args.get('month', date.today().month, type=int)

    from app.services.tourist_tax import get_tax_service
    service = get_tax_service(apt)
    report = service.generate_detailed_report(year, month)

    return render_template('admin_tourist_tax.html',
                           apt=apt, report=report, year=year, month=month)


@bp.route('/admin/compliance/tourist-tax/export')
@login_required
def tourist_tax_export():
    if not current_user.is_admin:
        abort(403)

    year = request.args.get('year', type=int) or date.today().year
    month = request.args.get('month', type=int) or date.today().month

    from app.services.tourist_tax import get_tax_service
    service = get_tax_service(get_apartment())
    csv_data = service.export_monthly_csv(year, month)

    return csv_data, 200, {
        'Content-Type': 'text/csv; charset=utf-8',
        'Content-Disposition': f'attachment; filename="tourist_tax_{year:04d}-{month:02d}.csv"'
    }


@bp.route('/admin/compliance/tourist-tax/generate-report', methods=['POST'])
@login_required
def tourist_tax_generate():
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
def tourist_tax_update_reservation(reservation_id):
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

    if tax_override is not None:
        res.tourist_tax_amount = max(0.0, tax_override)

    res.tourist_tax_paid = tax_paid
    db.session.commit()

    flash(f'Reservation #{reservation_id} updated.', 'success')
    return redirect(url_for('routes.tourist_tax', year=request.form.get('year'), month=request.form.get('month')))


@bp.route('/admin/compliance/tourist-tax/toggle-exclude/<int:reservation_id>', methods=['POST'])
@login_required
def tourist_tax_toggle_exclude(reservation_id):
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
def compliance_config():
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
            'updated_at': c.updated_at
        }
        for c in configs
    ]

    return render_template('admin_compliance_config.html', configs=masked, ComplianceConfig=ComplianceConfig)


@bp.route('/admin/compliance/config/set', methods=['POST'])
@login_required
def config_set():
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


# ── Check-in Links ───────────────────────────────────────────────────────────


@bp.route('/admin/compliance/send-checkin-link', methods=['POST'])
@login_required
def send_checkin_link():
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
        "sender": {"name": "Lotto235 Garbatella", "email": "lotto235roma@gmail.com"},
        "to": [{"email": res.guest_email}],
        "subject": "Check-in Link — Lotto 235 Garbatella",
        "htmlContent": render_template('email_checkin_link.html', reservation=res, checkin_url=checkin_url)
    }

    import requests, json
    try:
        r = requests.post(
            "https://api.brevo.com/v3/smtp/email",
            headers={"accept": "application/json", "content-type": "application/json", "api-key": brevo_api_key},
            data=json.dumps(payload)
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
def regenerate_checkin_token():
    if not current_user.is_admin:
        abort(403)

    res_id = request.form.get('reservation_id', type=int)
    res = Reservation.query.get_or_404(res_id)
    res.checkin_token = secrets.token_urlsafe(32)
    res.checkin_token_used = False
    db.session.commit()
    flash('Check-in token regenerated.', 'success')
    return redirect(url_for('routes.compliance_dashboard'))

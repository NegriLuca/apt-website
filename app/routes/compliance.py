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

    apartment = get_apartment()
    today = date.today()
    month_start = today.replace(day=1)

    confirmed = Reservation.query.filter(
        Reservation.status == 'confirmed',
        Reservation.check_in >= month_start,
        Reservation.check_in < month_start.replace(month=month_start.month + 1) if month_start.month < 12 else month_start.replace(year=month_start.year + 1, month=1)
    ).count()

    pending_questura = Reservation.query.filter(
        Reservation.status == 'confirmed',
        Reservation.questura_status.in_(['pending', None]),
        Reservation.check_in <= today
    ).count()

    tax_report = get_tax_report_summary()

    return render_template('admin_compliance.html',
                           apartment=apartment,
                           stats={
                               'confirmed_this_month': confirmed,
                               'pending_questura': pending_questura,
                               'tax_month': tax_report['month'],
                               'tax_total': tax_report['total'],
                           })


def get_tax_report_summary():
    today = date.today()
    month = today.month
    year = today.year
    from app.services.tourist_tax import get_tax_service
    apartment = get_apartment()
    service = get_tax_service(apartment)
    report = service.generate_detailed_report(year, month)
    return {'month': f'{month:02d}/{year}', 'total': report['total_tax']}


# ── Questura ─────────────────────────────────────────────────────────────────


@bp.route('/admin/compliance/questura')
@login_required
def questura_list():
    if not current_user.is_admin:
        abort(403)

    today = date.today()
    active_reservations = Reservation.query.filter(
        Reservation.status == 'confirmed',
        Reservation.check_in <= today,
        Reservation.check_out >= today
    ).order_by(Reservation.check_in).all()

    return render_template('admin_questura.html', reservations=active_reservations)


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

    logs = QuesturaLog.query.order_by(QuesturaLog.created_at.desc()).limit(100).all()
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

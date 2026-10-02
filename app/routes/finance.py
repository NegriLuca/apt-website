from datetime import date, datetime

from flask import abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app import db
from app.models import RunningCost
from app.routes import bp
from app.routes.admin import admin_audit_log
from app.services.finance import available_years, compute_finance, sankey_data

MIN_YEAR = 2000
MAX_YEAR = 2100


def _parse_cost_form():
    """Parse the add-cost form: costs are booked per month, or per whole year (IMU)."""
    category = (request.form.get('category') or 'other').strip().lower()
    if category not in RunningCost.CATEGORIES:
        category = 'other'
    try:
        year = int(request.form.get('year', 0))
    except (TypeError, ValueError):
        return None, 'Invalid year.'
    if not (MIN_YEAR <= year <= MAX_YEAR):
        return None, 'Invalid year.'
    if category == 'imu':
        month = None  # IMU is always a whole-year entry
    else:
        try:
            month = int(request.form.get('month', 0))
        except (TypeError, ValueError):
            return None, 'Pick a month.'
        if not (1 <= month <= 12):
            return None, 'Pick a month.'
    try:
        amount = float(request.form.get('amount', 0))
    except (TypeError, ValueError):
        return None, 'Amount must be a number.'
    if amount <= 0:
        return None, 'Amount must be positive.'
    if amount > 1_000_000:
        return None, 'Amount looks too large.'
    note = (request.form.get('note') or '').strip()[:250]
    return {'year': year, 'month': month, 'category': category, 'amount': round(amount, 2), 'note': note or None}, None


@bp.route('/admin/finance', methods=['GET', 'POST'])
@login_required
def admin_finance():
    if not current_user.is_admin:
        abort(403)

    today = date.today()

    if request.method == 'POST':
        action = request.form.get('action', 'add')
        if action == 'delete':
            cost = RunningCost.query.get_or_404(request.form.get('cost_id', type=int))
            label = cost.period_label
            db.session.delete(cost)
            db.session.commit()
            admin_audit_log('delete_cost', 'RunningCost', cost.id, f'{cost.category} €{cost.amount:.2f} {label}')
            flash('Cost deleted.', 'info')
            return redirect(
                url_for('routes.admin_finance', year=request.args.get('year'), month=request.args.get('month'))
            )
        parsed, error = _parse_cost_form()
        if error:
            flash(error, 'danger')
            return redirect(
                url_for('routes.admin_finance', year=request.args.get('year'), month=request.args.get('month'))
            )
        cost = RunningCost(**parsed)
        db.session.add(cost)
        db.session.commit()
        admin_audit_log('add_cost', 'RunningCost', cost.id, f'{cost.category} €{cost.amount:.2f} {cost.period_label}')
        flash(f'Cost added: {cost.category} €{cost.amount:.2f} ({cost.period_label}).', 'success')
        # Jump to the period the entry was booked to.
        return redirect(url_for('routes.admin_finance', year=cost.year, month=cost.month or today.month))

    years = available_years()
    try:
        year = int(request.args.get('year', today.year))
    except (TypeError, ValueError):
        year = today.year
    month_raw = request.args.get('month', str(today.month))
    try:
        month = int(month_raw)
    except (TypeError, ValueError):
        month = today.month
    if month < 1 or month > 12:
        month = today.month
    if year not in years:
        years = sorted(set(years + [year]), reverse=True)

    monthly = compute_finance(year, month)
    yearly = compute_finance(year, None)
    month_costs = RunningCost.query.filter_by(year=year, month=month).order_by(RunningCost.id.desc()).all()
    year_costs = (
        RunningCost.query.filter(RunningCost.year == year, RunningCost.month.is_(None))
        .order_by(RunningCost.id.desc())
        .all()
    )

    return render_template(
        'admin_finance.html',
        years=years,
        year=year,
        month=month,
        months=[(m, datetime(2000, m, 1).strftime('%B')) for m in range(1, 13)],
        month_name=datetime(year, month, 1).strftime('%B %Y'),
        monthly=monthly,
        yearly=yearly,
        sankey_monthly=sankey_data(monthly),
        sankey_yearly=sankey_data(yearly),
        month_costs=month_costs,
        year_costs=year_costs,
        categories=RunningCost.CATEGORIES,
        today=today,
    )

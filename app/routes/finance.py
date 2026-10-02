from datetime import date, datetime

from flask import abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app import db
from app.models import RunningCost
from app.routes import bp
from app.routes.admin import admin_audit_log
from app.services.finance import available_years, compute_finance, sankey_data


def _parse_cost_form():
    try:
        cost_date = date.fromisoformat(request.form.get('cost_date', ''))
    except ValueError:
        return None, 'Invalid date.'
    category = (request.form.get('category') or 'other').strip().lower()
    if category not in RunningCost.CATEGORIES:
        category = 'other'
    try:
        amount = float(request.form.get('amount', 0))
    except ValueError:
        return None, 'Amount must be a number.'
    if amount <= 0:
        return None, 'Amount must be positive.'
    if amount > 1_000_000:
        return None, 'Amount looks too large.'
    note = (request.form.get('note') or '').strip()[:250]
    return {'cost_date': cost_date, 'category': category, 'amount': round(amount, 2), 'note': note or None}, None


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
            db.session.delete(cost)
            db.session.commit()
            admin_audit_log(
                'delete_cost', 'RunningCost', cost.id, f'{cost.category} €{cost.amount:.2f} {cost.cost_date}'
            )
            flash('Cost deleted.', 'info')
        else:
            parsed, error = _parse_cost_form()
            if error:
                flash(error, 'danger')
            else:
                cost = RunningCost(**parsed)
                db.session.add(cost)
                db.session.commit()
                admin_audit_log(
                    'add_cost', 'RunningCost', cost.id, f'{cost.category} €{cost.amount:.2f} {cost.cost_date}'
                )
                flash(
                    f'Cost added: {cost.category} €{cost.amount:.2f} ({cost.cost_date.strftime("%d/%m/%Y")}).',
                    'success',
                )
        # Keep the current period selection after redirect.
        return redirect(url_for('routes.admin_finance', year=request.args.get('year'), month=request.args.get('month')))

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
    # Costs table shows the selected month's entries (IMU rows included but flagged yearly-only).
    month_start = date(year, month, 1)
    month_end = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    month_costs = (
        RunningCost.query.filter(
            RunningCost.cost_date >= month_start,
            RunningCost.cost_date < month_end,
        )
        .order_by(RunningCost.cost_date.desc(), RunningCost.id.desc())
        .all()
    )

    return render_template(
        'admin_finance.html',
        years=years,
        year=year,
        month=month,
        month_name=datetime(year, month, 1).strftime('%B %Y'),
        monthly=monthly,
        yearly=yearly,
        sankey_monthly=sankey_data(monthly),
        sankey_yearly=sankey_data(yearly),
        month_costs=month_costs,
        categories=RunningCost.CATEGORIES,
        today=today,
    )

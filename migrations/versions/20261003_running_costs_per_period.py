"""running costs per period (year/month) instead of exact date

Revision ID: 20261003
Revises: 20261002
Create Date: 2026-10-02

Costs are booked per month (month 1-12) or per whole year (month NULL,
used by IMU). Existing rows are backfilled from cost_date.

NOTE: the backfill is plain Python (no strftime/printf/EXTRACT) so it
runs on both SQLite (local/tests) and PostgreSQL (Railway).
"""

from datetime import date as _date

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '20261003'
down_revision = '20261002'
branch_labels = None
depends_on = None


def _as_date(value):
    if isinstance(value, _date):
        return value
    return _date.fromisoformat(str(value))


def upgrade():
    op.add_column('running_costs', sa.Column('month', sa.Integer(), nullable=True))
    op.add_column('running_costs', sa.Column('year', sa.Integer(), nullable=True))
    conn = op.get_bind()
    rows = conn.execute(sa.text('SELECT id, cost_date, category FROM running_costs')).mappings().all()
    for row in rows:
        d = _as_date(row['cost_date'])
        month = None if (row['category'] or '') == 'imu' else d.month
        conn.execute(
            sa.text('UPDATE running_costs SET year = :y, month = :m WHERE id = :i'),
            {'y': d.year, 'm': month, 'i': row['id']},
        )
    with op.batch_alter_table('running_costs', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_running_costs_cost_date'))
        batch_op.drop_column('cost_date')
        batch_op.alter_column('year', existing_type=sa.Integer(), nullable=False)
        batch_op.create_index(batch_op.f('ix_running_costs_year'), ['year'], unique=False)
        batch_op.create_index(batch_op.f('ix_running_costs_month'), ['month'], unique=False)


def downgrade():
    op.add_column('running_costs', sa.Column('cost_date', sa.Date(), nullable=True))
    conn = op.get_bind()
    rows = conn.execute(sa.text('SELECT id, year, month FROM running_costs')).mappings().all()
    for row in rows:
        conn.execute(
            sa.text('UPDATE running_costs SET cost_date = :d WHERE id = :i'),
            {'d': _date(row['year'], row['month'] or 1, 1), 'i': row['id']},
        )
    with op.batch_alter_table('running_costs', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_running_costs_month'))
        batch_op.drop_index(batch_op.f('ix_running_costs_year'))
        batch_op.alter_column('cost_date', existing_type=sa.Date(), nullable=False)
        batch_op.create_index(batch_op.f('ix_running_costs_cost_date'), ['cost_date'], unique=False)
        batch_op.drop_column('year')
        batch_op.drop_column('month')

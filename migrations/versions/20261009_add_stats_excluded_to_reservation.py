"""add stats_excluded to reservation

Revision ID: 20261009
Revises: 20261003
Create Date: 2026-10-09

Friends/family stays are kept as rows but excluded from revenue/avg/
occupancy/finance stats via Reservation.stats_excluded (default False).
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = '20261009'
down_revision = '20261003'
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()
    inspector = inspect(conn)
    columns = [c['name'] for c in inspector.get_columns('reservation')]
    if 'stats_excluded' not in columns:
        with op.batch_alter_table('reservation', schema=None) as batch_op:
            batch_op.add_column(sa.Column(
                'stats_excluded',
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
                comment='Keep the row but exclude from revenue/avg/occupancy/finance stats (compliance unaffected)',
            ))


def downgrade():
    with op.batch_alter_table('reservation', schema=None) as batch_op:
        batch_op.drop_column('stats_excluded')

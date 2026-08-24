"""add configurable access window hours to reservation

Revision ID: 20260824
Revises: f5dfcb91edf1
Create Date: 2026-08-24

"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = '20260824'
down_revision = 'f5dfcb91edf1'
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    columns = [c['name'] for c in inspector.get_columns('reservation')]
    with op.batch_alter_table('reservation', schema=None) as batch_op:
        if 'access_checkin_time' not in columns:
            batch_op.add_column(sa.Column('access_checkin_time', sa.String(length=5), nullable=True,
                                          comment='HH:MM on check-in day, Rome time (default 13:00)'))
        if 'access_checkout_time' not in columns:
            batch_op.add_column(sa.Column('access_checkout_time', sa.String(length=5), nullable=True,
                                          comment='HH:MM on check-out day, Rome time (default 13:00)'))


def downgrade():
    with op.batch_alter_table('reservation', schema=None) as batch_op:
        batch_op.drop_column('access_checkout_time')
        batch_op.drop_column('access_checkin_time')

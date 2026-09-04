"""add is_confirmed to receipts

Revision ID: 20260905
Revises: 20260904
Create Date: 2026-09-04 00:00:00.000000

"""
import sqlalchemy as sa
from alembic import op

revision = '20260905'
down_revision = '20260904'
branch_labels = None
depends_on = None

def upgrade():
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    cols = [c['name'] for c in inspector.get_columns('receipts')] if 'receipts' in inspector.get_table_names() else []
    with op.batch_alter_table('receipts', schema=None) as batch_op:
        if 'is_confirmed' not in cols:
            batch_op.add_column(sa.Column('is_confirmed', sa.Boolean(), nullable=False, server_default=sa.text('0')))
        if 'confirmed_at' not in cols:
            batch_op.add_column(sa.Column('confirmed_at', sa.DateTime(), nullable=True))

def downgrade():
    with op.batch_alter_table('receipts', schema=None) as batch_op:
        try:
            batch_op.drop_column('confirmed_at')
        except Exception:
            pass
        try:
            batch_op.drop_column('is_confirmed')
        except Exception:
            pass

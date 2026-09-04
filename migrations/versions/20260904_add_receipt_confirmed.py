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
    # PostgreSQL boolean needs 'false', SQLite accepts 0 — use no server_default then backfill
    with op.batch_alter_table('receipts', schema=None) as batch_op:
        if 'is_confirmed' not in cols:
            batch_op.add_column(sa.Column('is_confirmed', sa.Boolean(), nullable=True))
        if 'confirmed_at' not in cols:
            batch_op.add_column(sa.Column('confirmed_at', sa.DateTime(), nullable=True))
    # backfill existing rows to false, then set NOT NULL
    if 'is_confirmed' not in cols:
        if conn.dialect.name == 'postgresql':
            conn.execute(sa.text("UPDATE receipts SET is_confirmed = false WHERE is_confirmed IS NULL"))
        else:
            conn.execute(sa.text("UPDATE receipts SET is_confirmed = 0 WHERE is_confirmed IS NULL"))
        # Use batch again to set nullable=False (Postgres needs separate step)
        with op.batch_alter_table('receipts', schema=None) as batch_op:
            if conn.dialect.name == 'postgresql':
                batch_op.alter_column('is_confirmed', existing_type=sa.Boolean(), nullable=False, server_default=sa.text('false'))
            else:
                batch_op.alter_column('is_confirmed', existing_type=sa.Boolean(), nullable=False, server_default=sa.text('0'))

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

"""add bollo_image_path to receipts

Revision ID: 20260906
Revises: 20260905
Create Date: 2026-09-05 00:00:00.000000

"""
import sqlalchemy as sa
from alembic import op

revision = '20260906'
down_revision = '20260905'
branch_labels = None
depends_on = None

def upgrade():
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    cols = [c['name'] for c in inspector.get_columns('receipts')] if 'receipts' in inspector.get_table_names() else []
    if 'bollo_image_path' not in cols:
        with op.batch_alter_table('receipts', schema=None) as batch_op:
            batch_op.add_column(sa.Column('bollo_image_path', sa.String(length=300), nullable=True))

def downgrade():
    with op.batch_alter_table('receipts', schema=None) as batch_op:
        try:
            batch_op.drop_column('bollo_image_path')
        except Exception:
            pass

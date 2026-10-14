"""add company billing fields to receipts

Revision ID: 20261015
Revises: 20261014
Create Date: 2026-10-14 00:00:00.000000

Optional company header (denominazione / P.IVA / sede legale) so a receipt
can be made out to a company instead of a natural person. Nullable — existing
person receipts keep working.
"""

import sqlalchemy as sa
from alembic import op

revision = '20261015'
down_revision = '20261014'
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    cols = [c['name'] for c in inspector.get_columns('receipts')] if 'receipts' in inspector.get_table_names() else []
    with op.batch_alter_table('receipts', schema=None) as batch_op:
        if 'entity_type' not in cols:
            batch_op.add_column(sa.Column('entity_type', sa.String(length=10), nullable=True))
        if 'company_name' not in cols:
            batch_op.add_column(sa.Column('company_name', sa.String(length=200), nullable=True))
        if 'company_vat' not in cols:
            batch_op.add_column(sa.Column('company_vat', sa.String(length=20), nullable=True))
        if 'company_address' not in cols:
            batch_op.add_column(sa.Column('company_address', sa.String(length=250), nullable=True))


def downgrade():
    with op.batch_alter_table('receipts', schema=None) as batch_op:
        for col in ['company_address', 'company_vat', 'company_name', 'entity_type']:
            try:
                batch_op.drop_column(col)
            except Exception:
                pass

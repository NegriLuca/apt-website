"""add balance_invoice_sent_at to reservation

Revision ID: d5e6f7a8b9c0
Revises: 20260728_add_ross1000_fields
Create Date: 2026-08-04 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = 'd5e6f7a8b9c0'
down_revision = '20260728_add_ross1000_fields'
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()
    inspector = inspect(conn)
    columns = [c['name'] for c in inspector.get_columns('reservation')]
    if 'balance_invoice_sent_at' not in columns:
        with op.batch_alter_table('reservation', schema=None) as batch_op:
            batch_op.add_column(sa.Column(
                'balance_invoice_sent_at',
                sa.DateTime(),
                nullable=True,
                comment='When the balance invoice reminder email was sent',
            ))


def downgrade():
    with op.batch_alter_table('reservation', schema=None) as batch_op:
        batch_op.drop_column('balance_invoice_sent_at')

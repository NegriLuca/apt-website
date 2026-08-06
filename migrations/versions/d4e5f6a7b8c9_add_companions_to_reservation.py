"""add companions JSON to reservation

Revision ID: d4e5f6a7b8c9
Revises: c0ffee123456
Create Date: 2026-08-06 18:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = 'd4e5f6a7b8c9'
down_revision = 'c0ffee123456'
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()
    inspector = inspect(conn)
    columns = [c['name'] for c in inspector.get_columns('reservation')]
    if 'companions' not in columns:
        with op.batch_alter_table('reservation', schema=None) as batch_op:
            batch_op.add_column(sa.Column(
                'companions',
                sa.JSON(),
                nullable=True,
                comment='List of additional guest dicts (surname, first_name, birth_date, birth_place, nationality, gender, document_type, document_number, document_expiry, document_country)',
            ))


def downgrade():
    with op.batch_alter_table('reservation', schema=None) as batch_op:
        batch_op.drop_column('companions')

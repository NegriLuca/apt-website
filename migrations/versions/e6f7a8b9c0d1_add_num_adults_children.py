"""add num_adults and num_children to reservation

Revision ID: e6f7a8b9c0d1
Revises: d5e6f7a8b9c0
Create Date: 2026-08-04 13:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = 'e6f7a8b9c0d1'
down_revision = 'd5e6f7a8b9c0'
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()
    inspector = inspect(conn)
    columns = [c['name'] for c in inspector.get_columns('reservation')]
    if 'num_adults' not in columns:
        with op.batch_alter_table('reservation', schema=None) as batch_op:
            batch_op.add_column(sa.Column(
                'num_adults',
                sa.Integer(),
                nullable=True,
                comment='Number of adults (taxable for city tax)',
            ))
    if 'num_children' not in columns:
        with op.batch_alter_table('reservation', schema=None) as batch_op:
            batch_op.add_column(sa.Column(
                'num_children',
                sa.Integer(),
                nullable=True,
                comment='Number of children aged 3-9 (exempt from city tax)',
            ))


def downgrade():
    with op.batch_alter_table('reservation', schema=None) as batch_op:
        batch_op.drop_column('num_children')
        batch_op.drop_column('num_adults')

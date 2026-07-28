"""add tourist_tax_excluded column to reservation

Revision ID: 20260728_add_tourist_tax_excluded
Revises: c99797619e6f
Create Date: 2026-07-28 18:30:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = '20260728_add_tourist_tax_excluded'
down_revision = 'c99797619e6f'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('reservation', schema=None) as batch_op:
        batch_op.add_column(sa.Column('tourist_tax_excluded', sa.Boolean(), nullable=True, server_default=sa.text('false'), comment='Exclude from tourist tax reports'))


def downgrade():
    with op.batch_alter_table('reservation', schema=None) as batch_op:
        batch_op.drop_column('tourist_tax_excluded')

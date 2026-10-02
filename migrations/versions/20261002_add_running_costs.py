"""add running costs

Revision ID: 20261002
Revises: 20260906
Create Date: 2026-10-02

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = '20261002'
down_revision = '20260906'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'running_costs',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('cost_date', sa.Date(), nullable=False, comment='Date the cost refers to'),
        sa.Column('category', sa.String(length=30), nullable=False, comment='internet/cleaning/electricity/imu/other'),
        sa.Column('amount', sa.Float(), nullable=False, comment='Positive euro amount'),
        sa.Column('note', sa.String(length=250), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('running_costs', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_running_costs_cost_date'), ['cost_date'], unique=False)
        batch_op.create_index(batch_op.f('ix_running_costs_category'), ['category'], unique=False)


def downgrade():
    with op.batch_alter_table('running_costs', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_running_costs_category'))
        batch_op.drop_index(batch_op.f('ix_running_costs_cost_date'))

    op.drop_table('running_costs')

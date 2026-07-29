"""add ross1000 fields to reservation and create ross1000_log table

Revision ID: 20260728_add_ross1000_fields
Revises: 20260727_add_questura_fields
Create Date: 2026-07-28 10:00:00.000000

"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '20260728_add_ross1000_fields'
down_revision = 'b2c3d4e5f6a7'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('reservation', schema=None) as batch_op:
        batch_op.add_column(sa.Column('ross1000_status', sa.String(20), nullable=True, comment='pending, accepted, rejected'))
        batch_op.add_column(sa.Column('ross1000_submitted_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('ross1000_response', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('ross1000_error', sa.Text(), nullable=True))

    op.create_table(
        'ross1000_log',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('reservation_id', sa.Integer(), sa.ForeignKey('reservation.id'), nullable=False, index=True),
        sa.Column('action', sa.String(20), nullable=False),
        sa.Column('request_xml', sa.Text(), nullable=True),
        sa.Column('response_xml', sa.Text(), nullable=True),
        sa.Column('status', sa.String(20), nullable=False),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )


def downgrade():
    op.drop_table('ross1000_log')

    with op.batch_alter_table('reservation', schema=None) as batch_op:
        batch_op.drop_column('ross1000_error')
        batch_op.drop_column('ross1000_response')
        batch_op.drop_column('ross1000_submitted_at')
        batch_op.drop_column('ross1000_status')

"""add earnings table for Airbnb/Booking payouts

Revision ID: 20260825
Revises: 20260824
Create Date: 2026-08-25

"""
import sqlalchemy as sa
from alembic import op

revision = '20260825'
down_revision = '20260824'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('earnings',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('platform', sa.String(length=20), nullable=False),
        sa.Column('confirmation_code', sa.String(length=64), nullable=False),
        sa.Column('guest_name', sa.String(length=120), nullable=True),
        sa.Column('listing', sa.String(length=200), nullable=True),
        sa.Column('start_date', sa.Date(), nullable=True),
        sa.Column('end_date', sa.Date(), nullable=True),
        sa.Column('payout_date', sa.Date(), nullable=True),
        sa.Column('booking_date', sa.Date(), nullable=True),
        sa.Column('nights', sa.Integer(), nullable=True),
        sa.Column('currency', sa.String(length=10), nullable=False),
        sa.Column('amount', sa.Float(), nullable=False),
        sa.Column('service_fee', sa.Float(), nullable=False),
        sa.Column('cleaning_fee', sa.Float(), nullable=False),
        sa.Column('gross_earnings', sa.Float(), nullable=False),
        sa.Column('airbnb_tax', sa.Float(), nullable=False),
        sa.Column('withholding', sa.Float(), nullable=False),
        sa.Column('net', sa.Float(), nullable=False),
        sa.Column('reservation_id', sa.Integer(), nullable=True),
        sa.Column('raw_json', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['reservation_id'], ['reservation.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('platform', 'confirmation_code', name='uq_earning_platform_code')
    )
    with op.batch_alter_table('earnings', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_earnings_confirmation_code'), ['confirmation_code'], unique=False)
        batch_op.create_index(batch_op.f('ix_earnings_reservation_id'), ['reservation_id'], unique=False)


def downgrade():
    with op.batch_alter_table('earnings', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_earnings_reservation_id'))
        batch_op.drop_index(batch_op.f('ix_earnings_confirmation_code'))
    op.drop_table('earnings')

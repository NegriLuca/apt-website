"""add amount_paid and balance_payment_intent_id to reservation

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-07-28 19:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = 'b2c3d4e5f6a7'
down_revision = 'a1b2c3d4e5f6'
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()
    inspector = inspect(conn)
    columns = [c['name'] for c in inspector.get_columns('reservation')]
    if 'amount_paid' not in columns:
        with op.batch_alter_table('reservation', schema=None) as batch_op:
            batch_op.add_column(sa.Column('amount_paid', sa.Float(), nullable=True, server_default='0.0', comment='Amount actually charged so far'))
    if 'balance_payment_intent_id' not in columns:
        with op.batch_alter_table('reservation', schema=None) as batch_op:
            batch_op.add_column(sa.Column('balance_payment_intent_id', sa.String(128), nullable=True))
            batch_op.create_index(op.f('ix_reservation_balance_payment_intent_id'), ['balance_payment_intent_id'], unique=True)


def downgrade():
    with op.batch_alter_table('reservation', schema=None) as batch_op:
        batch_op.drop_index(op.f('ix_reservation_balance_payment_intent_id'))
        batch_op.drop_column('balance_payment_intent_id')
        batch_op.drop_column('amount_paid')

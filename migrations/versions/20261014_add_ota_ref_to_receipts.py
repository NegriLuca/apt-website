"""add OTA origin fields to receipts (Booking/Airbnb support)

Revision ID: 20261014
Revises: 20261009
Create Date: 2026-10-14 00:00:00.000000

Adds origin_source / external_ref / platform_net to receipts so fiscal
receipts can also be issued for OTA stays (Booking.com / Airbnb / VRBO),
referencing the platform reservation number. Existing direct receipts
keep working — new columns are nullable.
"""

import sqlalchemy as sa
from alembic import op

revision = '20261014'
down_revision = '20261009'
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    cols = [c['name'] for c in inspector.get_columns('receipts')] if 'receipts' in inspector.get_table_names() else []
    with op.batch_alter_table('receipts', schema=None) as batch_op:
        if 'origin_source' not in cols:
            batch_op.add_column(sa.Column('origin_source', sa.String(length=20), nullable=True))
        if 'external_ref' not in cols:
            batch_op.add_column(sa.Column('external_ref', sa.String(length=128), nullable=True))
        if 'platform_net' not in cols:
            batch_op.add_column(sa.Column('platform_net', sa.Float(), nullable=True))


def downgrade():
    with op.batch_alter_table('receipts', schema=None) as batch_op:
        for col in ['platform_net', 'external_ref', 'origin_source']:
            try:
                batch_op.drop_column(col)
            except Exception:
                pass

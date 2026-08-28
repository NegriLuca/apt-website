"""add boiler shelly fields to apartment

Revision ID: 20260826
Revises: 20260825
Create Date: 2026-08-26 00:00:00.000000

"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = '20260826'
down_revision = '20260825'
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    columns = [c['name'] for c in inspector.get_columns('apartment')]
    with op.batch_alter_table('apartment', schema=None) as batch_op:
        if 'boiler_shelly_enabled' not in columns:
            batch_op.add_column(sa.Column('boiler_shelly_enabled', sa.Boolean(), nullable=True, comment='Enable boiler auto ON/OFF'))
        if 'boiler_shelly_device_id' not in columns:
            batch_op.add_column(sa.Column('boiler_shelly_device_id', sa.String(length=100), nullable=True, comment='Shelly device ID for boiler (e.g., 206ef104b850)'))
        if 'boiler_shelly_channel' not in columns:
            batch_op.add_column(sa.Column('boiler_shelly_channel', sa.Integer(), nullable=True, comment='Relay channel for boiler (0 for Shelly 1 Mini)'))
        if 'boiler_shelly_host' not in columns:
            batch_op.add_column(sa.Column('boiler_shelly_host', sa.String(length=100), nullable=True, comment='Optional fallback host/IP for boiler Shelly (local mode)'))


def downgrade():
    with op.batch_alter_table('apartment', schema=None) as batch_op:
        batch_op.drop_column('boiler_shelly_host')
        batch_op.drop_column('boiler_shelly_channel')
        batch_op.drop_column('boiler_shelly_device_id')
        batch_op.drop_column('boiler_shelly_enabled')

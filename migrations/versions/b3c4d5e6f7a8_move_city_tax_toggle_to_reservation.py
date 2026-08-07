"""move guest_city_tax_enabled from apartment to reservation (idempotent)

Revision ID: b3c4d5e6f7a8
Revises: a1b2c3d4e5f7
Create Date: 2026-08-07 11:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = 'b3c4d5e6f7a8'
down_revision = 'a1b2c3d4e5f7'
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()
    inspector = inspect(conn)

    res_columns = [c['name'] for c in inspector.get_columns('reservation')]
    if 'guest_city_tax_enabled' not in res_columns:
        with op.batch_alter_table('reservation', schema=None) as batch_op:
            batch_op.add_column(
                sa.Column(
                    'guest_city_tax_enabled',
                    sa.Boolean(),
                    nullable=True,
                    server_default=sa.text('false'),
                    comment='Show the city tax Stripe payment option to this guest in check-in/messages',
                )
            )

    apt_columns = [c['name'] for c in inspector.get_columns('apartment')]
    if 'guest_city_tax_enabled' in apt_columns:
        with op.batch_alter_table('apartment', schema=None) as batch_op:
            batch_op.drop_column('guest_city_tax_enabled')


def downgrade():
    with op.batch_alter_table('apartment', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('guest_city_tax_enabled', sa.Boolean(), nullable=True, server_default=sa.text('false'))
        )
    with op.batch_alter_table('reservation', schema=None) as batch_op:
        batch_op.drop_column('guest_city_tax_enabled')

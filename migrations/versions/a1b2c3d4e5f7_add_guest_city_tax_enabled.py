"""add guest_city_tax_enabled column to apartment (idempotent)

Revision ID: a1b2c3d4e5f7
Revises: d4e5f6a7b8c9
Create Date: 2026-08-07 10:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = 'a1b2c3d4e5f7'
down_revision = 'd4e5f6a7b8c9'
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()
    inspector = inspect(conn)
    columns = [c['name'] for c in inspector.get_columns('apartment')]
    if 'guest_city_tax_enabled' not in columns:
        with op.batch_alter_table('apartment', schema=None) as batch_op:
            batch_op.add_column(
                sa.Column(
                    'guest_city_tax_enabled',
                    sa.Boolean(),
                    nullable=True,
                    server_default=sa.text('false'),
                    comment='Show the city tax Stripe payment option to guests in check-in/messages',
                )
            )


def downgrade():
    with op.batch_alter_table('apartment', schema=None) as batch_op:
        batch_op.drop_column('guest_city_tax_enabled')

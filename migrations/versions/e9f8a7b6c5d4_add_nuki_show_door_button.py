"""add nuki_show_door_button to apartment

Revision ID: e9f8a7b6c5d4
Revises: d3a5f2b1c4e9
Create Date: 2026-08-11 12:30:00.000000

"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = 'e9f8a7b6c5d4'
down_revision = 'd3a5f2b1c4e9'
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    columns = [c['name'] for c in inspector.get_columns('apartment')]
    if 'nuki_show_door_button' not in columns:
        with op.batch_alter_table('apartment', schema=None) as batch_op:
            batch_op.add_column(sa.Column(
                'nuki_show_door_button',
                sa.Boolean(),
                nullable=True,
                comment='Show the door button on the guest page (else guests use the keypad only)',
            ))


def downgrade():
    with op.batch_alter_table('apartment', schema=None) as batch_op:
        batch_op.drop_column('nuki_show_door_button')

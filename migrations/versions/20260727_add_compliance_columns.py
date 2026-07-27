"""add missing compliance columns to apartment

Revision ID: 20260727_add_compliance_columns
Revises: 771f86e69e3f
Create Date: 2026-07-27 17:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '20260727_add_compliance_columns'
down_revision = '771f86e69e3f'
branch_labels = None
depends_on = None


def upgrade():
    # Add missing columns to apartment table
    with op.batch_alter_table('apartment', schema=None) as batch_op:
        batch_op.add_column(sa.Column('cin_code', sa.String(50), nullable=True, comment='Codice Identificativo Nazionale'))
        batch_op.add_column(sa.Column('cir_code', sa.String(50), nullable=True, comment='Codice Identificativo Regionale Lazio'))
        batch_op.add_column(sa.Column('tourist_tax_category', sa.String(20), nullable=True, default='CAV', comment='CAV, BB, etc.'))
        batch_op.add_column(sa.Column('tourist_tax_rate', sa.Float(), nullable=True, default=6.00, comment='Euro per night per person'))
        batch_op.add_column(sa.Column('max_guests', sa.Integer(), nullable=True, default=4))
        batch_op.add_column(sa.Column('questura_protocol', sa.String(50), nullable=True, comment='Protocollo Questura per AlloggiatiWeb'))
        batch_op.add_column(sa.Column('questura_ip_whitelisted', sa.Boolean(), default=False))


def downgrade():
    with op.batch_alter_table('apartment', schema=None) as batch_op:
        batch_op.drop_column('questura_ip_whitelisted')
        batch_op.drop_column('questura_protocol')
        batch_op.drop_column('max_guests')
        batch_op.drop_column('tourist_tax_rate')
        batch_op.drop_column('tourist_tax_category')
        batch_op.drop_column('cir_code')
        batch_op.drop_column('cin_code')
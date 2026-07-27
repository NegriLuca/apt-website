"""add questura guest fields to reservation

Revision ID: 20260727_add_questura_fields
Revises: 20260727_add_compliance_columns
Create Date: 2026-07-27 17:30:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '20260727_add_questura_fields'
down_revision = '20260727_add_compliance_columns'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('reservation', schema=None) as batch_op:
        batch_op.add_column(sa.Column('guest_surname', sa.String(100), nullable=True))
        batch_op.add_column(sa.Column('guest_first_name', sa.String(100), nullable=True))
        batch_op.add_column(sa.Column('guest_birth_date', sa.Date(), nullable=True))
        batch_op.add_column(sa.Column('guest_birth_place', sa.String(100), nullable=True))
        batch_op.add_column(sa.Column('guest_nationality', sa.String(50), nullable=True))
        batch_op.add_column(sa.Column('guest_document_type', sa.String(20), nullable=True, comment='passport, id_card, driving_license'))
        batch_op.add_column(sa.Column('guest_document_number', sa.String(50), nullable=True))
        batch_op.add_column(sa.Column('guest_document_expiry', sa.Date(), nullable=True))
        batch_op.add_column(sa.Column('guest_document_country', sa.String(3), nullable=True, comment='ISO 3166-1 alpha-3'))
        batch_op.add_column(sa.Column('guest_gender', sa.String(1), nullable=True, comment='M/F'))
        batch_op.add_column(sa.Column('checkin_token', sa.String(128), nullable=True))
        batch_op.add_column(sa.Column('checkin_completed_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('checkin_token_used', sa.Boolean(), default=False))
        batch_op.add_column(sa.Column('questura_submitted_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('questura_response', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('questura_status', sa.String(20), nullable=True, comment='pending, sent, accepted, rejected'))
        batch_op.add_column(sa.Column('questura_error', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('tourist_tax_amount', sa.Float(), nullable=True, default=0.0))
        batch_op.add_column(sa.Column('tourist_tax_paid', sa.Boolean(), default=False))
    
    # Create index on checkin_token
    op.create_index('ix_reservation_checkin_token', 'reservation', ['checkin_token'], unique=True)


def downgrade():
    op.drop_index('ix_reservation_checkin_token', 'reservation')
    
    with op.batch_alter_table('reservation', schema=None) as batch_op:
        batch_op.drop_column('tourist_tax_paid')
        batch_op.drop_column('tourist_tax_amount')
        batch_op.drop_column('questura_error')
        batch_op.drop_column('questura_status')
        batch_op.drop_column('questura_response')
        batch_op.drop_column('questura_submitted_at')
        batch_op.drop_column('checkin_token_used')
        batch_op.drop_column('checkin_completed_at')
        batch_op.drop_column('checkin_token')
        batch_op.drop_column('guest_gender')
        batch_op.drop_column('guest_document_country')
        batch_op.drop_column('guest_document_expiry')
        batch_op.drop_column('guest_document_number')
        batch_op.drop_column('guest_document_type')
        batch_op.drop_column('guest_nationality')
        batch_op.drop_column('guest_birth_place')
        batch_op.drop_column('guest_birth_date')
        batch_op.drop_column('guest_first_name')
        batch_op.drop_column('guest_surname')
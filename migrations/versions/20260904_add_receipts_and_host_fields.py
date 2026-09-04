"""add receipts + host/guest billing fields for italian fiscal receipt

Revision ID: 20260904
Revises: 20260826
Create Date: 2026-09-04 00:00:00.000000

"""
import sqlalchemy as sa
from alembic import op

revision = '20260904'
down_revision = '20260826'
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    # Apartment host fields
    apt_cols = [c['name'] for c in inspector.get_columns('apartment')]
    with op.batch_alter_table('apartment', schema=None) as batch_op:
        if 'host_full_name' not in apt_cols:
            batch_op.add_column(sa.Column('host_full_name', sa.String(length=150), nullable=True, comment='Nome e Cognome proprietario/gestore'))
        if 'host_codice_fiscale' not in apt_cols:
            batch_op.add_column(sa.Column('host_codice_fiscale', sa.String(length=20), nullable=True, comment='Codice Fiscale proprietario'))
        if 'host_address' not in apt_cols:
            batch_op.add_column(sa.Column('host_address', sa.String(length=250), nullable=True, comment='Indirizzo struttura (Via, CAP, Roma)'))
        if 'host_vat_mode' not in apt_cols:
            batch_op.add_column(sa.Column('host_vat_mode', sa.String(length=50), nullable=True, comment='Regime fiscale'))

    # Reservation guest/billing fields
    res_cols = [c['name'] for c in inspector.get_columns('reservation')]
    with op.batch_alter_table('reservation', schema=None) as batch_op:
        if 'guest_residence_address' not in res_cols:
            batch_op.add_column(sa.Column('guest_residence_address', sa.String(length=250), nullable=True))
        if 'guest_residence_city' not in res_cols:
            batch_op.add_column(sa.Column('guest_residence_city', sa.String(length=100), nullable=True))
        if 'guest_residence_zip' not in res_cols:
            batch_op.add_column(sa.Column('guest_residence_zip', sa.String(length=20), nullable=True))
        if 'guest_residence_country' not in res_cols:
            batch_op.add_column(sa.Column('guest_residence_country', sa.String(length=100), nullable=True))
        if 'guest_codice_fiscale' not in res_cols:
            batch_op.add_column(sa.Column('guest_codice_fiscale', sa.String(length=20), nullable=True))
        if 'guest_billing_address_line1' not in res_cols:
            batch_op.add_column(sa.Column('guest_billing_address_line1', sa.String(length=250), nullable=True))
        if 'guest_billing_address_line2' not in res_cols:
            batch_op.add_column(sa.Column('guest_billing_address_line2', sa.String(length=250), nullable=True))
        if 'guest_billing_city' not in res_cols:
            batch_op.add_column(sa.Column('guest_billing_city', sa.String(length=100), nullable=True))
        if 'guest_billing_postal_code' not in res_cols:
            batch_op.add_column(sa.Column('guest_billing_postal_code', sa.String(length=20), nullable=True))
        if 'guest_billing_country' not in res_cols:
            batch_op.add_column(sa.Column('guest_billing_country', sa.String(length=5), nullable=True))
        if 'guest_billing_state' not in res_cols:
            batch_op.add_column(sa.Column('guest_billing_state', sa.String(length=100), nullable=True))
        if 'stripe_charge_id' not in res_cols:
            batch_op.add_column(sa.Column('stripe_charge_id', sa.String(length=128), nullable=True))
        if 'stripe_receipt_url' not in res_cols:
            batch_op.add_column(sa.Column('stripe_receipt_url', sa.String(length=500), nullable=True))

    # Receipts table
    if 'receipts' not in inspector.get_table_names():
        op.create_table('receipts',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('reservation_id', sa.Integer(), nullable=False),
            sa.Column('year', sa.Integer(), nullable=False),
            sa.Column('sequence', sa.Integer(), nullable=False),
            sa.Column('receipt_number', sa.String(length=20), nullable=False),
            sa.Column('issue_date', sa.Date(), nullable=False),
            sa.Column('stay_amount', sa.Float(), nullable=False),
            sa.Column('tourist_tax_amount', sa.Float(), nullable=False),
            sa.Column('total_amount', sa.Float(), nullable=False),
            sa.Column('payment_method', sa.String(length=50), nullable=True),
            sa.Column('stripe_payment_intent_id', sa.String(length=128), nullable=True),
            sa.Column('stripe_charge_id', sa.String(length=128), nullable=True),
            sa.Column('stripe_receipt_url', sa.String(length=500), nullable=True),
            sa.Column('bollo_required', sa.Boolean(), nullable=True),
            sa.Column('bollo_amount', sa.Float(), nullable=True),
            sa.Column('bollo_id', sa.String(length=30), nullable=True),
            sa.Column('host_full_name', sa.String(length=150), nullable=True),
            sa.Column('host_codice_fiscale', sa.String(length=20), nullable=True),
            sa.Column('host_address', sa.String(length=250), nullable=True),
            sa.Column('cin_code', sa.String(length=50), nullable=True),
            sa.Column('cir_code', sa.String(length=50), nullable=True),
            sa.Column('guest_full_name', sa.String(length=150), nullable=True),
            sa.Column('guest_email', sa.String(length=120), nullable=True),
            sa.Column('guest_residence_address', sa.String(length=250), nullable=True),
            sa.Column('guest_residence_city', sa.String(length=100), nullable=True),
            sa.Column('guest_residence_zip', sa.String(length=20), nullable=True),
            sa.Column('guest_residence_country', sa.String(length=100), nullable=True),
            sa.Column('guest_codice_fiscale', sa.String(length=20), nullable=True),
            sa.Column('guest_document_type', sa.String(length=20), nullable=True),
            sa.Column('guest_document_number', sa.String(length=50), nullable=True),
            sa.Column('check_in', sa.Date(), nullable=True),
            sa.Column('check_out', sa.Date(), nullable=True),
            sa.Column('nights', sa.Integer(), nullable=True),
            sa.Column('num_guests', sa.Integer(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=True),
            sa.Column('updated_at', sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(['reservation_id'], ['reservation.id'], ),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('receipt_number', name='uq_receipt_number'),
            sa.UniqueConstraint('reservation_id', name='uq_receipt_reservation'),
            sa.UniqueConstraint('year', 'sequence', name='uq_receipt_year_sequence')
        )
        with op.batch_alter_table('receipts', schema=None) as batch_op:
            batch_op.create_index(batch_op.f('ix_receipts_receipt_number'), ['receipt_number'], unique=True)
            batch_op.create_index(batch_op.f('ix_receipts_reservation_id'), ['reservation_id'], unique=True)
            batch_op.create_index(batch_op.f('ix_receipts_year'), ['year'], unique=False)


def downgrade():
    with op.batch_alter_table('receipts', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_receipts_year'))
        batch_op.drop_index(batch_op.f('ix_receipts_reservation_id'))
        batch_op.drop_index(batch_op.f('ix_receipts_receipt_number'))
    op.drop_table('receipts')
    with op.batch_alter_table('reservation', schema=None) as batch_op:
        for col in ['stripe_receipt_url', 'stripe_charge_id', 'guest_billing_state', 'guest_billing_country', 'guest_billing_postal_code', 'guest_billing_city', 'guest_billing_address_line2', 'guest_billing_address_line1', 'guest_codice_fiscale', 'guest_residence_country', 'guest_residence_zip', 'guest_residence_city', 'guest_residence_address']:
            try:
                batch_op.drop_column(col)
            except Exception:
                pass
    with op.batch_alter_table('apartment', schema=None) as batch_op:
        for col in ['host_vat_mode', 'host_address', 'host_codice_fiscale', 'host_full_name']:
            try:
                batch_op.drop_column(col)
            except Exception:
                pass

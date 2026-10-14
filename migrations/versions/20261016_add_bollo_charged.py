"""split bollo display vs charge (host always pays the stamp)

Revision ID: 20261016
Revises: 20261015
Create Date: 2026-10-14 00:00:00.000000

Adds receipts.bollo_charged. Rule going forward: above €77.47 the stamp is
always shown (paid out-of-pocket by the host) and NEVER included in totals
(total = stay + tourist tax). Backfills:
- required=1 on every over-threshold receipt (even those created while the
  stamp was hidden, e.g. OTA receipts with a saved bollo ID);
- charged=1 only where the stored total already includes the €2 (legacy
  direct receipts), so old PDFs keep rendering the same.
"""

import sqlalchemy as sa
from alembic import op

revision = '20261016'
down_revision = '20261015'
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    cols = [c['name'] for c in inspector.get_columns('receipts')] if 'receipts' in inspector.get_table_names() else []
    with op.batch_alter_table('receipts', schema=None) as batch_op:
        if 'bollo_charged' not in cols:
            batch_op.add_column(sa.Column('bollo_charged', sa.Boolean(), nullable=True, server_default='0'))
    # Stamp shown on every over-threshold receipt, ID present or not
    conn.execute(sa.text(
        "UPDATE receipts SET bollo_required=1 "
        "WHERE stay_amount > 77.47 AND (bollo_required IS NULL OR bollo_required = 0)"
    ))
    # Charged only where the stored total already includes the €2
    conn.execute(sa.text(
        "UPDATE receipts SET bollo_charged=1 "
        "WHERE bollo_required = 1 AND total_amount >= stay_amount + tourist_tax_amount + 1.99"
    ))


def downgrade():
    with op.batch_alter_table('receipts', schema=None) as batch_op:
        try:
            batch_op.drop_column('bollo_charged')
        except Exception:
            pass

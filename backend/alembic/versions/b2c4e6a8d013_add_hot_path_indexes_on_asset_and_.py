"""add hot-path indexes on asset and receipt_item

Pure read-path accelerators on four columns that the analytics, planning, and
provenance queries filter or join on but that were never indexed:

  - receipt_item.order_item_id  — every received-quantity SUM rollup filters here
  - asset.status                — on-hand / deployed capacity counts and the
                                  /assets?status= filter narrow by status
  - asset.source_order_item_id  — the spend-analytics join and provenance lookup
  - asset.current_location_id   — capacity group-bys and the location filter

Additive and backward-compatible: CREATE INDEX only, no data change, no column
change. Invisible on a small DB; at 100k+ assets these turn sequential scans
into index lookups on every dashboard hit.

Revision ID: b2c4e6a8d013
Revises: f1a2b3c4d5e6
Create Date: 2026-06-10 09:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'b2c4e6a8d013'
down_revision: Union[str, Sequence[str], None] = 'f1a2b3c4d5e6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('receipt_item', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_receipt_item_order_item_id'),
                              ['order_item_id'], unique=False)
    with op.batch_alter_table('asset', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_asset_status'), ['status'], unique=False)
        batch_op.create_index(batch_op.f('ix_asset_current_location_id'),
                              ['current_location_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_asset_source_order_item_id'),
                              ['source_order_item_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('asset', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_asset_source_order_item_id'))
        batch_op.drop_index(batch_op.f('ix_asset_current_location_id'))
        batch_op.drop_index(batch_op.f('ix_asset_status'))
    with op.batch_alter_table('receipt_item', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_receipt_item_order_item_id'))

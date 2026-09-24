"""The world clock, and the index the forecast backtest walks

Two things the simulation tab needed from the schema:

* ``world_clock``: one row per time the simulation let days pass (how many, when, which
  event). The simulation moves every date the fleet carries back by N days, which every
  read sees as the same world N days later; the log is what lets a KPI measurement say
  how many simulated days ago it was taken and whether it still stands. Emptied by a
  rebuild like every operational table.
* ``ix_asset_status_deployed_product``: the forecast backtest asks nineteen times per
  run for deployments per product in service since a date (``status IN (...) AND
  deployed_date > ?``, grouped by product). Without an index that carries all three
  columns the planner picks between two ``status`` indexes on a tie, and after the
  simulation rebuilt the date indexes the tie broke the other way: the same nineteen
  statements went from 3.2 to 19.5 seconds on the full fleet, because the losing index
  walks the rows in date order. With this index the plan is the same whatever the order
  of the others, and the backtest measured 5.4 seconds against 8.2 on the seeded fleet.

Additive only.

Revision ID: e3f5a7b9c1d2
Revises: d2e4f6a8b0c1
Create Date: 2026-09-24 14:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'e3f5a7b9c1d2'
down_revision: Union[str, None] = 'd2e4f6a8b0c1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'world_clock',
        sa.Column('days', sa.Integer(), nullable=False),
        sa.Column('advanced_at', sa.DateTime(), nullable=False),
        sa.Column('action', sa.String(length=32), nullable=False),
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('date_created', sa.DateTime(), nullable=False),
        sa.Column('last_updated', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('asset', schema=None) as batch_op:
        batch_op.create_index('ix_asset_status_deployed_product', ['status', 'deployed_date', 'product_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('asset', schema=None) as batch_op:
        batch_op.drop_index('ix_asset_status_deployed_product')
    op.drop_table('world_clock')

"""Indexes for a 400,000-device fleet

The DaaS dataset is three orders of magnitude larger than the datacenter demo, and
every screen asks the same four questions of it: how many devices are in each station
and how long have they been there, which contracts end in a window, how many first
rentals started per month, and what a product's recent deployments were. Each of those
is a range scan over one or two columns; with a single-column index on ``status`` alone
the database still had to touch every row of the fleet to answer them.

These are plain composite indexes — additive, no data change, and they help Postgres
and SQLite alike. Names follow the existing ``ix_<table>_<columns>`` convention.

Revision ID: f6a8b0c2d357
Revises: e5f7a9c1d246
Create Date: 2026-09-22 23:40:00.000000

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'f6a8b0c2d357'
down_revision: Union[str, None] = 'e5f7a9c1d246'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# (index name, table, columns)
INDEXES = [
    # the warehouse stations and their dwell time (fleet summary, aging and median KPIs)
    ("ix_asset_status_status_since", "asset", ["status", "status_since"]),
    # deployments per product over a window (demand forecast, backtest, stock turns)
    ("ix_asset_product_deployed", "asset", ["product_id", "deployed_date"]),
    ("ix_asset_deployed_date", "asset", ["deployed_date"]),
    # resale in the last twelve months
    ("ix_asset_sold_date", "asset", ["sold_date"]),
    # spend by provenance: every serial joins to its order line
    ("ix_asset_status_product", "asset", ["status", "product_id"]),
    # "how many of the rented devices are on their second rental" - the overview's first line
    ("ix_asset_status_cycle", "asset", ["status", "cycle_no"]),
    # the calendar joins every running contract to its device: a covering pair keeps that
    # join inside the index instead of fetching 300,000 rows
    ("ix_asset_id_product", "asset", ["id", "product_id"]),
    # the return calendar and the due-in-N-days counts
    ("ix_rental_status_planned_end", "rental_contract", ["status", "planned_end"]),
    # contracts that ended in the last 90 days, and the running-at-a-date comparison
    ("ix_rental_status_actual_end", "rental_contract", ["status", "actual_end"]),
    # first rentals per month (the growth curve)
    ("ix_rental_cycle_start", "rental_contract", ["cycle_no", "start_date"]),
    # "how many contracts were running a year ago" - the honest base for the growth figure
    ("ix_rental_start_actual_end", "rental_contract", ["start_date", "actual_end"]),
]


def upgrade() -> None:
    for name, table, cols in INDEXES:
        op.create_index(name, table, cols)


def downgrade() -> None:
    for name, table, _cols in reversed(INDEXES):
        op.drop_index(name, table_name=table)

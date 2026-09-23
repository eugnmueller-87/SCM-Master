"""Device TCO: the model on the rental contract, and service events with a cost

The device-as-a-service fleet had no cost layer of its own: the five TCO tables
model a datacenter asset (power, cooling, racking) and stay empty for a fleet of
phones and laptops. What a rented device costs is a handful of measured quantities
times a rate, plus one fact the fleet did not record at all: what each repair and
each refurbishment cost. This adds that fact as ``service_event``.

It also copies the device's model onto the rental contract. The TCO asks the
contracts for months in service per model, and the only way to answer that was to
probe the asset row of every one of 500,000 contracts, two seconds on the full
fleet. A device never changes model, so the copy cannot drift, and with it the read
is an index scan. Existing rows are backfilled here. Additive apart from that.

Revision ID: c9d1e3f5a680
Revises: b8c0d2e4f579
Create Date: 2026-09-23 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'c9d1e3f5a680'
down_revision: Union[str, None] = 'b8c0d2e4f579'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('rental_contract', schema=None) as batch_op:
        batch_op.add_column(sa.Column('product_id', sa.String(length=36), nullable=True))
        batch_op.create_index(batch_op.f('ix_rental_contract_product_id'), ['product_id'], unique=False)
        batch_op.create_index('ix_rental_product_cycle_start', ['product_id', 'cycle_no', 'start_date'], unique=False)
        batch_op.create_index('ix_rental_product_cycle_end', ['product_id', 'cycle_no', 'actual_end', 'end_reason'], unique=False)
        batch_op.create_foreign_key('fk_rental_contract_product', 'product', ['product_id'], ['id'])
        # the device's rental history from the index alone; leads with asset_id, so it takes
        # over the foreign key lookups of the single-column index it replaces
        batch_op.drop_index(batch_op.f('ix_rental_contract_asset_id'))
        batch_op.create_index('ix_rental_asset_life', ['asset_id', 'cycle_no', 'start_date', 'actual_end', 'end_reason'], unique=False)

    # Backfill from the asset each contract points at. One statement per dialect: Postgres
    # has UPDATE ... FROM, SQLite takes the correlated form (one indexed lookup per row).
    bind = op.get_bind()
    if bind.dialect.name == 'postgresql':
        op.execute(
            "UPDATE rental_contract AS c SET product_id = a.product_id "
            "FROM asset AS a WHERE a.id = c.asset_id AND c.product_id IS NULL"
        )
    else:
        op.execute(
            "UPDATE rental_contract SET product_id = "
            "(SELECT a.product_id FROM asset AS a WHERE a.id = rental_contract.asset_id) "
            "WHERE product_id IS NULL"
        )

    op.create_table(
        'service_event',
        sa.Column('asset_id', sa.String(length=36), nullable=False),
        sa.Column('product_id', sa.String(length=36), nullable=False),
        sa.Column('kind', sa.Enum('REPAIR', 'REFURB', name='servicekind'), nullable=False),
        sa.Column('cycle_no', sa.Integer(), nullable=False),
        sa.Column('event_date', sa.Date(), nullable=False),
        sa.Column('cost', sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column('currency', sa.String(length=3), nullable=False),
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('date_created', sa.DateTime(), nullable=False),
        sa.Column('last_updated', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['asset_id'], ['asset.id']),
        sa.ForeignKeyConstraint(['product_id'], ['product.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('service_event', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_service_event_product_id'), ['product_id'], unique=False)
        batch_op.create_index('ix_service_event_product_kind_cost', ['product_id', 'kind', 'cost'], unique=False)
        batch_op.create_index('ix_service_event_asset_kind_cost', ['asset_id', 'kind', 'cost'], unique=False)

    # Two covering indexes on asset for the device TCO's reads (the why is on the model). A
    # join driven from a status set had to fetch the row behind each of 31,200 finished
    # devices just for its id; the dwell and resale reads by order line fetched 100,000 rows.
    op.create_index('ix_asset_status_id_product', 'asset', ['status', 'id', 'product_id'])
    op.create_index('ix_asset_status_line_since_sale', 'asset', ['status', 'source_order_item_id', 'status_since', 'sale_price'])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_asset_status_line_since_sale', table_name='asset')
    op.drop_index('ix_asset_status_id_product', table_name='asset')
    with op.batch_alter_table('service_event', schema=None) as batch_op:
        batch_op.drop_index('ix_service_event_asset_kind_cost')
        batch_op.drop_index('ix_service_event_product_kind_cost')
        batch_op.drop_index(batch_op.f('ix_service_event_product_id'))
    op.drop_table('service_event')
    bind = op.get_bind()
    if bind.dialect.name == 'postgresql':
        op.execute("DROP TYPE IF EXISTS servicekind")

    with op.batch_alter_table('rental_contract', schema=None) as batch_op:
        batch_op.drop_index('ix_rental_asset_life')
        batch_op.create_index(batch_op.f('ix_rental_contract_asset_id'), ['asset_id'], unique=False)
        batch_op.drop_constraint('fk_rental_contract_product', type_='foreignkey')
        batch_op.drop_index('ix_rental_product_cycle_end')
        batch_op.drop_index('ix_rental_product_cycle_start')
        batch_op.drop_index(batch_op.f('ix_rental_contract_product_id'))
        batch_op.drop_column('product_id')

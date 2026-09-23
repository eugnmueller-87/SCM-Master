"""DaaS cycle: rental states on the asset, rental_contract table

The device-as-a-service scenario: a device is bought, rented, taken back,
graded, repaired or refurbished, rented again, sold or recycled. Additive:
new enum values on assetstatus (Postgres: ALTER TYPE ... ADD VALUE; SQLite
stores the enum as text), nullable columns on asset, one new table.

Revision ID: e5f7a9c1d246
Revises: d4e6f8b0c135
Create Date: 2026-09-22 11:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e5f7a9c1d246'
down_revision: Union[str, Sequence[str], None] = 'd4e6f8b0c135'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

NEW_STATES = ('RENTED', 'RETURNED', 'MDM_RELEASE', 'WIPE_GRADING', 'REPAIR', 'REFURB', 'SELLABLE', 'SWAP_BUFFER', 'SOLD', 'RECYCLED')


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    if bind.dialect.name == 'postgresql':
        # Postgres 12+ accepts ADD VALUE inside a transaction as long as the new value
        # is not used in the same transaction; this migration only adds.
        for v in NEW_STATES:
            op.execute(f"ALTER TYPE assetstatus ADD VALUE IF NOT EXISTS '{v}'")

    with op.batch_alter_table('asset', schema=None) as batch_op:
        batch_op.add_column(sa.Column('cycle_no', sa.Integer(), nullable=False, server_default='0'))
        batch_op.add_column(sa.Column('grade', sa.String(length=1), nullable=True))
        batch_op.add_column(sa.Column('battery_health', sa.Float(), nullable=True))
        batch_op.add_column(sa.Column('customer_id', sa.String(length=36), nullable=True))
        batch_op.add_column(sa.Column('status_since', sa.Date(), nullable=True))
        batch_op.add_column(sa.Column('sold_date', sa.Date(), nullable=True))
        batch_op.add_column(sa.Column('sale_price', sa.Numeric(14, 2), nullable=True))
        batch_op.add_column(sa.Column('sale_channel', sa.String(length=32), nullable=True))
        batch_op.create_index(batch_op.f('ix_asset_customer_id'), ['customer_id'], unique=False)
        batch_op.create_foreign_key('fk_asset_customer', 'organization', ['customer_id'], ['id'])

    op.create_table(
        'rental_contract',
        sa.Column('asset_id', sa.String(length=36), nullable=False),
        sa.Column('customer_id', sa.String(length=36), nullable=False),
        sa.Column('cycle_no', sa.Integer(), nullable=False),
        sa.Column('start_date', sa.Date(), nullable=False),
        sa.Column('term_months', sa.Integer(), nullable=False),
        sa.Column('planned_end', sa.Date(), nullable=False),
        sa.Column('actual_end', sa.Date(), nullable=True),
        sa.Column('end_reason', sa.String(length=24), nullable=True),
        sa.Column('rent_eur_month', sa.Numeric(14, 2), nullable=True),
        sa.Column('status', sa.Enum('RUNNING', 'ENDED', name='contractstatus'), nullable=False),
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('date_created', sa.DateTime(), nullable=False),
        sa.Column('last_updated', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['asset_id'], ['asset.id']),
        sa.ForeignKeyConstraint(['customer_id'], ['organization.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('rental_contract', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_rental_contract_asset_id'), ['asset_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_rental_contract_customer_id'), ['customer_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_rental_contract_start_date'), ['start_date'], unique=False)
        batch_op.create_index(batch_op.f('ix_rental_contract_planned_end'), ['planned_end'], unique=False)
        batch_op.create_index(batch_op.f('ix_rental_contract_status'), ['status'], unique=False)


def downgrade() -> None:
    """Downgrade schema. Enum values stay (Postgres cannot drop them safely); columns and the table go."""
    with op.batch_alter_table('rental_contract', schema=None) as batch_op:
        for ix in ('status', 'planned_end', 'start_date', 'customer_id', 'asset_id'):
            batch_op.drop_index(batch_op.f(f'ix_rental_contract_{ix}'))
    op.drop_table('rental_contract')
    bind = op.get_bind()
    if bind.dialect.name == 'postgresql':
        op.execute("DROP TYPE IF EXISTS contractstatus")
    with op.batch_alter_table('asset', schema=None) as batch_op:
        batch_op.drop_constraint('fk_asset_customer', type_='foreignkey')
        batch_op.drop_index(batch_op.f('ix_asset_customer_id'))
        for col in ('sale_channel', 'sale_price', 'sold_date', 'status_since', 'customer_id', 'battery_health', 'grade', 'cycle_no'):
            batch_op.drop_column(col)

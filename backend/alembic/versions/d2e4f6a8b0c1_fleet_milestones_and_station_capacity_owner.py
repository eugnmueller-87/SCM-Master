"""Capacity plan: fleet milestones, who set a station's capacity, the term index

The business owner asked what capacity we assume per compartment, how fast each has to
turn to reach 500,000 devices at customers by the end of 2026 and 1,000,000 by the end
of 2027, and when more room has to be leased. Three things the schema did not hold:

* ``fleet_milestone``: a target fleet on a date with its owner, edited through the API
  like a KPI target, so "and if it were 800,000?" is a request and not a deploy.
* ``location.capacity_set_by``: empty while the capacity is the seed's design parameter,
  a name once a person decided it. The plan shows which, because "how much room do we
  assume" is the question.
* ``ix_rental_status_cycle_term``: the mean rental term of the running contracts, by
  cycle, from the index alone. Reading the term behind each of 300,000 running rows
  took 3.4 seconds cold on the full fleet; with this it is six index entries.

Additive only.

Revision ID: d2e4f6a8b0c1
Revises: c9d1e3f5a680
Create Date: 2026-09-24 12:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'd2e4f6a8b0c1'
down_revision: Union[str, None] = 'c9d1e3f5a680'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'fleet_milestone',
        sa.Column('milestone_date', sa.Date(), nullable=False),
        sa.Column('target_fleet', sa.Integer(), nullable=False),
        sa.Column('owner', sa.String(length=128), nullable=True),
        sa.Column('note', sa.Text(), nullable=True),
        sa.Column('placeholder', sa.Boolean(), nullable=False),
        sa.Column('updated_by', sa.String(length=128), nullable=True),
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('date_created', sa.DateTime(), nullable=False),
        sa.Column('last_updated', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('fleet_milestone', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_fleet_milestone_milestone_date'), ['milestone_date'], unique=True)

    with op.batch_alter_table('location', schema=None) as batch_op:
        batch_op.add_column(sa.Column('capacity_set_by', sa.String(length=128), nullable=True))

    with op.batch_alter_table('rental_contract', schema=None) as batch_op:
        batch_op.create_index('ix_rental_status_cycle_term', ['status', 'cycle_no', 'term_months'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('rental_contract', schema=None) as batch_op:
        batch_op.drop_index('ix_rental_status_cycle_term')

    with op.batch_alter_table('location', schema=None) as batch_op:
        batch_op.drop_column('capacity_set_by')

    with op.batch_alter_table('fleet_milestone', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_fleet_milestone_milestone_date'))
    op.drop_table('fleet_milestone')

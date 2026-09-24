"""The movement log's time: when a move happened on the fleet's calendar, and the stay it ended

``asset_event`` already recorded ``from_status -> to_status`` on every transition, and a
compartment is defined by its statuses, so the log has said which compartment a device
left and entered since the lifecycle log was added. It could not say when on the fleet's
own calendar (only the real wall clock ``date_created``, which the simulation's calendar
never moves) or how long the device had been in the compartment it left. Three columns,
written by the asset service at the moment of the move: ``effective_date`` (the day, the
same stamp that restarts the dwell clock), ``from_since`` (the dwell start the device
carried in the compartment it left) and ``dwell_days`` (their difference). The dates move
with the demo's calendar like every other Date column; the day count is their difference
and stays.

One index for the window read of the movement log, carrying every column that read
groups on. Rows written before this migration carry none of the three; the reads say so.

Additive only.

Revision ID: f7a9b1c3d5e4
Revises: e3f5a7b9c1d2
Create Date: 2026-09-24 18:30:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'f7a9b1c3d5e4'
down_revision: Union[str, None] = 'e3f5a7b9c1d2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('asset_event', schema=None) as batch_op:
        batch_op.add_column(sa.Column('effective_date', sa.Date(), nullable=True))
        batch_op.add_column(sa.Column('from_since', sa.Date(), nullable=True))
        batch_op.add_column(sa.Column('dwell_days', sa.Integer(), nullable=True))
        batch_op.create_index('ix_asset_event_window', ['effective_date', 'from_status', 'to_status', 'dwell_days'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('asset_event', schema=None) as batch_op:
        batch_op.drop_index('ix_asset_event_window')
        batch_op.drop_column('dwell_days')
        batch_op.drop_column('from_since')
        batch_op.drop_column('effective_date')

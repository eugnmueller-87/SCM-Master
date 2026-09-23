"""add kpi_target and kpi_snapshot

The KPIs tab: targets for one, two and three years per KPI (owned, placeholder
until confirmed) and one value per KPI per day, so the trend is measured rather
than invented. Additive only, no existing table is touched.

Revision ID: d4e6f8b0c135
Revises: c3d5e7a9f024
Create Date: 2026-09-22 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd4e6f8b0c135'
down_revision: Union[str, Sequence[str], None] = 'c3d5e7a9f024'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'kpi_target',
        sa.Column('kpi_id', sa.String(length=64), nullable=False),
        sa.Column('target_y1', sa.Float(), nullable=True),
        sa.Column('target_y2', sa.Float(), nullable=True),
        sa.Column('target_y3', sa.Float(), nullable=True),
        sa.Column('owner', sa.String(length=128), nullable=True),
        sa.Column('note', sa.Text(), nullable=True),
        sa.Column('placeholder', sa.Boolean(), nullable=False),
        sa.Column('updated_by', sa.String(length=128), nullable=True),
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('date_created', sa.DateTime(), nullable=False),
        sa.Column('last_updated', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('kpi_target', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_kpi_target_kpi_id'), ['kpi_id'], unique=True)

    op.create_table(
        'kpi_snapshot',
        sa.Column('kpi_id', sa.String(length=64), nullable=False),
        sa.Column('as_of', sa.Date(), nullable=False),
        sa.Column('value', sa.Float(), nullable=True),
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('date_created', sa.DateTime(), nullable=False),
        sa.Column('last_updated', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('kpi_id', 'as_of', name='uq_kpi_snapshot_day'),
    )
    with op.batch_alter_table('kpi_snapshot', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_kpi_snapshot_kpi_id'), ['kpi_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_kpi_snapshot_as_of'), ['as_of'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('kpi_snapshot', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_kpi_snapshot_as_of'))
        batch_op.drop_index(batch_op.f('ix_kpi_snapshot_kpi_id'))
    op.drop_table('kpi_snapshot')

    with op.batch_alter_table('kpi_target', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_kpi_target_kpi_id'))
    op.drop_table('kpi_target')

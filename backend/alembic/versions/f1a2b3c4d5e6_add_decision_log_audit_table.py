"""add decision_log audit table

Append-only audit trail for the autonomous purchasing decision engine. Additive
only — CREATE TABLE plus its indexes; no existing table is touched.

Revision ID: f1a2b3c4d5e6
Revises: db90c46c5938
Create Date: 2026-06-09 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f1a2b3c4d5e6'
down_revision: Union[str, Sequence[str], None] = 'db90c46c5938'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'decision_log',
        sa.Column('run_at', sa.String(length=32), nullable=False),
        sa.Column('dry_run', sa.Boolean(), nullable=False),
        sa.Column('product_id', sa.String(length=36), nullable=False),
        sa.Column('supplier_id', sa.String(length=36), nullable=True),
        sa.Column('qty', sa.Integer(), nullable=False),
        sa.Column('unit_price', sa.Float(), nullable=True),
        sa.Column('total', sa.Float(), nullable=False),
        sa.Column('trigger_type', sa.String(length=48), nullable=True),
        sa.Column('evidence', sa.JSON(), nullable=True),
        sa.Column('tier', sa.String(length=16), nullable=False),
        sa.Column('confidence', sa.Float(), nullable=True),
        sa.Column('rationale', sa.Text(), nullable=True),
        sa.Column('placed_po_id', sa.String(length=36), nullable=True),
        sa.Column('actor', sa.String(length=128), nullable=True),
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('date_created', sa.DateTime(), nullable=False),
        sa.Column('last_updated', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('decision_log', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_decision_log_run_at'), ['run_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_decision_log_product_id'), ['product_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_decision_log_supplier_id'), ['supplier_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_decision_log_tier'), ['tier'], unique=False)
        batch_op.create_index(batch_op.f('ix_decision_log_placed_po_id'), ['placed_po_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('decision_log', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_decision_log_placed_po_id'))
        batch_op.drop_index(batch_op.f('ix_decision_log_tier'))
        batch_op.drop_index(batch_op.f('ix_decision_log_supplier_id'))
        batch_op.drop_index(batch_op.f('ix_decision_log_product_id'))
        batch_op.drop_index(batch_op.f('ix_decision_log_run_at'))

    op.drop_table('decision_log')

"""add contract_document table

Stores uploaded supplier contract files (the actual document bytes live in a
pluggable ContractStore; this table is the metadata + the opaque storage key).
Additive only — CREATE TABLE plus its indexes; no existing table is touched.

Revision ID: c3d5e7a9f024
Revises: b2c4e6a8d013
Create Date: 2026-06-10 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c3d5e7a9f024'
down_revision: Union[str, Sequence[str], None] = 'b2c4e6a8d013'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'contract_document',
        sa.Column('organization_id', sa.String(length=36), nullable=False),
        sa.Column('original_filename', sa.String(length=255), nullable=False),
        sa.Column('content_type', sa.String(length=128), nullable=False),
        sa.Column('size_bytes', sa.Integer(), nullable=False),
        sa.Column('storage_key', sa.String(length=512), nullable=False),
        sa.Column('kind', sa.String(length=32), nullable=True),
        sa.Column('uploaded_at', sa.DateTime(), nullable=False),
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('date_created', sa.DateTime(), nullable=False),
        sa.Column('last_updated', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['organization_id'], ['organization.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('contract_document', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_contract_document_organization_id'),
                              ['organization_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_contract_document_storage_key'),
                              ['storage_key'], unique=True)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('contract_document', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_contract_document_storage_key'))
        batch_op.drop_index(batch_op.f('ix_contract_document_organization_id'))

    op.drop_table('contract_document')

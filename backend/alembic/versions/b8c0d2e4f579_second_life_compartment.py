"""Second-life stock: READY_SECOND on assetstatus

A station was a status, one to one, and the refurbished devices waiting for their
second customer had no place of their own: ST-REFURB is the refurbishment process.
So first-life and second-life stock were mixed in the numbers, which is the one thing
the warehouse owner said must never happen. This adds the compartment. Additive:
one enum value (Postgres: ALTER TYPE ... ADD VALUE; SQLite stores the enum as text).

Revision ID: b8c0d2e4f579
Revises: a7b9c1d3e468
Create Date: 2026-09-23 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'b8c0d2e4f579'
down_revision: Union[str, None] = 'a7b9c1d3e468'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

NEW_STATES = ('READY_SECOND',)


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    if bind.dialect.name == 'postgresql':
        # Postgres 12+ accepts ADD VALUE inside a transaction as long as the new value
        # is not used in the same transaction; this migration only adds.
        for v in NEW_STATES:
            op.execute(f"ALTER TYPE assetstatus ADD VALUE IF NOT EXISTS '{v}'")


def downgrade() -> None:
    """Downgrade schema.

    The enum value stays (Postgres cannot drop one safely). Devices in the compartment
    go back to REFURB, the status the older code used for them, so an older build can
    still read every row instead of failing on a value it does not know.
    """
    op.execute("UPDATE asset SET status = 'REFURB' WHERE status = 'READY_SECOND'")
    op.execute("UPDATE asset_event SET to_status = 'REFURB' WHERE to_status = 'READY_SECOND'")
    op.execute("UPDATE asset_event SET from_status = 'REFURB' WHERE from_status = 'READY_SECOND'")

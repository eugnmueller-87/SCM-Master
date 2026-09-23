"""kpi_snapshot keeps why a KPI could not be measured

A KPI is measured once a day and the measurement is reused for the rest of that day —
31 measurements over a 400,000-device fleet are a minute of database work, and repeating
them on every page load would change no number, because each KPI is defined over a day.

Reusing the value means the reason has to travel with it. Without this column a reused
snapshot could only say "no number", and the tab's promise is the opposite: a KPI that
cannot be measured says what is missing, and never shows a zero instead.

Revision ID: a7b9c1d3e468
Revises: f6a8b0c2d357
Create Date: 2026-09-22 23:55:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'a7b9c1d3e468'
down_revision: Union[str, None] = 'f6a8b0c2d357'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("kpi_snapshot") as batch:
        batch.add_column(sa.Column("reason", sa.String(length=200), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("kpi_snapshot") as batch:
        batch.drop_column("reason")

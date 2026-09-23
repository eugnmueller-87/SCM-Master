"""KPI targets and snapshots.

The KPI *values* are never stored as facts: every value on the KPIs tab is computed
live from the operational tables (assets, orders, requisitions, contracts, costing)
by ``app.services.kpis``. What this module stores is the two things the system
cannot derive on its own:

* ``kpi_target``   — where we want each KPI to be in one, two and three years, and
                     who owns that target. A row seeded by the system is a
                     ``placeholder`` (derived from today's value by a fixed rule) until
                     a person confirms or overwrites it.
* ``kpi_snapshot`` — one value per KPI per day, written whenever the KPIs are read,
                     so the tab can show a trend without inventing history.
"""
from __future__ import annotations

from datetime import date
from typing import Optional

from sqlalchemy import Boolean, Date, Float, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, IdMixin, TimestampMixin


class KpiTarget(IdMixin, TimestampMixin, Base):
    __tablename__ = "kpi_target"

    kpi_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    target_y1: Mapped[Optional[float]] = mapped_column(Float)
    target_y2: Mapped[Optional[float]] = mapped_column(Float)
    target_y3: Mapped[Optional[float]] = mapped_column(Float)
    owner: Mapped[Optional[str]] = mapped_column(String(128))
    note: Mapped[Optional[str]] = mapped_column(Text)
    placeholder: Mapped[bool] = mapped_column(Boolean, default=True)   # system-derived until a person sets it
    updated_by: Mapped[Optional[str]] = mapped_column(String(128))


class KpiSnapshot(IdMixin, TimestampMixin, Base):
    __tablename__ = "kpi_snapshot"
    __table_args__ = (UniqueConstraint("kpi_id", "as_of", name="uq_kpi_snapshot_day"),)

    kpi_id: Mapped[str] = mapped_column(String(64), index=True)
    as_of: Mapped[date] = mapped_column(Date, index=True)
    value: Mapped[Optional[float]] = mapped_column(Float)   # None = not measurable that day
    # Why there is no value. Without it a reused snapshot could only say "no number",
    # and the tab's promise is that an unmeasurable KPI always says what is missing.
    reason: Mapped[Optional[str]] = mapped_column(String(200))

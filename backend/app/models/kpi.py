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
* ``fleet_milestone`` — where the business owner wants the fleet at customers to be
                     on a date. The capacity plan inverts these into a required stock
                     per compartment; a planner asks "and if it were 800,000?" by
                     writing a new figure through the API, never by a deploy.
"""
from __future__ import annotations

from datetime import date
from typing import Optional

from sqlalchemy import Boolean, Date, Float, Integer, String, Text, UniqueConstraint
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


class FleetMilestone(IdMixin, TimestampMixin, Base):
    """A target fleet at customers on a date, with the person or role that owns it.

    Unlike a KPI target, a milestone is dated rather than "in one year": the owner said
    "this year 500,000 and by the end of next year a million", and the plan interpolates
    the fleet between those dates. The two seeded rows are that instruction, not a
    placeholder; ``placeholder`` exists so a system-derived milestone could be marked
    the way a seeded KPI target is.
    """

    __tablename__ = "fleet_milestone"

    milestone_date: Mapped[date] = mapped_column(Date, unique=True, index=True)
    target_fleet: Mapped[int] = mapped_column(Integer)
    owner: Mapped[Optional[str]] = mapped_column(String(128))
    note: Mapped[Optional[str]] = mapped_column(Text)
    placeholder: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_by: Mapped[Optional[str]] = mapped_column(String(128))

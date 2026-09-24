"""The world clock of the demo: every time the simulation let days pass, one row.

The simulation lets time pass by moving every date the fleet carries back by N days;
every read compares stored dates to the real today, so that is the same world as N days
later. This table is the calendar's own event log: one row per move, with how many
days, when in real time, and which event made it. The total is the sum, and every
screen can say that the dataset stands N days later than when it was seeded.

It is a log and not a counter for one reason: a KPI measurement taken before a move
describes a world-day that has since moved back, and whether that measurement still
stands is answered by "how many days did the world move since it was taken", which only
a log can say. A rebuild empties the table with every other operational table, which is
exactly right: a freshly seeded fleet stands at day zero.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, IdMixin, TimestampMixin


class WorldClock(IdMixin, TimestampMixin, Base):
    __tablename__ = "world_clock"

    days: Mapped[int] = mapped_column(Integer)                        # how many days this move let pass
    advanced_at: Mapped[datetime] = mapped_column(DateTime)           # real time of the move
    action: Mapped[str] = mapped_column(String(32))                   # the event that made it

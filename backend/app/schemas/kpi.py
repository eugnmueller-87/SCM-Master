"""KPIs tab: read model per KPI, write model for its targets."""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from pydantic import BaseModel, Field


class KpiPoint(BaseModel):
    as_of: date
    value: Optional[float]


class KpiRead(BaseModel):
    id: str
    group: str
    group_label: str
    name: str
    unit: str
    direction: str
    definition: str
    source: str
    current: Optional[float]
    reason: Optional[str]        # why current is None, when it is
    as_of: date
    measured_at: Optional[datetime]   # when this value was actually measured; a reused day's measurement keeps its time
    measured_on: date                 # the world-day the value describes; before a simulated move it is earlier than as_of
    stale_days: int                   # how many simulated days ago it was measured; 0 for today's measurement
    target_y1: Optional[float]
    target_y2: Optional[float]
    target_y3: Optional[float]
    owner: Optional[str]
    note: Optional[str]
    placeholder: bool
    updated_by: Optional[str]
    status: str                  # met | on_track | behind | open | not_measurable | no_target
    progress_pct: Optional[float]
    gap_to_y1: Optional[float]   # target minus today, in the KPI's unit
    history: list[KpiPoint]


class KpiTargetWrite(BaseModel):
    target_y1: Optional[float] = Field(None, description="target in one year")
    target_y2: Optional[float] = Field(None, description="target in two years")
    target_y3: Optional[float] = Field(None, description="target in three years")
    owner: Optional[str] = Field(None, max_length=128)
    note: Optional[str] = None

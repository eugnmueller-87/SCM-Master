"""KPIs tab: read model per KPI, write model for its targets."""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from pydantic import BaseModel, Field


class KpiPoint(BaseModel):
    as_of: date
    value: Optional[float]


# ---- the goal and where it comes from (services/kpi_goals.py) ----------------------------

class KpiGoalStep(BaseModel):
    date: date
    value: float


class KpiGoalMilestone(BaseModel):
    date: date
    fleet: int
    need: Optional[float]          # the dwell the stations may not exceed at today's capacity
    extra_places: int              # the other lever: places needed at today's dwell
    fits_today: bool


class KpiGoalPlan(BaseModel):
    milestones: list[KpiGoalMilestone]
    capacity_placeholder: bool
    capacity_owner: Optional[str]
    basis: str


class KpiGoalIndustry(BaseModel):
    value: Optional[float]         # the public value in the KPI's unit, where one fits
    goal: Optional[float]          # what a goal may be set to; None where the match is too loose
    match: str                     # how well it fits this KPI's definition
    text: str
    source: str                    # publisher, title, date, URL, retrieval date


class KpiGoalLever(BaseModel):
    text: str
    kind: str                      # contract | process | code | missing


class KpiGoal(BaseModel):
    basis: str                     # plan | industry | owner | set (a person set the target)
    goal: Optional[float]
    unit: str
    horizon: date                  # the last milestone of the fleet plan
    steps: list[KpiGoalStep]       # the goal at each half-year end up to the horizon
    plan: Optional[KpiGoalPlan]
    industry: Optional[KpiGoalIndustry]
    owner: Optional[str]
    levers: list[KpiGoalLever]
    steered_by: Optional[str]      # an older KPI whose station the plan sizes: the plan KPI that carries its goal
    note: Optional[str]


class KpiRead(BaseModel):
    id: str
    group: str
    group_label: str
    name: str
    unit: str
    direction: str
    definition: str
    source: str
    # The explanation, from the same registry entry as the value (services/kpis.py, the
    # @explained block above each compute function). A screen renders it; none writes its own.
    basis: str                   # measured | derived | placeholder: how far the number is a fact of the tables
    calculation: str             # the arithmetic in words: what over what, which window, counted how
    reads: str                   # the tables and columns actually read
    caveats: str                 # what it excludes or assumes, where that changes the reading
    why: str                     # one sentence: what a bad number means
    needs: str                   # what data the measurement needs; for a not-measurable KPI, what would make it so
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
    goal: Optional[KpiGoal] = None   # the goal, its origin, its half-year steps, owner and levers
    history: list[KpiPoint]


class KpiTargetWrite(BaseModel):
    target_y1: Optional[float] = Field(None, description="target in one year")
    target_y2: Optional[float] = Field(None, description="target in two years")
    target_y3: Optional[float] = Field(None, description="target in three years")
    owner: Optional[str] = Field(None, max_length=128)
    note: Optional[str] = None

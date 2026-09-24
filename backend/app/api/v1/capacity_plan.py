"""Capacity plan API (DaaS scenario): how fast each compartment must turn for the fleet
the owner wants, and when it runs out of room.

  GET /capacity-plan                                the plan: model, milestones with one row per compartment, the path, the breach per compartment
  PUT /capacity-plan/milestones/{date}              set a milestone's target fleet, owner and note (PROCUREMENT; ADMIN passes)
  PUT /capacity-plan/compartments/{code}/capacity   set a station's capacity (WAREHOUSE; ADMIN passes)

Any authenticated user may read; a read writes the owner's two milestones once if the
table is empty, the way the KPI read seeds its targets. The datacenter scenario answers
too (scenario "datacenter", no milestones, a reason), so the frontend can branch on one
call. Both writes return the whole recomputed plan, because a change ripples through
every row. Values are computed in services/capacity_plan.py, never typed in.
"""
from __future__ import annotations

from datetime import date
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db, require_role
from app.models.auth import Role, User
from app.services import capacity_plan as svc

router = APIRouter(prefix="/capacity-plan", tags=["capacity-plan"], dependencies=[Depends(get_current_user)])
_proc = require_role(Role.PROCUREMENT)
_whse = require_role(Role.WAREHOUSE)


class PlanModel(BaseModel):
    term_months: Optional[float]
    term_contracts: int
    term_by_cycle: Dict[str, float]
    term_reason: Optional[str]
    cycle2_share: Optional[float]
    returns_per_month_now: Optional[int]
    exit_share: Optional[float]
    exit_share_basis: str
    returns_12m: int
    gone_12m: int
    exit_share_measured_12m: Optional[float]
    exit_share_measured_reason: Optional[str]
    first_rentals_per_month_measured: Optional[int]
    first_rentals_months: int
    first_rentals_reason: Optional[str]
    next_step_share: Dict[str, Dict[str, float]]
    swap_ratio: Optional[float]
    swap_basis: str
    dwell_basis: str
    reason: Optional[str]


class MilestoneRow(BaseModel):
    code: str
    name: str
    step: int
    throughput_per_month: Optional[int]
    throughput_reason: Optional[str]
    dwell_days: Optional[float]
    required_stock: Optional[int]
    capacity: Optional[int]
    gap: Optional[int]
    fits: Optional[bool]
    required_dwell_days: Optional[float]
    required_dwell_reason: Optional[str]
    extra_places: Optional[int]
    reason: Optional[str]


class MilestoneView(BaseModel):
    date: date
    target_fleet: int
    owner: Optional[str]
    note: Optional[str]
    placeholder: bool
    updated_by: Optional[str]
    months_from_today: float
    growth_per_month: int
    returns_per_month: Optional[int]
    exits_per_month: Optional[int]
    placements_per_month: Optional[int]
    second_rentals_per_month: Optional[int]
    extra_places_total: Optional[int]
    compartments_short: int
    rows: List[MilestoneRow]


class PathPoint(BaseModel):
    date: date
    month: str
    fleet: int
    growth_per_month: int
    toward: Optional[date]


class CompartmentPlan(BaseModel):
    code: str
    name: str
    step: int
    stage: str
    holds: str
    on_hand: int
    capacity: Optional[int]
    capacity_placeholder: bool
    capacity_owner: str
    capacity_set_by: Optional[str]
    capacity_set_on: Optional[date]
    capacity_reason: Optional[str]
    dwell_days: Optional[float]
    dwell_median_days: Optional[float]
    dwell_reason: Optional[str]
    flow_basis: str
    share_of_returns: Optional[float]
    required_now: Optional[int]
    required_now_reason: Optional[str]
    over_capacity_today: bool
    breach_state: str            # over_today | now | later | fits | no_capacity | unknown
    breach_month: Optional[str]
    breach_date: Optional[date]
    breach_fleet: Optional[int]
    breach_now: bool
    breach_reason: Optional[str]


class FirstBreach(BaseModel):
    code: str
    name: str
    month: str
    date: date
    over_capacity_today: bool
    breach_now: bool
    state: str


class CapacityPlanView(BaseModel):
    scenario: str
    as_of: date
    fleet_now: Optional[int]
    model: Optional[PlanModel]
    milestones: List[MilestoneView]
    path: List[PathPoint]
    compartments: List[CompartmentPlan]
    first_breach: Optional[FirstBreach]
    reason: Optional[str]


class MilestoneWrite(BaseModel):
    target_fleet: int = Field(..., ge=0, description="devices at customers on that date")
    owner: Optional[str] = Field(None, max_length=128, description="who owns the target; left out, the owner stays")
    note: Optional[str] = None


class CapacityWrite(BaseModel):
    capacity: int = Field(..., ge=0, description="places in the station")


@router.get("", response_model=CapacityPlanView)
def capacity_plan(db: Session = Depends(get_db), _u: User = Depends(get_current_user)):
    out = svc.plan(db)
    db.commit()   # the owner's milestones, seeded once
    return out


@router.put("/milestones/{milestone_date}", response_model=CapacityPlanView, dependencies=[Depends(_proc)])
def set_milestone(milestone_date: date, payload: MilestoneWrite,
                  db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    svc.set_milestone(db, milestone_date, target_fleet=payload.target_fleet, owner=payload.owner, note=payload.note,
                      actor=getattr(user, "email", None))
    db.commit()
    return svc.plan(db)


@router.put("/compartments/{code}/capacity", response_model=CapacityPlanView, dependencies=[Depends(_whse)])
def set_capacity(code: str, payload: CapacityWrite,
                 db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    svc.set_capacity(db, code, capacity=payload.capacity, actor=getattr(user, "email", None))
    db.commit()
    return svc.plan(db)

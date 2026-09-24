"""Simulation API (DaaS scenario): fire the day-to-day of a device fleet at the running system.

  GET  /simulation/actions          the actions, defaults filled from today's measured rates, the choices the forms need
  POST /simulation/actions/{id}     fire one action (WAREHOUSE; ADMIN passes): what moved, what was refused and why, KPIs before and after
  GET  /simulation/status           whether writes are allowed here, and whether a rebuild is running
  POST /simulation/reset            rebuild the demo dataset from scratch in the background (ADMIN only)

Every write is refused in production the way the seeders are (``assert_demo_write_allowed``
answers 403), and role-gated so the public demo's read-only guest cannot fire one. The
datacenter scenario answers the catalogue with a reason and refuses to fire (422).
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app import seed_reset
from app.api.deps import get_current_user, get_db, require_role
from app.core.config import is_production
from app.core.safety import ProductionSafetyError, assert_demo_write_allowed
from app.models.auth import Role, User
from app.services import simulation as svc
from app.services import timeshift

router = APIRouter(prefix="/simulation", tags=["simulation"], dependencies=[Depends(get_current_user)])
_ops = require_role(Role.WAREHOUSE)
_admin = require_role(Role.ADMIN)


def _demo_only() -> None:
    """The forge-lock, as an HTTP answer: a simulated event in production is refused at the door."""
    try:
        assert_demo_write_allowed("a simulated event")
    except ProductionSafetyError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))


class FireRequest(BaseModel):
    units: Optional[int] = Field(None, ge=1, le=svc.MAX_UNITS, description="devices this event touches; left out, the measured rate")
    product_code: Optional[str] = Field(None, max_length=64)
    station: Optional[str] = Field(None, max_length=16, description="compartment code for a hold")
    days: Optional[int] = Field(None, ge=1, le=svc.MAX_DAYS)
    factor: Optional[float] = Field(None, ge=0.0, le=1.0, description="outflow kept during a hold: 0 stopped, 0.5 halved")
    from_status: Optional[str] = Field(None, max_length=32)
    to_status: Optional[str] = Field(None, max_length=32)


class Move(BaseModel):
    from_status: str
    to_status: str
    from_name: str
    to_name: str
    units: int


class Refusal(BaseModel):
    what: str
    reason: str
    units: int


class KpiDelta(BaseModel):
    id: str
    name: str
    unit: str
    direction: str
    before: Optional[float]
    after: Optional[float]
    delta: Optional[float]
    better: Optional[bool]
    before_reason: Optional[str]
    after_reason: Optional[str]
    before_measured_at: Optional[datetime]
    after_measured_at: Optional[datetime]
    after_measured_on: date          # the world-day the "after" value describes
    after_stale_days: int            # simulated days since it was measured; 0 when this event measured it


class CompartmentDelta(BaseModel):
    code: str
    name: str
    step: int
    capacity: Optional[int]
    on_hand_before: int
    on_hand_after: int
    delta: int
    over_capacity_before: bool
    over_capacity_after: bool
    past_target_before: Optional[int]
    past_target_after: Optional[int]
    target_dwell_days: int
    median_before: Optional[float]
    median_after: Optional[float]
    verdict_before: Optional[str]
    verdict_after: Optional[str]
    breach_before: Optional[str]
    breach_after: Optional[str]
    breach_month_before: Optional[str]
    breach_month_after: Optional[str]


class Answer(BaseModel):
    action: str
    name: str
    requested: Dict[str, Any]
    as_of: date
    ran_at: datetime
    actor: str
    moved: List[Move]
    moved_total: int
    refused: List[Refusal]
    refused_total: int
    notes: List[str]
    rates: Optional[Dict[str, Any]]
    hold: Optional[Dict[str, Any]]
    world: Dict[str, Any]           # advanced_days by this event, and the clock: days_advanced since the seed, advanced_at, last_action
    proceeds_eur: Optional[float]
    compartments: List[CompartmentDelta]
    warehouse: Dict[str, Any]
    plan: Dict[str, Any]
    kpis: List[KpiDelta]
    kpis_measured: List[str]         # re-measured by this event
    kpis_kept: List[Dict[str, Any]]  # deliberately not, with the world-day each was measured on
    kpis_kept_reason: str
    timing_ms: Dict[str, int]
    calendar_note: str


class RebuildState(BaseModel):
    running: bool
    started_at: Optional[datetime]
    finished_at: Optional[datetime]
    ok: Optional[bool]
    detail: Optional[str]


class WorldState(BaseModel):
    days_advanced: int
    advanced_at: Optional[datetime]
    last_action: Optional[str]
    last_days: Optional[int]


class Status(BaseModel):
    production: bool
    writes_allowed: bool
    can_fire: bool
    can_reset: bool
    rebuild: RebuildState
    world: WorldState


@router.get("/actions", response_model=Dict[str, Any])
def simulation_actions(db: Session = Depends(get_db), _u: User = Depends(get_current_user)):
    return svc.catalogue(db)


@router.post("/actions/{action_id}", response_model=Answer, dependencies=[Depends(_ops), Depends(_demo_only)])
def fire_action(action_id: str, payload: FireRequest, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    if seed_reset.rebuild_state()["running"]:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="the dataset is being rebuilt; wait for it to finish")
    out = svc.fire(db, action_id, payload.model_dump(exclude_none=True), actor=f"simulation ({getattr(user, 'email', None) or 'api'})")
    db.commit()   # the moves, the contracts and invoices they wrote, and the fresh KPI measurement
    return out


@router.get("/status", response_model=Status)
def simulation_status(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    role = getattr(user, "role", None)
    prod = is_production()
    return {
        "production": prod, "writes_allowed": not prod,
        "can_fire": (not prod) and role in (Role.ADMIN, Role.WAREHOUSE),
        "can_reset": (not prod) and role == Role.ADMIN,
        "rebuild": seed_reset.rebuild_state(),
        "world": timeshift.state(db),
    }


@router.post("/reset", response_model=RebuildState, status_code=status.HTTP_202_ACCEPTED, dependencies=[Depends(_admin), Depends(_demo_only)])
def reset_dataset(_u: User = Depends(get_current_user)):
    try:
        return seed_reset.rebuild_in_background()
    except ProductionSafetyError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))

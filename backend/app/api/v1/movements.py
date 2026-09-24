"""Movement log API (DaaS scenario): which compartment a device left, which it entered, when, how long it stayed.

  GET /movements?days=30            the moves of the window by pair of compartments, and per compartment the measured
                                    flow and finished stay next to what the Warehouse tab derives
  GET /movements/serials/{serial}   one device's path through the chain, with the days in each station

Read-only, any authenticated user. Values are computed in services/movements.py; every flow and dwell here
is measured from logged moves and says so, and a figure the log cannot give is None with a reason.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.models.auth import User
from app.services import movements as svc

router = APIRouter(prefix="/movements", tags=["movements"], dependencies=[Depends(get_current_user)])


class PairRow(BaseModel):
    from_status: Optional[str]
    to_status: str
    from_code: Optional[str]
    to_code: Optional[str]
    from_name: str
    to_name: str
    units: int
    dated_units: int
    unknown_units: int
    per_week: float
    median_days: Optional[float]
    p90_days: Optional[float]
    mean_days: Optional[float]
    max_days: Optional[int]
    dwell_reason: Optional[str]


class CompartmentFlow(BaseModel):
    code: str
    name: str
    step: int
    stage: str
    units_in: int
    units_out: int
    in_per_week: float
    out_per_week: float
    flow_basis: str
    dated_out: int
    unknown_out: int
    median_days: Optional[float]
    p90_days: Optional[float]
    mean_days: Optional[float]
    max_days: Optional[int]
    dwell_basis: str
    dwell_reason: Optional[str]
    target_dwell_days: int
    on_hand: Optional[int]
    derived_units_per_week: Optional[float]
    derived_mean_days: Optional[float]
    derived_basis: str


class WindowView(BaseModel):
    as_of: date
    days: int
    since: date
    covered_days: int
    coverage_basis: str
    moves: int
    devices: int
    pairs_count: int
    undated_events: int
    undated_reason: Optional[str]
    first_move: Optional[date]
    last_move: Optional[date]
    history_note: str
    reason: Optional[str]
    pairs: List[PairRow]
    compartments: List[CompartmentFlow]


class PathStep(BaseModel):
    kind: str
    from_status: Optional[str]
    to_status: Optional[str]
    from_name: Optional[str]
    to_name: Optional[str]
    from_code: Optional[str]
    to_code: Optional[str]
    effective_date: Optional[date]
    from_since: Optional[date]
    dwell_days: Optional[int]
    actor: Optional[str]
    note: Optional[str]
    logged_at: datetime


class PathView(BaseModel):
    serial_number: str
    asset_id: str
    product: str
    family: Optional[str]
    cycle_no: int
    grade: Optional[str]
    status: str
    station_name: str
    station_code: Optional[str]
    location_code: Optional[str]
    since: Optional[date]
    days_so_far: Optional[int]
    steps: List[PathStep]
    moves_logged: int
    warehouse_days_measured: int
    customer_days_measured: int
    unknown_stays: int
    history_reason: Optional[str]
    as_of: date


@router.get("", response_model=WindowView)
def movements_window(days: int = Query(svc.DEFAULT_DAYS, ge=1, le=svc.MAX_DAYS),
                     db: Session = Depends(get_db), _u: User = Depends(get_current_user)):
    return svc.window(db, days=days)


@router.get("/serials/{serial}", response_model=PathView)
def movements_path(serial: str, db: Session = Depends(get_db), _u: User = Depends(get_current_user)):
    return svc.path(db, serial)

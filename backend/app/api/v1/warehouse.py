"""Warehouse API (DaaS scenario): the compartments, how full, how fast, how well.

  GET /warehouse/compartments                  one row per compartment in chain order, plus the rollup
  GET /warehouse/compartments/{code}/offenders the late stock of one compartment by device, and its oldest units

Read-only, any authenticated user. The datacenter scenario answers too (scenario
"datacenter", no compartments, a reason), so the frontend can branch on one call.
"""
from __future__ import annotations

from datetime import date
from typing import List, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.models.auth import User
from app.services import warehouse as svc

router = APIRouter(prefix="/warehouse", tags=["warehouse"], dependencies=[Depends(get_current_user)])


class CompartmentRow(BaseModel):
    code: str
    name: str
    holds: str
    stage: str
    step: int
    statuses: List[str]
    on_hand: int
    capacity: Optional[int]
    free: Optional[int]
    overflow: int
    utilisation: Optional[float]
    over_capacity: bool
    capacity_reason: Optional[str]
    target_dwell_days: int
    target_placeholder: bool
    target_owner: str
    median_days: Optional[float]
    p90_days: Optional[float]
    mean_days: Optional[float]
    oldest_days: Optional[int]
    past_target_units: Optional[int]
    past_target_share: Optional[float]
    undated_units: int
    dwell_reason: Optional[str]
    units_per_week: Optional[float]
    turns_per_year: Optional[float]
    throughput_derived: bool
    throughput_basis: str
    throughput_reason: Optional[str]
    verdict: Optional[str]
    verdict_reason: Optional[str]


class WarehouseView(BaseModel):
    scenario: str
    as_of: date
    capacity: Optional[int]
    on_hand: Optional[int]
    free: Optional[int]
    utilisation: Optional[float]
    over_capacity: int
    chain: List[str]
    compartments: List[CompartmentRow]
    reason: Optional[str]


class LateProduct(BaseModel):
    name: str
    family: Optional[str]
    units: int


class OldUnit(BaseModel):
    asset_id: str
    serial_number: str
    product: str
    family: Optional[str]
    grade: Optional[str]
    cycle_no: int
    status: str
    since: date
    days: int


class Offenders(BaseModel):
    code: str
    name: str
    target_dwell_days: int
    as_of: date
    past_target_by_product: List[LateProduct]
    oldest: List[OldUnit]


@router.get("/compartments", response_model=WarehouseView)
def warehouse_compartments(db: Session = Depends(get_db), _u: User = Depends(get_current_user)):
    return svc.compartments(db)


@router.get("/compartments/{code}/offenders", response_model=Offenders)
def warehouse_offenders(code: str, limit: int = Query(svc.OFFENDER_LIMIT, ge=1, le=500),
                        db: Session = Depends(get_db), _u: User = Depends(get_current_user)):
    return svc.offenders(db, code, limit=limit)

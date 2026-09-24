"""Warehouse API (DaaS scenario): the compartments, how full, how fast, how well.

  GET /warehouse/compartments                  one row per compartment in chain order, plus the rollup
  GET /warehouse/compartments/{code}/offenders the late stock of one compartment by device, and its oldest units
  GET /warehouse/compartments/{code}/contents  what is inside one compartment: by model and class, how old against
                                               the target, what condition, the oldest serials, what is on its way in

Read-only, any authenticated user. The datacenter scenario answers too (scenario
"datacenter", no compartments, a reason), so the frontend can branch on one call.

The contents payload carries every label, unit and basis a second screen needs; the
analytics cockpit renders it without a database of its own.
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


class ClassRow(BaseModel):
    key: str
    label: str
    units: int
    share: Optional[float]
    models: int


class ModelRow(BaseModel):
    product_id: str
    name: str
    family: Optional[str]
    units: int
    share: Optional[float]
    past_target_units: int
    far_past_units: int
    oldest_days: Optional[int]
    undated_units: int


class AgeBand(BaseModel):
    key: str
    label: str
    from_days: int
    to_days: Optional[int]
    units: int
    share: Optional[float]


class AgeView(BaseModel):
    target_dwell_days: int
    far_past_from_days: int
    basis: str
    dated_units: int
    undated_units: int
    bands: List[AgeBand]
    median_days: Optional[float]
    p90_days: Optional[float]
    mean_days: Optional[float]
    oldest_days: Optional[int]
    past_target_units: int
    past_target_share: Optional[float]
    far_past_units: int
    far_past_share: Optional[float]


class GradeRow(BaseModel):
    grade: str
    label: str
    units: int
    share: Optional[float]


class CycleRow(BaseModel):
    cycle: str
    label: str
    units: int
    share: Optional[float]


class ConditionView(BaseModel):
    grades: Optional[List[GradeRow]]
    graded_units: int
    ungraded_units: int
    ungraded_share: Optional[float]
    grade_basis: str
    grade_reason: Optional[str]
    cycles: List[CycleRow]
    battery_health_mean: Optional[float]
    battery_health_units: int
    battery_reason: Optional[str]


class InboundLine(BaseModel):
    order_number: str
    order_status: str
    order_item_id: str
    product_id: str
    product: str
    family: Optional[str]
    ordered: int
    received: int
    outstanding: int
    eta: Optional[date]
    days_to_eta: Optional[int]
    late: Optional[bool]
    days_late: Optional[int]
    eta_reason: Optional[str]


class InboundModel(BaseModel):
    product_id: str
    name: str
    family: Optional[str]
    units: int
    share: Optional[float]
    lines: int
    late_units: int
    next_eta: Optional[date]


class InboundView(BaseModel):
    station: Optional[str]
    basis: str
    units: Optional[int]
    lines: List[InboundLine]
    by_model: List[InboundModel]
    late_units: Optional[int]
    late_lines: Optional[int]
    next_eta: Optional[date]
    last_eta: Optional[date]
    committed: Optional[int]
    committed_share: Optional[float]
    inbound_share: Optional[float]
    reason: Optional[str]


class CompartmentContents(BaseModel):
    code: str
    name: str
    holds: str
    stage: str
    step: int
    statuses: List[str]
    as_of: date
    unit: str
    target_dwell_days: int
    target_placeholder: bool
    target_owner: str
    on_hand: int
    undated_units: int
    capacity: Optional[int]
    free: Optional[int]
    overflow: int
    utilisation: Optional[float]
    over_capacity: bool
    capacity_reason: Optional[str]
    by_class: List[ClassRow]
    by_model: List[ModelRow]
    age: Optional[AgeView]
    age_reason: Optional[str]
    condition: Optional[ConditionView]
    condition_reason: Optional[str]
    oldest: List[OldUnit]
    inbound: InboundView


@router.get("/compartments", response_model=WarehouseView)
def warehouse_compartments(db: Session = Depends(get_db), _u: User = Depends(get_current_user)):
    return svc.compartments(db)


@router.get("/compartments/{code}/offenders", response_model=Offenders)
def warehouse_offenders(code: str, limit: int = Query(svc.OFFENDER_LIMIT, ge=1, le=500),
                        db: Session = Depends(get_db), _u: User = Depends(get_current_user)):
    return svc.offenders(db, code, limit=limit)


@router.get("/compartments/{code}/contents", response_model=CompartmentContents)
def warehouse_contents(code: str, oldest: int = Query(svc.CONTENTS_OLDEST_LIMIT, ge=1, le=100),
                       db: Session = Depends(get_db), _u: User = Depends(get_current_user)):
    return svc.contents(db, code, oldest_limit=oldest)

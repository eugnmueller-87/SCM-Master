"""Fleet API (DaaS scenario): where the devices are and what comes back when.

  GET /fleet/summary            — scenario, counts at customer / in warehouse per station, returns due, resale last 12 months
  GET /fleet/returns/calendar   — per month: planned contract ends by cycle and the expected next step
  GET /fleet/returns/upcoming   — the contracts ending in the next N days, one row each
  GET /fleet/customers          — every customer with the devices it holds now, most first, and its returns by month
  GET /fleet/customers/{code}   — one customer: devices by model, returns by month, contracts ending in 90 days
  GET /fleet/inflow             — per month, what comes back from customers beside what open purchase orders bring, and defects

Read-only, any authenticated user. The datacenter scenario answers too (scenario
"datacenter", empty fleet figures), so the frontend can branch on one call.
"""
from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.models.auth import User
from app.services import fleet as svc
from app.services import rented_fleet

router = APIRouter(prefix="/fleet", tags=["fleet"], dependencies=[Depends(get_current_user)])


class CalendarMonth(BaseModel):
    month: str
    from_cycle1: int
    from_cycle2: int
    total: int
    second_rental: int
    repair: int
    sale: int
    recycling: int
    by_family: Dict[str, int]


class UpcomingReturn(BaseModel):
    contract_id: str
    asset_id: str
    serial_number: str
    product: str
    family: Optional[str]
    customer: str
    cycle_no: int
    start_date: date
    term_months: int
    planned_end: date
    overdue: bool
    age_months: Optional[float]
    expected_next: str


@router.get("/summary", response_model=Dict[str, Any])
def fleet_summary(db: Session = Depends(get_db), _u: User = Depends(get_current_user)):
    return svc.summary(db)


@router.get("/returns/calendar", response_model=List[CalendarMonth])
def returns_calendar(months: int = Query(24, ge=1, le=60), db: Session = Depends(get_db), _u: User = Depends(get_current_user)):
    return svc.return_calendar(db, months=months)


@router.get("/returns/upcoming", response_model=List[UpcomingReturn])
def returns_upcoming(days: int = Query(30, ge=1, le=365), limit: int = Query(200, ge=1, le=2000),
                     db: Session = Depends(get_db), _u: User = Depends(get_current_user)):
    return svc.upcoming_returns(db, days=days, limit=limit)


@router.get("/customers", response_model=Dict[str, Any])
def fleet_customers(db: Session = Depends(get_db), _u: User = Depends(get_current_user)):
    return rented_fleet.customers(db)


@router.get("/customers/{code}", response_model=Dict[str, Any])
def fleet_customer(code: str, limit: int = Query(rented_fleet.DETAIL_LIMIT, ge=1, le=5000),
                   db: Session = Depends(get_db), _u: User = Depends(get_current_user)):
    return rented_fleet.customer_detail(db, code, limit=limit)


@router.get("/inflow", response_model=Dict[str, Any])
def fleet_inflow(db: Session = Depends(get_db), _u: User = Depends(get_current_user)):
    return rented_fleet.overview(db)

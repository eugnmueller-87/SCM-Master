"""Ordering mask API (DaaS scenario): what is needed and why, what we already own that could serve it,
what is coming in, and the consequence of a quantity before anything is ordered.

  GET /order-mask/scopes                                         the models, manufacturers and classes the mask opens for
  GET /order-mask?product_code=&manufacturer=&family=&quantity=  the mask for a scope; a quantity adds the what-if

Read-only, any authenticated user. Nothing here places an order: the purchasing gate stays on the requisition
path. Values are computed in services/order_mask.py; the nested blocks are carried as they are computed, with
their bases, so the cockpit can render them without a model of its own.
"""
from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.models.auth import User
from app.services import order_mask as svc

router = APIRouter(prefix="/order-mask", tags=["order-mask"], dependencies=[Depends(get_current_user)])


class ScopeProduct(BaseModel):
    product_id: str
    code: str
    name: str
    family: Optional[str]
    manufacturer: Optional[str]
    manufacturer_code: Optional[str]


class Scopes(BaseModel):
    scenario: str
    products: List[ScopeProduct]
    manufacturers: List[str]
    families: List[str]


class Factor(BaseModel):
    key: str
    label: str
    sign: str
    value: float
    basis: str


class ProductRow(BaseModel):
    product_id: str
    code: str
    name: str
    family: Optional[str]
    manufacturer: Optional[str]
    manufacturer_code: Optional[str]
    usage: float
    eol: int
    gross: int
    rate_per_day: float
    method: Optional[str]
    buffer: int
    service_level: Optional[float]
    abc_class: Optional[str]
    new: int
    second_life: int
    inbound: int
    staged: int
    need: int
    gap: int
    moq: int
    recommended: int
    lead_time_days: int
    unit_price: Optional[float]
    order_by: Optional[date]
    forecast_recommended: int
    demand_reason: Optional[str]
    buffer_reason: Optional[str]
    source_reason: Optional[str]


class Recommendation(BaseModel):
    gross: int
    usage: float
    eol: int
    buffer: int
    new: int
    second_life: int
    inbound: int
    staged: int
    need: int
    gap: int
    recommended: int
    forecast_recommended: int
    rate_per_day: float
    lead_time_days: int
    order_by: Optional[date]
    horizon_days: int
    factors: List[Factor]
    gap_basis: str
    recommended_basis: str
    guard: Dict[str, Any]
    guard_for: str
    orderable_now: int
    deferred: int
    not_counted: Dict[str, int]
    cover_days_now: Optional[float]
    products: List[ProductRow]


class Mask(BaseModel):
    scenario: str
    as_of: date
    scope: Optional[Dict[str, Any]]
    demand: Optional[Dict[str, Any]]
    owned: Optional[Dict[str, Any]]
    inbound: Optional[Dict[str, Any]]
    recommendation: Optional[Recommendation]
    what_if: Optional[Dict[str, Any]]
    reason: Optional[str]
    timing_ms: Dict[str, int]


@router.get("/scopes", response_model=Scopes)
def order_mask_scopes(db: Session = Depends(get_db), _u: User = Depends(get_current_user)):
    return svc.scopes(db)


@router.get("", response_model=Mask)
def order_mask(product_code: Optional[str] = Query(None, max_length=64),
               manufacturer: Optional[str] = Query(None, max_length=128),
               family: Optional[str] = Query(None, max_length=128),
               quantity: Optional[int] = Query(None, ge=0, le=10_000_000, description="a what-if quantity; left out, the mask answers for its own recommendation"),
               db: Session = Depends(get_db), _u: User = Depends(get_current_user)):
    return svc.mask(db, product_code=product_code, manufacturer=manufacturer, family=family, quantity=quantity)

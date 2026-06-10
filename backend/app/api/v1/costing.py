"""Should-cost / clean-sheet costing routes.

Reads (should-cost, gap, sensitivity, analytics) are open to any authenticated
user; writes (commodity catalog, BOM/params edits) are PROCUREMENT-gated. The
cost math is in services/costing.py (pure); this router only adapts HTTP ↔ the
costing_service DB layer. Errors map centrally via ServiceError → HTTP.
"""
from __future__ import annotations

from datetime import date
from typing import List, Optional

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db, require_role
from app.models.auth import Role, User
from app.schemas.costing import (
    BOMIn,
    BOMRead,
    CommodityCreate,
    CommodityPriceCreate,
    CommodityPriceRead,
    CommodityRead,
    CostGapResult,
    SavingsSummary,
    SensitivityResult,
    ShouldCostResult,
    SupplierGapRow,
)
from app.services import costing_service as svc

router = APIRouter(tags=["costing"], dependencies=[Depends(get_current_user)])

_buyer = require_role(Role.PROCUREMENT)


def _today() -> date:
    # Deterministic "now" matching the rest of the demo's fixed clock.
    return date(2026, 6, 1)


# --- commodity catalog -----------------------------------------------------

@router.get("/commodities", response_model=List[CommodityRead])
def list_commodities(db: Session = Depends(get_db), _u: User = Depends(get_current_user)):
    return svc.list_commodities(db)


@router.post("/commodities", response_model=CommodityRead, status_code=status.HTTP_201_CREATED)
def create_commodity(payload: CommodityCreate, db: Session = Depends(get_db),
                     _b: User = Depends(_buyer)):
    return svc.create_commodity(db, payload)


@router.post("/commodities/{commodity_id}/prices", response_model=CommodityPriceRead,
             status_code=status.HTTP_201_CREATED)
def add_commodity_price(commodity_id: str, payload: CommodityPriceCreate,
                        db: Session = Depends(get_db), _b: User = Depends(_buyer)):
    return svc.add_commodity_price(db, commodity_id, payload)


# --- BOM -------------------------------------------------------------------

@router.get("/products/{product_id}/bom", response_model=BOMRead)
def get_bom(product_id: str, db: Session = Depends(get_db), _u: User = Depends(get_current_user)):
    return svc.get_bom(db, product_id)


@router.put("/products/{product_id}/bom", response_model=BOMRead)
def put_bom(product_id: str, payload: BOMIn, db: Session = Depends(get_db),
            _b: User = Depends(_buyer)):
    return svc.upsert_bom(db, product_id, payload)


# --- computed views --------------------------------------------------------

@router.post("/products/{product_id}/should-cost", response_model=ShouldCostResult)
def should_cost(product_id: str,
                as_of: Optional[date] = Query(None),
                persist: bool = Query(False),
                db: Session = Depends(get_db), _u: User = Depends(get_current_user)):
    return svc.compute_should_cost(db, product_id, as_of or _today(), persist=persist)


@router.get("/products/{product_id}/cost-gap", response_model=CostGapResult)
def cost_gap(product_id: str,
             as_of: Optional[date] = Query(None),
             annual_volume: int = Query(0, ge=0),
             db: Session = Depends(get_db), _u: User = Depends(get_current_user)):
    return svc.cost_gap(db, product_id, as_of or _today(), annual_volume)


@router.get("/products/{product_id}/sensitivity", response_model=SensitivityResult)
def sensitivity(product_id: str,
                delta: float = Query(0.2, gt=0, lt=1),
                as_of: Optional[date] = Query(None),
                db: Session = Depends(get_db), _u: User = Depends(get_current_user)):
    return svc.sensitivity(db, product_id, as_of or _today(), delta)


# --- analytics (for Power BI) ----------------------------------------------

@router.get("/analytics/should-cost/by-supplier", response_model=List[SupplierGapRow])
def should_cost_by_supplier(as_of: Optional[date] = Query(None),
                            db: Session = Depends(get_db), _u: User = Depends(get_current_user)):
    return svc.gap_by_supplier(db, as_of or _today())


@router.get("/analytics/should-cost/savings", response_model=SavingsSummary)
def should_cost_savings(as_of: Optional[date] = Query(None),
                        db: Session = Depends(get_db), _u: User = Depends(get_current_user)):
    return svc.savings_summary(db, as_of or _today())

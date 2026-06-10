"""Sourcing suggestions and spend analytics routes."""
from __future__ import annotations

from typing import Annotated, List

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.schemas.sourcing import (
    CategorySpend,
    ProductSpend,
    SourceSuggestion,
    SpendSummary,
    SupplierSpend,
)
from app.services import analytics, sourcing

router = APIRouter(tags=["sourcing"], dependencies=[Depends(get_current_user)])


@router.get("/products/{product_id}/sources", response_model=List[SourceSuggestion])
def product_source_suggestions(product_id: str, include_inactive: bool = False,
                               db: Session = Depends(get_db)):
    """Ranked sourcing options for a product (best first)."""
    return sourcing.suggest_sources(db, product_id, include_inactive=include_inactive)


# All spend reads accept an optional ``year`` (calendar year of asset receipt).
# Omit it for the all-time total; pass e.g. ``?year=2026`` to scope to one year.
# Valid years come from ``/analytics/spend/years``.
_Year = Annotated[int | None, Query(ge=2000, le=2100, description="Filter to assets received in this calendar year")]


@router.get("/analytics/spend/years", response_model=List[int])
def spend_years(db: Session = Depends(get_db)):
    """Calendar years that have spend data, newest first — drives the year selector."""
    return analytics.spend_years(db)


@router.get("/analytics/spend", response_model=SpendSummary)
def spend_summary(year: _Year = None, db: Session = Depends(get_db)):
    return analytics.spend_summary(db, year)


@router.get("/analytics/spend/by-supplier", response_model=List[SupplierSpend])
def spend_by_supplier(year: _Year = None, db: Session = Depends(get_db)):
    return analytics.spend_by_supplier(db, year)


@router.get("/analytics/spend/by-product", response_model=List[ProductSpend])
def spend_by_product(year: _Year = None, db: Session = Depends(get_db)):
    return analytics.spend_by_product(db, year)


@router.get("/analytics/spend/by-category", response_model=List[CategorySpend])
def spend_by_category(year: _Year = None, db: Session = Depends(get_db)):
    return analytics.spend_by_category(db, year)

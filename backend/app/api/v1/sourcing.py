"""Sourcing suggestions and spend analytics routes."""
from __future__ import annotations

from typing import List

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.schemas.sourcing import (
    CategorySpend, ProductSpend, SourceSuggestion, SpendSummary, SupplierSpend,
)
from app.services import analytics, sourcing

router = APIRouter(tags=["sourcing"])


@router.get("/products/{product_id}/sources", response_model=List[SourceSuggestion])
def product_source_suggestions(product_id: str, include_inactive: bool = False,
                               db: Session = Depends(get_db)):
    """Ranked sourcing options for a product (best first)."""
    return sourcing.suggest_sources(db, product_id, include_inactive=include_inactive)


@router.get("/analytics/spend", response_model=SpendSummary)
def spend_summary(db: Session = Depends(get_db)):
    return analytics.spend_summary(db)


@router.get("/analytics/spend/by-supplier", response_model=List[SupplierSpend])
def spend_by_supplier(db: Session = Depends(get_db)):
    return analytics.spend_by_supplier(db)


@router.get("/analytics/spend/by-product", response_model=List[ProductSpend])
def spend_by_product(db: Session = Depends(get_db)):
    return analytics.spend_by_product(db)


@router.get("/analytics/spend/by-category", response_model=List[CategorySpend])
def spend_by_category(db: Session = Depends(get_db)):
    return analytics.spend_by_category(db)

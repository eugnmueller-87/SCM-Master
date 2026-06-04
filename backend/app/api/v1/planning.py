"""Capacity & flow planning routes (read-only)."""
from __future__ import annotations

from typing import List

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.schemas.planning import (
    DeploymentForecast,
    InboundLine,
    InventoryItem,
    LocationCapacity,
)
from app.services import planning

router = APIRouter(tags=["planning"], prefix="/planning")


@router.get("/inbound", response_model=List[InboundLine])
def inbound_pipeline(db: Session = Depends(get_db)):
    """Open order lines with quantity still expected to arrive."""
    return planning.inbound_pipeline(db)


@router.get("/capacity", response_model=List[LocationCapacity])
def location_capacity(db: Session = Depends(get_db)):
    """Per-location occupancy vs capacity."""
    return planning.location_capacity(db)


@router.get("/forecast", response_model=DeploymentForecast)
def deployment_forecast(db: Session = Depends(get_db)):
    """On-hand + inbound units that could reach service."""
    return planning.deployment_forecast(db)


@router.get("/inventory", response_model=List[InventoryItem])
def inventory(db: Session = Depends(get_db)):
    """Per-product stock + reorder inputs (reorder math is client-side)."""
    return planning.inventory_plan(db)

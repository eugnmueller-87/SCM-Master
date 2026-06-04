"""Capacity & flow planning routes (reads + the one-click capacity rebalance)."""
from __future__ import annotations

from typing import List

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_role
from app.models.auth import Role, User
from app.schemas.planning import (
    DemandForecastItem,
    DeploymentForecast,
    InboundLine,
    InventoryItem,
    LocationCapacity,
    RebalanceResult,
)
from app.services import planning

router = APIRouter(tags=["planning"], prefix="/planning")

# Moving stock is warehouse/datacenter work.
_ops = require_role(Role.WAREHOUSE, Role.DATACENTER)


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


@router.get("/demand", response_model=List[DemandForecastItem])
def demand(db: Session = Depends(get_db)):
    """Forward demand forecast per product — recency-weighted usage rate +
    end-of-life replacement projected over the horizon, vs on-hand + inbound."""
    return planning.demand_forecast(db)


@router.post("/capacity/{location_id}/rebalance", response_model=RebalanceResult)
def rebalance(location_id: str, db: Session = Depends(get_db),
              user: User = Depends(_ops)):
    """One-click fix for an over-capacity location: move the overflow to the
    best-fit same-type location(s). The correct response to over-capacity is to
    redistribute, not to buy."""
    return planning.rebalance_location(db, location_id, actor=user.email)

"""Capacity & flow planning routes (reads + the one-click capacity rebalance)."""
from __future__ import annotations

from typing import List

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.agent import copilot
from app.agent.copilot import AgentError
from app.agent.schemas import DemandReasoningResult
from app.api.deps import get_current_user, get_db, require_role
from app.models.auth import Role, User
from app.schemas.planning import (
    CapacityDiagnosis,
    CapacityFlow,
    DemandForecastItem,
    DeploymentForecast,
    InboundLine,
    InventoryItem,
    InventoryPositionRow,
    LocationCapacity,
    RebalanceResult,
    StorageHeadroom,
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


@router.get("/capacity/diagnosis", response_model=List[CapacityDiagnosis])
def capacity_diagnosis(db: Session = Depends(get_db)):
    """For locations approaching/over capacity: what's filling them (by product,
    source PO, status), any inbound that will worsen it, and the RIGHT fix —
    rebalance or hold inbound. Over-capacity is a placement problem, never a buy."""
    return planning.capacity_diagnosis(db)


@router.get("/storage-headroom", response_model=StorageHeadroom)
def storage_headroom(db: Session = Depends(get_db)):
    """Max units we could still land (free warehouse space net of inbound) — the
    cap on any order, so we never buy more than we can store."""
    return planning.storage_headroom(db)


@router.get("/capacity-flow", response_model=CapacityFlow)
def capacity_flow(db: Session = Depends(get_db)):
    """One capacity-vs-flow metric: warehouse capacity, committed (on-hand+inbound),
    free-to-order, daily in/out flow, weeks-of-cover and days-to-depletion. The
    single source of truth the order UI, the over-order guard, and the cockpit tile
    all read."""
    return planning.capacity_flow(db)


@router.get("/forecast", response_model=DeploymentForecast)
def deployment_forecast(db: Session = Depends(get_db)):
    """On-hand + inbound units that could reach service."""
    return planning.deployment_forecast(db)


@router.get("/inventory", response_model=List[InventoryItem])
def inventory(db: Session = Depends(get_db)):
    """Per-product stock + reorder inputs (reorder math is client-side)."""
    return planning.inventory_plan(db)


@router.get("/inventory-position", response_model=List[InventoryPositionRow])
def inventory_position(db: Session = Depends(get_db), period_days: int = 7):
    """The canonical MRP position per product — the SAME model the agent nets
    against (planning.inventory_position with the agent's trigger demand folded
    in), so the Overview panel and the agent's proposals can't disagree. Includes
    the open-PO drill-down behind each product's on_order.
    """
    from app.agent.purchasing import trigger_extra_demand
    extra = trigger_extra_demand(db, period_days=period_days)
    rows = planning.inventory_position(db, period_days=period_days, extra_demand=extra)
    po_by_product = planning.open_po_lines_by_product(db)
    out = []
    for r in rows:
        d = r.__dict__.copy()
        d["po_lines"] = po_by_product.get(r.product_id, [])
        out.append(d)
    return out


@router.get("/demand", response_model=List[DemandForecastItem])
def demand(db: Session = Depends(get_db)):
    """Forward demand forecast per product — recency-weighted usage rate +
    end-of-life replacement projected over the horizon, vs on-hand + inbound.
    Deterministic and fast (no LLM) — this is the real-time monitoring read."""
    return planning.demand_forecast(db)


@router.post("/demand/reason", response_model=DemandReasoningResult)
def demand_reason(db: Session = Depends(get_db), _user: User = Depends(get_current_user)):
    """AI reasoning over the live demand forecast: per product it adjusts the
    recommendation and flags risks the arithmetic misses (expiring contract,
    single source, overdue inbound, no capacity). On-demand (one LLM call) so the
    /demand read stays fast for real-time monitoring."""
    try:
        return copilot.reason_demand(db)
    except AgentError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc))


@router.post("/capacity/{location_id}/rebalance", response_model=RebalanceResult)
def rebalance(location_id: str, db: Session = Depends(get_db),
              user: User = Depends(_ops)):
    """One-click fix for an over-capacity location: move the overflow to the
    best-fit same-type location(s). The correct response to over-capacity is to
    redistribute, not to buy."""
    return planning.rebalance_location(db, location_id, actor=user.email)

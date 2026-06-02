"""Capacity & flow planning — read-only views over orders, assets, and locations.

Three questions this answers:
  - inbound_pipeline  : what's still on order and when is it due (expected vs
    actual), driven by order-line quantities, dates, and what's been received;
  - location_capacity : how full is each location vs its (tunable) capacity;
  - deployment_forecast: how many units could land in service = on-hand
    (RECEIVED/IN_STORAGE) + still-inbound.

Everything here is computed from existing data — no new tables.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.flow import Asset, AssetStatus, Location, ReceiptItem
from app.models.procurement import OrderItem, OrderStatus, PurchaseOrder

# Statuses that count as "on hand in the warehouse, not yet deployed".
_ON_HAND = (AssetStatus.RECEIVED, AssetStatus.IN_STORAGE)
# Order statuses that still have units expected to arrive.
_OPEN_ORDER = (
    OrderStatus.PENDING, OrderStatus.APPROVED,
    OrderStatus.PLACED, OrderStatus.PARTIALLY_RECEIVED,
)


def _received_qty(db: Session, order_item_id: str) -> int:
    total = db.scalar(
        select(func.coalesce(func.sum(ReceiptItem.quantity_received), 0))
        .where(ReceiptItem.order_item_id == order_item_id)
    )
    return int(total or 0)


def inbound_pipeline(db: Session, *, as_of: Optional[date] = None) -> list[dict]:
    """Open order lines with quantity still outstanding, one row per line."""
    as_of = as_of or date.today()
    stmt = (
        select(OrderItem, PurchaseOrder)
        .join(PurchaseOrder, OrderItem.order_id == PurchaseOrder.id)
        .where(PurchaseOrder.status.in_(_OPEN_ORDER))
    )
    rows = db.execute(stmt).all()
    out = []
    for oi, order in rows:
        received = _received_qty(db, oi.id)
        outstanding = oi.quantity - received
        if outstanding <= 0:
            continue
        eta = oi.estimated_delivery_date
        out.append({
            "order_id": order.id,
            "order_number": order.order_number,
            "order_status": order.status,
            "order_item_id": oi.id,
            "product_id": oi.product_id,
            "ordered": oi.quantity,
            "received": received,
            "outstanding": outstanding,
            "estimated_delivery_date": eta,
            "overdue": bool(eta and eta < as_of),
        })
    return sorted(out, key=lambda r: (r["estimated_delivery_date"] or date.max))


def location_capacity(db: Session) -> list[dict]:
    """Per-location occupancy: assets currently there vs capacity."""
    locations = db.scalars(select(Location)).all()
    out = []
    for loc in locations:
        used = db.scalar(
            select(func.count(Asset.id)).where(Asset.current_location_id == loc.id)
        ) or 0
        free = (loc.capacity - used) if loc.capacity is not None else None
        utilisation = (used / loc.capacity) if loc.capacity else None
        out.append({
            "location_id": loc.id,
            "code": loc.code,
            "name": loc.name,
            "location_type": loc.location_type,
            "capacity": loc.capacity,
            "used": int(used),
            "free": free,
            "utilisation": round(utilisation, 4) if utilisation is not None else None,
            "over_capacity": bool(loc.capacity is not None and used > loc.capacity),
        })
    return out


def deployment_forecast(db: Session) -> dict:
    """Units that could reach service: on-hand (not yet deployed) + inbound."""
    on_hand = db.scalar(
        select(func.count(Asset.id)).where(Asset.status.in_(_ON_HAND))
    ) or 0
    deployed = db.scalar(
        select(func.count(Asset.id)).where(Asset.status == AssetStatus.DEPLOYED)
    ) or 0
    inbound = sum(r["outstanding"] for r in inbound_pipeline(db))
    return {
        "on_hand": int(on_hand),
        "inbound": int(inbound),
        "deployed": int(deployed),
        "forecast_deployable": int(on_hand) + int(inbound),
    }

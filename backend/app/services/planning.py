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

from datetime import date, timedelta
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.catalog import Product, ProductSupplier
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


def rebalance_location(db: Session, location_id: str, *, actor: Optional[str] = None) -> dict:
    """Move a location's overflow to the best-fit same-type location(s).

    Over-capacity means too many physical units in one place, so the fix is to
    redistribute — not to buy. We move ``used - capacity`` assets off the source
    into locations of the SAME type that have free space (most-free first), via
    the audited asset move. Returns what moved and where.
    """
    from app.services.asset import asset_service  # local import avoids cycle
    from app.services.exceptions import NotFoundError, ValidationError

    src = db.get(Location, location_id)
    if src is None:
        raise NotFoundError(f"Location {location_id!r} not found")
    if src.capacity is None:
        raise ValidationError("Location has no capacity set; nothing to rebalance")

    used = db.scalar(select(func.count(Asset.id)).where(Asset.current_location_id == src.id)) or 0
    overflow = int(used) - src.capacity
    if overflow <= 0:
        return {"moved": 0, "source": src.code, "targets": [],
                "message": f"{src.code} is within capacity ({used}/{src.capacity})."}

    # Candidate targets: same type, has capacity, currently has free space.
    candidates = []
    for loc in db.scalars(select(Location).where(
            Location.location_type == src.location_type, Location.id != src.id)).all():
        if loc.capacity is None:
            continue
        loc_used = db.scalar(select(func.count(Asset.id)).where(Asset.current_location_id == loc.id)) or 0
        free = loc.capacity - int(loc_used)
        if free > 0:
            candidates.append([loc, free])
    candidates.sort(key=lambda c: c[1], reverse=True)  # most free first

    # Assets to move off the source (most recently arrived first is fine).
    movable = db.scalars(
        select(Asset).where(Asset.current_location_id == src.id)
        .order_by(Asset.date_created.desc()).limit(overflow)
    ).all()

    moved = 0
    targets: dict[str, int] = {}
    ci = 0
    for asset in movable:
        # advance to a candidate with remaining free space
        while ci < len(candidates) and candidates[ci][1] <= 0:
            ci += 1
        if ci >= len(candidates):
            break  # no more room anywhere
        target, free = candidates[ci]
        asset_service.move(db, asset.id, target.id, actor=actor or "rebalance",
                           note=f"Auto-rebalance from {src.code} (over capacity)")
        candidates[ci][1] -= 1
        moved += 1
        targets[target.code] = targets.get(target.code, 0) + 1

    remaining = overflow - moved
    msg = f"Moved {moved} unit(s) off {src.code}"
    if targets:
        msg += " to " + ", ".join(f"{c} (+{n})" for c, n in targets.items())
    if remaining > 0:
        msg += f"; {remaining} still over — no same-type location has free space."
    return {"moved": moved, "source": src.code,
            "targets": [{"code": c, "moved": n} for c, n in targets.items()],
            "remaining_over": max(0, remaining), "message": msg}


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


# --- Inventory & reorder read model ---------------------------------------

_BURN_WINDOW_DAYS = 90        # trailing window for the daily-burn estimate
_DEFAULT_CAPACITY = 100       # per-product capacity proxy (no per-product field in the model)


def _preferred_source(db: Session, product_id: str) -> Optional[ProductSupplier]:
    return db.scalars(
        select(ProductSupplier)
        .where(ProductSupplier.product_id == product_id, ProductSupplier.active.is_(True))
        .order_by(ProductSupplier.preference_rank)
    ).first()


def inventory_plan(db: Session, *, today: Optional[date] = None) -> list[dict]:
    """Per-product stock + reorder INPUTS for the Inventory screen.

    The reorder MATH (cover, reorder point, status) lives client-side; this only
    supplies the inputs. Real vs derived:
      - on_hand      : real — count of RECEIVED/IN_STORAGE assets;
      - deployed_window / daily_burn : real — assets deployed in the trailing
                       window (Asset.deployed_date) / window length;
      - lead_time_days, unit_price   : real — from the preferred ProductSupplier;
      - on_order, next_eta           : real — from the open inbound pipeline;
      - capacity     : DERIVED proxy (no per-product capacity in the model) —
                       max(default, on_hand + on_order rounded up);
      - safety_stock : DERIVED — ~half of lead-time demand (burn x lead / 2).
    Products with neither stock nor inbound are omitted.
    """
    today = today or date.today()
    window_start = today - timedelta(days=_BURN_WINDOW_DAYS)

    # on-hand counts per product
    on_hand: dict[str, int] = {}
    for pid, n in db.execute(
        select(Asset.product_id, func.count(Asset.id))
        .where(Asset.status.in_(_ON_HAND)).group_by(Asset.product_id)
    ).all():
        on_hand[pid] = int(n)

    # deployed-in-window counts per product -> daily burn
    deployed_window: dict[str, int] = {}
    for pid, n in db.execute(
        select(Asset.product_id, func.count(Asset.id))
        .where(Asset.deployed_date.is_not(None), Asset.deployed_date >= window_start)
        .group_by(Asset.product_id)
    ).all():
        deployed_window[pid] = int(n)

    # open inbound per product: outstanding qty + earliest ETA
    on_order: dict[str, int] = {}
    next_eta: dict[str, Optional[date]] = {}
    for row in inbound_pipeline(db, as_of=today):
        pid = row["product_id"]
        on_order[pid] = on_order.get(pid, 0) + int(row["outstanding"])
        eta = row.get("estimated_delivery_date")
        if eta and (next_eta.get(pid) is None or eta < next_eta[pid]):
            next_eta[pid] = eta

    product_ids = set(on_hand) | set(on_order)
    out: list[dict] = []
    for pid in product_ids:
        product = db.get(Product, pid)
        src = _preferred_source(db, pid)
        oh = on_hand.get(pid, 0)
        oo = on_order.get(pid, 0)
        burn = round(deployed_window.get(pid, 0) / _BURN_WINDOW_DAYS, 4)
        lead = (src.standard_lead_time_days if src and src.standard_lead_time_days else 0)
        safety = int(round(burn * lead / 2))
        capacity = max(_DEFAULT_CAPACITY, oh + oo)
        eta = next_eta.get(pid)
        out.append({
            "product_id": pid,
            "product_code": product.product_code if product else None,
            "name": product.name if product else None,
            "category": product.category if product else None,
            "on_hand": oh,
            "capacity": capacity,
            "safety_stock": safety,
            "daily_burn": burn,
            "lead_time_days": lead,
            "on_order": oo,
            "next_eta": eta,
            "unit_price": (float(src.contract_price) if src and src.contract_price is not None else None),
        })
    return sorted(out, key=lambda r: (r["name"] or ""))

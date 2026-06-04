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

import math
from datetime import date, timedelta
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
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


# --- Demand forecast: usage-driven projection -----------------------------

def _weighted_daily_rate(deploy_dates: list[date], today: date) -> float:
    """Recency-weighted deployments/day over the trailing window.

    Each in-window deployment contributes an exponentially-decayed weight by its
    age (half-life = demand_halflife_days), so a recent ramp-up lifts the rate
    faster than a flat average would. The effective denominator is the
    decay-weighted length of the window, keeping the result in units/day.
    """
    window = settings.demand_window_days
    halflife = max(1, settings.demand_halflife_days)
    decay = math.log(2) / halflife
    weighted_events = 0.0
    for d in deploy_dates:
        age = (today - d).days
        if 0 <= age <= window:
            weighted_events += math.exp(-decay * age)
    # Effective weighted window length = integral of the decay over [0, window].
    eff_days = (1 - math.exp(-decay * window)) / decay
    return weighted_events / eff_days if eff_days > 0 else 0.0


def demand_forecast(db: Session, *, today: Optional[date] = None) -> list[dict]:
    """Per-product forward demand from real usage + end-of-life replacement.

    For each product:
      usage_rate   = recency-weighted deployments/day over the trailing window;
      projected_usage  = usage_rate x horizon;
      eol_replacement  = deployed assets that pass their useful-life within the
                         horizon (refresh demand an ageing fleet generates);
      projected_demand = projected_usage + eol_replacement;
      available        = on-hand (RECEIVED/IN_STORAGE) + open inbound;
      shortfall        = max(0, projected_demand - available);
      recommended_qty  = shortfall rounded up to the source MOQ;
      order_by         = horizon_end - lead_time (when to place to cover it).
    Products with no usage, no stock and no inbound are omitted.
    """
    today = today or date.today()
    horizon = settings.demand_horizon_days
    life = settings.asset_useful_life_days

    # All deployment dates per product (for the rate) and deployed ages (for EOL).
    deployed = db.scalars(
        select(Asset).where(Asset.deployed_date.is_not(None))
    ).all()
    deploys_by_product: dict[str, list[date]] = {}
    eol_by_product: dict[str, int] = {}
    for a in deployed:
        deploys_by_product.setdefault(a.product_id, []).append(a.deployed_date)
        # still in service (DEPLOYED/MAINTENANCE) and crosses useful-life within horizon?
        if a.status in (AssetStatus.DEPLOYED, AssetStatus.MAINTENANCE):
            age = (today - a.deployed_date).days
            if life - horizon <= age < life + horizon:
                eol_by_product[a.product_id] = eol_by_product.get(a.product_id, 0) + 1

    on_hand: dict[str, int] = {}
    for pid, n in db.execute(
        select(Asset.product_id, func.count(Asset.id))
        .where(Asset.status.in_(_ON_HAND)).group_by(Asset.product_id)
    ).all():
        on_hand[pid] = int(n)
    inbound = {}
    for row in inbound_pipeline(db, as_of=today):
        inbound[row["product_id"]] = inbound.get(row["product_id"], 0) + int(row["outstanding"])

    product_ids = set(deploys_by_product) | set(on_hand) | set(inbound)
    out: list[dict] = []
    for pid in product_ids:
        product = db.get(Product, pid)
        src = _preferred_source(db, pid)
        rate = _weighted_daily_rate(deploys_by_product.get(pid, []), today)
        projected_usage = rate * horizon
        eol = eol_by_product.get(pid, 0)
        projected_demand = projected_usage + eol
        available = on_hand.get(pid, 0) + inbound.get(pid, 0)
        shortfall = max(0.0, projected_demand - available)

        moq = (src.min_order_quantity or 1) if src else 1
        rec_qty = 0
        if shortfall > 0:
            rec_qty = max(int(math.ceil(shortfall)), moq)
            if moq > 1:
                rec_qty = int(math.ceil(rec_qty / moq) * moq)
        lead = (src.standard_lead_time_days or 0) if src else 0
        order_by = today + timedelta(days=max(0, horizon - lead))

        out.append({
            "product_id": pid,
            "product_code": product.product_code if product else None,
            "name": product.name if product else None,
            "category": product.category if product else None,
            "usage_rate_per_day": round(rate, 3),
            "horizon_days": horizon,
            "projected_usage": round(projected_usage, 1),
            "eol_replacement": eol,
            "projected_demand": round(projected_demand, 1),
            "on_hand": on_hand.get(pid, 0),
            "on_order": inbound.get(pid, 0),
            "available": available,
            "projected_shortfall": round(shortfall, 1),
            "recommended_order_qty": rec_qty,
            "order_by": order_by if rec_qty > 0 else None,
            "lead_time_days": lead,
            "unit_price": (float(src.contract_price) if src and src.contract_price is not None else None),
        })
    return sorted(out, key=lambda r: r["projected_shortfall"], reverse=True)

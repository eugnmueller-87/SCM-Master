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
from app.models.flow import Asset, AssetStatus, Location, LocationType, ReceiptItem
from app.models.procurement import OrderItem, OrderStatus, PurchaseOrder
from app.services import forecasting

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


# --- shared aggregations (one definition, reused across the read views) ----

def _on_hand_by_product(db: Session) -> dict[str, int]:
    """Count of on-hand (RECEIVED/IN_STORAGE) assets per product id."""
    return {
        pid: int(n) for pid, n in db.execute(
            select(Asset.product_id, func.count(Asset.id))
            .where(Asset.status.in_(_ON_HAND)).group_by(Asset.product_id)
        ).all()
    }


def _inbound_by_destination(db: Session) -> tuple[dict[str, int], dict[str, set[str]]]:
    """Outstanding inbound units per destination location, plus the set of PO
    numbers heading to each. Open orders only. Returns (units_by_loc, pos_by_loc)."""
    units: dict[str, int] = {}
    pos: dict[str, set[str]] = {}
    for oi, order in db.execute(
        select(OrderItem, PurchaseOrder)
        .join(PurchaseOrder, OrderItem.order_id == PurchaseOrder.id)
        .where(PurchaseOrder.status.in_(_OPEN_ORDER),
               PurchaseOrder.destination_id.is_not(None))
    ).all():
        outstanding = oi.quantity - _received_qty(db, oi.id)
        if outstanding <= 0:
            continue
        dest = order.destination_id
        units[dest] = units.get(dest, 0) + outstanding
        pos.setdefault(dest, set()).add(order.order_number)
    return units, pos


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


_CRITICAL_UTIL = 0.85  # at/above this a location is "approaching critical"


def capacity_diagnosis(db: Session, *, threshold: float = _CRITICAL_UTIL) -> list[dict]:
    """For each location at/over ``threshold`` utilisation, explain WHAT is
    filling it and recommend the RIGHT action.

    Over-capacity is a *placement* problem, not a buying one: too many units we
    already own are in one place, so the fix is to redistribute (move overflow to
    a same-type location with room) and/or hold inbound headed there — never to
    order more. This read surfaces the cause so that action is obvious:

      - cause breakdown: units here by product and by source PO (provenance);
      - inbound pressure: open order lines whose destination is THIS location and
        the units still expected, i.e. what will make it worse if not deferred;
      - recommended_action: ``rebalance`` when a same-type location has free space
        for the overflow, else ``hold_inbound`` when inbound is the pressure, else
        ``add_capacity`` (over capacity with nowhere to move — an infrastructure
        decision), else ``watch`` (near capacity but not yet over).

    Returns one row per critical location, most-utilised first. Empty when
    nothing is near capacity.
    """
    out: list[dict] = []
    caps = {c["location_id"]: c for c in location_capacity(db)}
    inbound_to_loc, inbound_pos_to_loc = _inbound_by_destination(db)

    for loc_id, cap in caps.items():
        util = cap["utilisation"]
        if util is None or util < threshold:
            continue

        # What's here, by product (name) and by source PO (provenance).
        assets = db.scalars(select(Asset).where(Asset.current_location_id == loc_id)).all()
        by_product: dict[str, int] = {}
        by_po: dict[str, int] = {}
        by_status: dict[str, int] = {}
        for a in assets:
            prod = db.get(Product, a.product_id)
            pname = prod.name if prod else a.product_id
            by_product[pname] = by_product.get(pname, 0) + 1
            by_status[a.status.value] = by_status.get(a.status.value, 0) + 1
            if a.source_order_item_id:
                oi = db.get(OrderItem, a.source_order_item_id)
                if oi:
                    order = db.get(PurchaseOrder, oi.order_id)
                    if order:
                        by_po[order.order_number] = by_po.get(order.order_number, 0) + 1

        inbound_units = inbound_to_loc.get(loc_id, 0)
        inbound_pos = sorted(inbound_pos_to_loc.get(loc_id, set()))

        # Is there room to rebalance? A same-type location with free space.
        room_elsewhere = 0
        targets: list[dict] = []
        for other in caps.values():
            if other["location_id"] == loc_id or other["location_type"] != cap["location_type"]:
                continue
            free = other["free"]
            if free and free > 0:
                room_elsewhere += free
                targets.append({"code": other["code"], "free": int(free)})
        targets.sort(key=lambda t: t["free"], reverse=True)

        overflow = max(0, cap["used"] - (cap["capacity"] or cap["used"]))
        near = not cap["over_capacity"]  # at threshold but not yet over

        if overflow > 0 and room_elsewhere > 0:
            action = "rebalance"
            summary = (f"{cap['used']}/{cap['capacity']} used — move {min(overflow, room_elsewhere)} "
                       f"unit(s) to {targets[0]['code']} (has room). Do NOT order more.")
        elif inbound_units > 0:
            action = "hold_inbound"
            summary = (f"{cap['used']}/{cap['capacity']} used and {inbound_units} more inbound "
                       f"({', '.join(inbound_pos)}) — hold/defer that delivery; it has nowhere to land.")
        elif overflow > 0:
            action = "add_capacity"
            summary = (f"{cap['used']}/{cap['capacity']} used, over capacity, and no same-type "
                       f"location has free space — this is an infrastructure problem: add "
                       f"capacity or decommission units. Moving won't fix it.")
        else:
            action = "watch"
            summary = f"{cap['used']}/{cap['capacity']} used — approaching capacity; watch incoming."

        out.append({
            "location_id": loc_id,
            "code": cap["code"],
            "name": cap["name"],
            "location_type": cap["location_type"],
            "used": cap["used"],
            "capacity": cap["capacity"],
            "utilisation": util,
            "over_capacity": cap["over_capacity"],
            "near_capacity": near,
            "overflow": overflow,
            "inbound_units": inbound_units,
            "inbound_pos": inbound_pos,
            "by_product": [{"name": k, "units": v} for k, v in sorted(by_product.items(), key=lambda x: -x[1])],
            "by_source_po": [{"order_number": k, "units": v} for k, v in sorted(by_po.items(), key=lambda x: -x[1])],
            "by_status": by_status,
            "room_elsewhere": room_elsewhere,
            "rebalance_targets": targets,
            "recommended_action": action,
            "summary": summary,
        })

    out.sort(key=lambda r: r["utilisation"], reverse=True)
    return out


def storage_headroom(db: Session) -> dict:
    """How many MORE units we could physically land — the cap on any order.

    Goods arrive into WAREHOUSE-type locations (the transit warehouse + staging
    zones), so the storable maximum is the free space across those, **net of what
    is already inbound** (open orders heading there consume future space). This is
    the safe inverse of the over-capacity rule: never order more than will fit.

    Returns total storable headroom + a per-zone breakdown, so a buy can be capped
    (and the UI can say "max N more — that's all the warehouse can take").
    """
    zones = []
    total_free = 0
    total_inbound = 0
    caps = {c["location_id"]: c for c in location_capacity(db)}
    inbound_to_loc, _ = _inbound_by_destination(db)

    for loc_id, c in caps.items():
        if c["location_type"] != LocationType.WAREHOUSE or c["capacity"] is None:
            continue
        inbound = inbound_to_loc.get(loc_id, 0)
        # storable = free space now, minus units already on the way in
        storable = max(0, (c["free"] or 0) - inbound)
        total_free += (c["free"] or 0)
        total_inbound += inbound
        zones.append({
            "code": c["code"], "name": c["name"],
            "capacity": c["capacity"], "used": c["used"],
            "free": c["free"], "inbound": inbound, "storable": storable,
        })

    zones.sort(key=lambda z: z["storable"], reverse=True)
    # No warehouse zone with a defined capacity -> no known storage limit, so the
    # cap doesn't apply (None, not 0 — 0 would wrongly block all purchasing).
    storable_max = max(0, total_free - total_inbound) if zones else None
    return {
        "storable_max": storable_max,           # units we could order and still store (None = no defined limit)
        "free_now": total_free,
        "committed_inbound": total_inbound,
        "zones": zones,
    }


def capacity_flow(db: Session, *, today: Optional[date] = None) -> dict:
    """ONE warehouse capacity-vs-flow picture — the single source of truth for the
    'can we store more / when do we run dry' question.

    Thin composition of existing services (no parallel definitions):
      - capacity / used / free / inbound  ← storage_headroom (warehouse zones);
      - daily_in   = inbound units ÷ days-until-ETA, summed over the open pipeline;
      - daily_out  = Σ daily_burn across SKUs (the same burn inventory_plan uses);
      - on_hand    = units physically in warehouse zones now (= Σ used);
      - weeks_of_cover  = on_hand ÷ daily_out ÷ 7   (how long current stock lasts);
      - days_to_depletion = on_hand ÷ daily_out      (when we hit zero at current burn);
      - committed_pct = (used + inbound) ÷ capacity  (the over-order guard reads this).

    Returned per-zone AND as a portfolio rollup. ``storable_max`` is the hard cap a
    new order must respect (None = no warehouse capacity defined → no cap).
    """
    today = today or date.today()
    head = storage_headroom(db)
    zones = head["zones"]

    # daily_in: spread each open inbound line's outstanding qty over the days until
    # its ETA (min 1 day), so a big PO landing tomorrow counts as a fast inflow.
    daily_in = 0.0
    for row in inbound_pipeline(db, as_of=today):
        out = int(row.get("outstanding", 0))
        eta = row.get("estimated_delivery_date")
        days = max(1, (eta - today).days) if eta else 30
        daily_in += out / days

    # daily_out: the same recency-weighted burn the inventory plan uses (one
    # definition of consumption, not a second one).
    daily_out = sum(r["daily_burn"] for r in inventory_plan(db, today=today))

    on_hand = sum(z["used"] for z in zones)
    total_cap = sum(z["capacity"] for z in zones) or 0
    total_inbound = head["committed_inbound"]
    committed = on_hand + total_inbound

    weeks_cover = round(on_hand / daily_out / 7, 1) if daily_out > 0 else None
    days_deplete = round(on_hand / daily_out, 1) if daily_out > 0 else None

    return {
        "as_of": today.isoformat(),
        "capacity": total_cap,
        "on_hand": on_hand,
        "inbound": total_inbound,
        "committed": committed,                                  # on_hand + inbound
        "free_to_order": head["storable_max"],                   # the hard cap (None = no limit)
        "committed_pct": round(committed / total_cap, 4) if total_cap else None,
        "daily_in": round(daily_in, 2),                          # incoming units/day
        "daily_out": round(daily_out, 2),                        # outgoing units/day (burn)
        "net_flow_per_day": round(daily_in - daily_out, 2),      # >0 filling, <0 draining
        "weeks_of_cover": weeks_cover,                           # how long on-hand lasts
        "days_to_depletion": days_deplete,                       # when on-hand hits 0
        "zones": zones,
    }


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

_BURN_WINDOW_DAYS = 90        # trailing window for the daily-burn (rate) estimate
_VARIABILITY_WINDOW_DAYS = 365  # longer window for demand-variability (σ) — needs
                                # several lead-times of history to see batch lumpiness
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
    burn_start = today - timedelta(days=_BURN_WINDOW_DAYS)
    var_start = today - timedelta(days=_VARIABILITY_WINDOW_DAYS)

    # on-hand counts per product
    on_hand = _on_hand_by_product(db)

    # Deploy DATES per product over the LONGER variability window. The recent
    # burn-window slice drives the rate; the full window drives demand-variability
    # σ — a lumpy SKU (batches + zeros) gets a high σ, a steady one low. Keeping
    # dates (not counts) lets safety_stock bucket them by lead time.
    deploy_dates_var: dict[str, list[date]] = {}
    for pid, dep in db.execute(
        select(Asset.product_id, Asset.deployed_date)
        .where(Asset.deployed_date.is_not(None), Asset.deployed_date >= var_start)
    ).all():
        deploy_dates_var.setdefault(pid, []).append(dep)

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

    # First pass: annualised value per product (burn/day × 365 × unit_price) for
    # ABC classification, so each item's service level reflects its importance.
    sources = {pid: _preferred_source(db, pid) for pid in product_ids}
    annual_value: dict[str, float] = {}
    for pid in product_ids:
        burn_dates = [d for d in deploy_dates_var.get(pid, []) if d >= burn_start]
        burn = len(burn_dates) / _BURN_WINDOW_DAYS
        src = sources[pid]
        price = float(src.contract_price) if src and src.contract_price is not None else 0.0
        annual_value[pid] = burn * 365.0 * price

    abc = forecasting.classify_abc(
        annual_value, a_threshold=settings.abc_a_threshold,
        b_threshold=settings.abc_b_threshold)
    abc_sl = {"A": settings.abc_service_level_a, "B": settings.abc_service_level_b,
              "C": settings.abc_service_level_c}

    out: list[dict] = []
    for pid in product_ids:
        product = db.get(Product, pid)
        src = sources[pid]
        oh = on_hand.get(pid, 0)
        oo = on_order.get(pid, 0)
        all_dates = deploy_dates_var.get(pid, [])
        burn_dates = [d for d in all_dates if d >= burn_start]
        burn = round(len(burn_dates) / _BURN_WINDOW_DAYS, 4)
        lead = (src.standard_lead_time_days if src and src.standard_lead_time_days else 0)

        # ABC class drives the service level: class A (the high-value few) is
        # protected hardest, class C runs leaner.
        abc_class = abc.get(pid, "C")
        service_level = abc_sl[abc_class]

        # Service-level safety stock = z(SL) × σ(demand over lead time), from the
        # FULL variability window so batch lumpiness is visible (replaces the old
        # burn×lead/2 heuristic). Lumpy demand → large σ → large buffer; ~constant
        # demand → ~0.
        series = forecasting.daily_series(all_dates, today, _VARIABILITY_WINDOW_DAYS)
        safety = forecasting.safety_stock(series, lead, service_level=service_level)

        # Server-side reorder math (was client-side): reorder point = expected
        # lead-time demand + safety stock; status from available vs that point.
        reorder_point = int(math.ceil(burn * lead)) + safety
        available = oh + oo
        if available <= reorder_point:
            status = "reorder" if available <= safety else "low"
        else:
            status = "ok"

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
            "reorder_point": reorder_point,
            "reorder_status": status,
            "abc_class": abc_class,
            "service_level": service_level,
            "daily_burn": burn,
            "lead_time_days": lead,
            "on_order": oo,
            "next_eta": eta,
            "unit_price": (float(src.contract_price) if src and src.contract_price is not None else None),
        })
    return sorted(out, key=lambda r: (r["name"] or ""))


# --- Demand forecast: usage-driven projection -----------------------------

def _weighted_daily_rate(deploy_dates: list[date], today: date) -> float:
    """Recency-weighted deployments/day (delegates to the shared estimator).

    Kept as a thin wrapper so existing callers/tests are unaffected; the
    definition now lives in services.forecasting so the backtest and live
    forecast share exactly one implementation.
    """
    return forecasting.weighted_daily_rate(
        deploy_dates, today,
        window_days=settings.demand_window_days,
        halflife_days=settings.demand_halflife_days,
    )


def demand_forecast(db: Session, *, today: Optional[date] = None,
                    method: Optional[str] = None) -> list[dict]:
    """Per-product forward demand from real usage + end-of-life replacement.

    ``method`` selects the rate estimator (defaults to ``settings.forecast_method``):
      "run_rate" (incumbent), "tsb" (intermittent), or "auto" (classify each SKU
      and route lumpy ones to TSB). The EOL replacement term is method-independent
      and always added. Each row reports ``forecast_method`` = what actually ran
      for that SKU (so "auto" shows per-SKU routing).

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
    method = method or settings.forecast_method

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

    on_hand = _on_hand_by_product(db)
    inbound = {}
    for row in inbound_pipeline(db, as_of=today):
        inbound[row["product_id"]] = inbound.get(row["product_id"], 0) + int(row["outstanding"])

    product_ids = set(deploys_by_product) | set(on_hand) | set(inbound)
    out: list[dict] = []
    for pid in product_ids:
        product = db.get(Product, pid)
        src = _preferred_source(db, pid)
        rate, method_used = forecasting.daily_rate(
            method, deploys_by_product.get(pid, []), today,
            window_days=settings.demand_window_days,
            halflife_days=settings.demand_halflife_days,
            tsb_alpha=settings.forecast_tsb_alpha, tsb_beta=settings.forecast_tsb_beta,
        )
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
            "forecast_method": method_used,
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

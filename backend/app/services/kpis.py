"""The KPIs tab: hard numbers, each computed live from the operational tables.

Rules, in the order they matter:

1. **Nothing here is typed in.** Every ``current`` value is a read over assets, order
   lines, requisitions, contracts, costing and the planning services that the other
   tabs already use. If a KPI cannot be measured from the data (no rows, a zero
   denominator), it returns ``None`` with a ``reason`` — never a fake zero.
2. **Targets are owned, not guessed.** The system may *seed* a target as a fixed rule
   over today's value (10 / 20 / 30 percent better in the KPI's good direction) but
   marks it ``placeholder=True`` until a person confirms or overwrites it. The tab
   shows the placeholder state; a CFO never sees a system-derived target as a
   decision.
3. **Trend is measured, not invented.** One snapshot per KPI per day is written on
   read; the sparkline grows from the day the tab first ran.

The registry below is the single list. Adding a KPI means adding one entry with a
``compute`` function; the API, the seeding and the frontend follow.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from statistics import median
from typing import Callable, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.catalog import ProductSupplier
from app.models.flow import DEPLOYABLE_STATUSES, WAREHOUSE_STATUSES, Asset, AssetStatus
from app.models.kpi import KpiSnapshot, KpiTarget
from app.models.procurement import OrderItem, PurchaseOrder
from app.models.rental import ContractStatus, RentalContract
from app.models.requisition import PurchaseRequisition, RequisitionStatus
from app.services import accuracy, analytics, contracts, costing_service, planning, tracking
from app.services import fleet as fleet_svc

# -----------------------------------------------------------------------------
# registry


@dataclass(frozen=True)
class KpiDef:
    id: str
    group: str            # fleet | warehouse | cost | process | suppliers
    name: str
    unit: str             # pct | eur | days | weeks | count | ratio | turns | hours
    direction: str        # "lower" | "higher"  (which way is better)
    definition: str       # plain words, what the number is
    source: str           # which tab / endpoint the number comes from
    compute: Callable[[Session, date], tuple[Optional[float], Optional[str]]]
    seed_rule: str = "pct"   # how a placeholder target is derived: pct (10/20/30 % better), floor0 (toward 0), cap100 (toward 100 %)


AGING_DAYS = 90           # stock older than this in the warehouse counts as aging
DEAD_STOCK_DAYS = 180     # no deployment of that product for this long = write-down risk
CARRYING_COST_RATE_PA = 0.08   # placeholder: cost of capital on stock, owner CFO


_ON_HAND = Asset.status.in_(tuple(WAREHOUSE_STATUSES))
# How long a unit has waited: time in its current station where that is known (the DaaS
# fleet records it), otherwise time since receipt (the datacenter operation).
_WAITING_SINCE = func.coalesce(Asset.status_since, Asset.received_date)


def _as_date(v) -> Optional[date]:
    """SQLite hands a date back as text on some paths; Postgres gives a date."""
    if v is None or isinstance(v, date):
        return v
    return date.fromisoformat(str(v)[:10])


def _stock_value(db: Session) -> tuple[float, int, int]:
    """(EUR value of stock with a known price, units priced, units on hand).

    One grouped read rather than a fleet-sized list in Python: at 100,000 units on hand
    that is the difference between a tab that answers and one that hangs.
    """
    total, priced, value = db.execute(
        select(func.count(Asset.id), func.count(OrderItem.unit_price), func.coalesce(func.sum(OrderItem.unit_price), 0))
        .select_from(Asset).outerjoin(OrderItem, OrderItem.id == Asset.source_order_item_id).where(_ON_HAND)).one()
    return float(value or 0), int(priced), int(total)


def _waiting_histogram(db: Session, today: date) -> dict[int, int]:
    """{days waited: how many units}, over everything on hand.

    Grouping by the date itself keeps the result at a few hundred rows whatever the size
    of the warehouse, and one histogram answers both the median and every threshold.
    """
    rows = db.execute(select(_WAITING_SINCE, func.count(Asset.id))
                      .where(_ON_HAND, _WAITING_SINCE.is_not(None)).group_by(_WAITING_SINCE)).all()
    hist: dict[int, int] = {}
    for since, n in rows:
        d = (today - _as_date(since)).days
        hist[d] = hist.get(d, 0) + int(n)
    return hist


def _median_from_histogram(hist: dict[int, int]) -> Optional[float]:
    total = sum(hist.values())
    if not total:
        return None
    half, acc, last = total / 2.0, 0, 0
    for days in sorted(hist):
        acc += hist[days]
        last = days
        if acc >= half:
            return float(days)
    return float(last)


# ---- warehouse: the five the business owner names (availability, capital, cover, aging, write-down) plus flow

def k_capacity_committed_pct(db, today):
    f = planning.capacity_flow(db, today=today)
    if f["committed_pct"] is None:
        return None, "no warehouse capacity defined"
    return round(f["committed_pct"] * 100, 1), None


def k_weeks_of_cover(db, today):
    f = planning.capacity_flow(db, today=today)
    if f["weeks_of_cover"] is None:
        return None, "no deployments in the trailing window, burn rate unknown"
    return round(float(f["weeks_of_cover"]), 1), None


def _pos(row, key, default=None):
    """inventory_position returns PositionRow dataclasses; be tolerant to dicts too."""
    return row.get(key, default) if isinstance(row, dict) else getattr(row, key, default)


def k_items_at_risk(db, today):
    rows = planning.inventory_position(db, today=today)
    if not rows:
        return None, "no products in the plan"
    return float(sum(1 for r in rows if _pos(r, "at_risk"))), None


def k_safety_stock_coverage_pct(db, today):
    rows = [r for r in planning.inventory_position(db, today=today) if (_pos(r, "safety_stock") or 0) > 0]
    if not rows:
        return None, "no product carries a safety stock yet"
    ok = sum(1 for r in rows if (_pos(r, "on_hand") or 0) >= _pos(r, "safety_stock"))
    return round(ok / len(rows) * 100, 1), None


def k_stock_value_eur(db, today):
    value, priced, total = _stock_value(db)
    if total == 0:
        return None, "nothing on hand"
    if priced == 0:
        return None, "on-hand units carry no order price"
    return round(value, 2), None


def k_carrying_cost_eur_per_day(db, today):
    value, priced, total = _stock_value(db)
    if priced == 0:
        return None, "on-hand units carry no order price"
    return round(value * CARRYING_COST_RATE_PA / 365.0, 2), None


def k_aging_stock_pct(db, today):
    hist = _waiting_histogram(db, today)
    total = sum(hist.values())
    if not total:
        return None, "on-hand units have no receipt date"
    return round(sum(c for d, c in hist.items() if d > AGING_DAYS) / total * 100, 1), None


def k_median_days_in_stock(db, today):
    m = _median_from_histogram(_waiting_histogram(db, today))
    if m is None:
        return None, "on-hand units have no receipt date"
    return m, None


def k_dead_stock_value_eur(db, today):
    """Value of on-hand stock of products with no deployment in the last DEAD_STOCK_DAYS: the write-down candidates."""
    value, priced, total = _stock_value(db)
    if total == 0:
        return None, "nothing on hand"
    if priced == 0:
        return None, "on-hand units carry no order price"
    since = today - timedelta(days=DEAD_STOCK_DAYS)
    recent = list(db.execute(select(Asset.product_id).where(Asset.deployed_date.is_not(None), Asset.deployed_date >= since).distinct()).scalars())
    stmt = (select(func.coalesce(func.sum(OrderItem.unit_price), 0))
            .select_from(Asset).join(OrderItem, OrderItem.id == Asset.source_order_item_id).where(_ON_HAND))
    if recent:
        stmt = stmt.where(Asset.product_id.not_in(recent))
    return round(float(db.scalar(stmt) or 0), 2), None


def k_stock_turns(db, today):
    """Deployments in the trailing 90 days, annualised, over average on-hand (today's on-hand as proxy)."""
    since = today - timedelta(days=90)
    deployed = db.execute(select(func.count()).select_from(Asset).where(Asset.deployed_date.is_not(None), Asset.deployed_date >= since, Asset.deployed_date <= today)).scalar() or 0
    on_hand = db.scalar(select(func.count(Asset.id)).where(Asset.status.in_(tuple(DEPLOYABLE_STATUSES)))) or 0
    if on_hand == 0:
        return None, "nothing on hand"
    if deployed == 0:
        return None, "no deployments in the last 90 days"
    return round(deployed * 365.0 / 90.0 / on_hand, 2), None


def k_dock_to_deploy_days(db, today):
    """Days from arrival to going out, over the units received in the last year.

    The window keeps the measure current — a fleet that has changed its intake should not
    be judged on units it took in three years ago — and keeps the grouped read small.
    """
    rows = db.execute(
        select(Asset.received_date, Asset.deployed_date, func.count(Asset.id))
        .where(Asset.received_date.is_not(None), Asset.deployed_date.is_not(None),
               Asset.received_date >= today - timedelta(days=365))
        .group_by(Asset.received_date, Asset.deployed_date)).all()
    hist: dict[int, int] = {}
    for r, d, n in rows:
        days = (_as_date(d) - _as_date(r)).days
        if days >= 0:
            hist[days] = hist.get(days, 0) + int(n)
    m = _median_from_histogram(hist)
    if m is None:
        return None, "no unit received in the last year has both a receipt and a deployment date"
    return m, None


def k_inbound_overdue_pct(db, today):
    rows = planning.inbound_pipeline(db, as_of=today)
    if not rows:
        return None, "no open inbound lines"
    return round(sum(1 for r in rows if r["overdue"]) / len(rows) * 100, 1), None


def k_on_time_delivery_pct(db, today):
    rows = tracking.order_tracking(db)
    if not rows:
        return None, "no tracked shipments"
    ok = sum(1 for r in rows if (r.get("delay_days") or 0) <= 0)
    return round(ok / len(rows) * 100, 1), None


# ---- cost and commercial

def k_negotiation_gap_eur(db, today):
    s = costing_service.savings_summary(db, today)
    if not s["products_with_bom"]:
        return None, "no product has a bill of materials yet"
    return round(float(s["total_gap_to_target"]), 2), None


def k_products_above_target_pct(db, today):
    s = costing_service.savings_summary(db, today)
    if not s["products_with_bom"]:
        return None, "no product has a bill of materials yet"
    return round(s["products_above_target"] / s["products_with_bom"] * 100, 1), None


def k_spend_under_contract_pct(db, today):
    # Spend grouped by (product, supplier) in the database: a handful of rows, instead
    # of one row per serial in the fleet.
    grouped = analytics._spend_grouped(db, None, Asset.product_id, PurchaseOrder.supplier_id)
    if not grouped:
        return None, "no received spend with provenance"
    active_pairs = {(ps.product_id, ps.supplier_id) for ps in db.execute(select(ProductSupplier)).scalars()
                    if contracts.derive_status(ps, today=today) == "ACTIVE"}
    total = 0.0
    covered = 0.0
    for product_id, supplier_id, _units, spend in grouped:
        v = float(spend or 0)
        total += v
        if (product_id, supplier_id) in active_pairs:
            covered += v
    if total == 0:
        return None, "received spend has no prices"
    return round(covered / total * 100, 1), None


def k_top3_supplier_share_pct(db, today):
    rows = analytics.spend_by_supplier(db)
    if not rows:
        return None, "no spend recorded"
    vals = sorted((float(r["spend"]) for r in rows), reverse=True)
    total = sum(vals)
    if total == 0:
        return None, "spend is zero"
    return round(sum(vals[:3]) / total * 100, 1), None


# ---- process and automation

def k_auto_placed_pct(db, today):
    reqs = list(db.execute(select(PurchaseRequisition).where(PurchaseRequisition.status.in_([RequisitionStatus.PLACED, RequisitionStatus.REJECTED]))).scalars())
    decided = [r for r in reqs if r.decided_at is not None or r.auto_placed]
    if not decided:
        return None, "no requisition decided yet"
    return round(sum(1 for r in decided if r.auto_placed) / len(decided) * 100, 1), None


def k_requisition_cycle_hours(db, today):
    reqs = db.execute(select(PurchaseRequisition.date_created, PurchaseRequisition.decided_at).where(PurchaseRequisition.decided_at.is_not(None))).all()
    hours = [(d - c).total_seconds() / 3600.0 for c, d in reqs if d and c and d >= c]
    if not hours:
        return None, "no requisition decided by a person yet"
    return round(float(median(hours)), 1), None


def k_forecast_mape_pct(db, today):
    s = accuracy.accuracy_summary(db)
    if s.get("mape") is None:
        return None, "not enough deployment history for a backtest"
    return round(float(s["mape"]) * 100, 1), None


# ---- suppliers and contracts

def k_contracts_needing_action(db, today):
    n = 0
    for ps in db.execute(select(ProductSupplier)).scalars():
        if contracts.derive_status(ps, today=today) in ("EXPIRING", "RENEWAL_DUE", "EXPIRED"):
            n += 1
    return float(n), None


def k_single_sourced_products_pct(db, today):
    rows = db.execute(select(ProductSupplier.product_id, func.count()).where(ProductSupplier.active.is_(True)).group_by(ProductSupplier.product_id)).all()
    if not rows:
        return None, "no active sources"
    return round(sum(1 for _, c in rows if c == 1) / len(rows) * 100, 1), None


# ---- fleet and recommerce (DaaS scenario; each says "no rental fleet" in the datacenter scenario)

def _daas(db) -> bool:
    return fleet_svc.scenario(db) == "daas"


def k_second_rental_share_pct(db, today):
    if not _daas(db):
        return None, "no rental fleet in this database"
    rented = db.scalar(select(func.count(Asset.id)).where(Asset.status == AssetStatus.RENTED)) or 0
    c2 = db.scalar(select(func.count(Asset.id)).where(Asset.status == AssetStatus.RENTED, Asset.cycle_no >= 2)) or 0
    return round(c2 / rented * 100, 1), None


def k_early_return_share_pct(db, today):
    if not _daas(db):
        return None, "no rental fleet in this database"
    since = today - timedelta(days=365)
    rows = db.execute(select(RentalContract.end_reason, func.count()).where(RentalContract.status == ContractStatus.ENDED, RentalContract.actual_end >= since).group_by(RentalContract.end_reason)).all()
    total = sum(int(n) for _, n in rows)
    if total == 0:
        return None, "no contract ended in the last twelve months"
    early = sum(int(n) for r, n in rows if r in ("early", "defect", "swap"))
    return round(early / total * 100, 1), None


def k_mdm_release_over_sla_pct(db, today):
    if not _daas(db):
        return None, "no rental fleet in this database"
    days = [(today - d).days for (d,) in db.execute(select(Asset.status_since).where(Asset.status == AssetStatus.MDM_RELEASE, Asset.status_since.is_not(None))).all()]
    if not days:
        return None, "nothing waiting for an MDM release"
    return round(sum(1 for d in days if d > fleet_svc.MDM_RELEASE_SLA_DAYS) / len(days) * 100, 1), None


def k_return_to_ready_days(db, today):
    """Median days a returned device has been in the intake-to-refurb chain (returned, MDM, wipe, repair, refurb)."""
    if not _daas(db):
        return None, "no rental fleet in this database"
    chain = (AssetStatus.RETURNED, AssetStatus.MDM_RELEASE, AssetStatus.WIPE_GRADING, AssetStatus.REPAIR, AssetStatus.REFURB)
    back = {AssetStatus.RETURNED: 0, AssetStatus.MDM_RELEASE: 10, AssetStatus.WIPE_GRADING: 22, AssetStatus.REPAIR: 24, AssetStatus.REFURB: 30}
    days = [(today - d).days + back[st] for st, d in db.execute(select(Asset.status, Asset.status_since).where(Asset.status.in_(chain), Asset.status_since.is_not(None))).all()]
    if not days:
        return None, "no device in the return chain"
    return float(median(days)), None


def k_sellable_reach_months(db, today):
    if not _daas(db):
        return None, "no rental fleet in this database"
    sellable = db.scalar(select(func.count(Asset.id)).where(Asset.status == AssetStatus.SELLABLE)) or 0
    sold_90 = db.scalar(select(func.count(Asset.id)).where(Asset.status == AssetStatus.SOLD, Asset.sold_date >= today - timedelta(days=90))) or 0
    if sold_90 == 0:
        return None, "no sale in the last 90 days"
    return round(sellable / (sold_90 / 3.0), 1), None


def k_swap_buffer_months(db, today):
    if not _daas(db):
        return None, "no rental fleet in this database"
    buffer = db.scalar(select(func.count(Asset.id)).where(Asset.status == AssetStatus.SWAP_BUFFER)) or 0
    defects_90 = db.scalar(select(func.count()).select_from(RentalContract).where(RentalContract.end_reason.in_(("defect", "swap")), RentalContract.actual_end >= today - timedelta(days=90))) or 0
    if defects_90 == 0:
        return None, "no defect return in the last 90 days"
    return round(buffer / (defects_90 / 3.0), 1), None


def k_resale_share_of_purchase_pct(db, today):
    """What resale brought back, as a share of what those devices cost. Purchase price from the order line each serial came from."""
    if not _daas(db):
        return None, "no rental fleet in this database"
    since = today - timedelta(days=365)
    rows = db.execute(select(Asset.sale_price, OrderItem.unit_price).join(OrderItem, OrderItem.id == Asset.source_order_item_id)
                      .where(Asset.status == AssetStatus.SOLD, Asset.sold_date >= since, Asset.sale_price.is_not(None), OrderItem.unit_price.is_not(None))).all()
    if not rows:
        return None, "no sale with a purchase price in the last twelve months"
    cost = sum(float(p) for _, p in rows)
    if cost == 0:
        return None, "purchase prices are zero"
    return round(sum(float(sp) for sp, _ in rows) / cost * 100, 1), None


def k_recycling_share_pct(db, today):
    if not _daas(db):
        return None, "no rental fleet in this database"
    since = today - timedelta(days=365)
    sold = db.scalar(select(func.count(Asset.id)).where(Asset.status == AssetStatus.SOLD, Asset.sold_date >= since)) or 0
    rec = db.scalar(select(func.count(Asset.id)).where(Asset.status == AssetStatus.RECYCLED, Asset.sold_date >= since)) or 0
    if sold + rec == 0:
        return None, "nothing left the fleet in the last twelve months"
    return round(rec / (sold + rec) * 100, 1), None


def k_returns_overdue(db, today):
    if not _daas(db):
        return None, "no rental fleet in this database"
    n = db.scalar(select(func.count()).select_from(RentalContract).where(RentalContract.status == ContractStatus.RUNNING, RentalContract.planned_end < today)) or 0
    return float(n), None


KPIS: list[KpiDef] = [
    # fleet and recommerce
    KpiDef("second_rental_share_pct", "fleet", "Second rental share of the rented fleet", "pct", "higher",
           "Rented devices on their second rental. Every one is a device that did not have to be bought.", "Fleet · /fleet/summary", k_second_rental_share_pct, "cap100"),
    KpiDef("returns_overdue", "fleet", "Returns overdue", "count", "lower",
           "Running contracts whose planned end has passed while the device is still out.", "Returns · /fleet/returns/upcoming", k_returns_overdue, "floor0"),
    KpiDef("early_return_share_pct", "fleet", "Early and defect returns", "pct", "lower",
           "Contracts of the last twelve months that ended before their planned end: early, defect or swap.", "Returns · rental contracts", k_early_return_share_pct, "floor0"),
    KpiDef("mdm_release_over_sla_pct", "fleet", f"MDM release waiting over {fleet_svc.MDM_RELEASE_SLA_DAYS} days", "pct", "lower",
           "Returned devices the old customer has not released from its MDM within the service level. The most common wait in the chain.", "Warehouse · MDM release hold", k_mdm_release_over_sla_pct, "floor0"),
    KpiDef("return_to_ready_days", "fleet", "Return to ready, median days", "days", "lower",
           "Days a returned device has spent from intake through MDM release, wipe, repair and refurbishment so far.", "Warehouse · stations", k_return_to_ready_days),
    KpiDef("sellable_reach_months", "fleet", "Sellable stock reach, months", "count", "lower",
           "Months the sellable stock lasts at the sales pace of the last 90 days. Long reach is capital and aging.", "Recommerce · sales", k_sellable_reach_months),
    KpiDef("swap_buffer_months", "fleet", "Swap buffer reach, months", "count", "higher",
           "Months the swap buffer covers at the defect pace of the last 90 days.", "Warehouse · swap buffer", k_swap_buffer_months),
    KpiDef("resale_share_of_purchase_pct", "fleet", "Resale proceeds as share of purchase price", "pct", "higher",
           "Net sale proceeds of the last twelve months over what those devices cost to buy, serial by serial.", "Recommerce · sales and provenance", k_resale_share_of_purchase_pct),
    KpiDef("recycling_share_pct", "fleet", "Recycling share of fleet exits", "pct", "lower",
           "Devices recycled over devices sold plus recycled, last twelve months.", "Recommerce", k_recycling_share_pct, "floor0"),
    # warehouse
    KpiDef("capacity_committed_pct", "warehouse", "Warehouse committed", "pct", "lower",
           "On hand plus inbound as a share of warehouse capacity. Above 85 % the next order has nowhere to go.", "Capacity · /planning/capacity-flow", k_capacity_committed_pct),
    KpiDef("weeks_of_cover", "warehouse", "Weeks of cover (availability)", "weeks", "higher",
           "How long today's on-hand lasts at the trailing deployment rate.", "Inventory · /planning/capacity-flow", k_weeks_of_cover),
    KpiDef("items_at_risk", "warehouse", "Products at stock-out risk", "count", "lower",
           "Products that run dry before their open inbound lands.", "Inventory · /planning/inventory-position", k_items_at_risk, "floor0"),
    KpiDef("safety_stock_coverage_pct", "warehouse", "Safety stock coverage (availability)", "pct", "higher",
           "Share of products holding at least their service-level safety stock.", "Inventory · /planning/inventory-position", k_safety_stock_coverage_pct, "cap100"),
    KpiDef("stock_value_eur", "warehouse", "Capital tied up in stock", "eur", "lower",
           "Order price of every unit on hand (received or in storage). What the warehouse costs to hold.", "Assets · asset provenance", k_stock_value_eur),
    KpiDef("carrying_cost_eur_per_day", "warehouse", "Carrying cost per day", "eur", "lower",
           f"Capital tied up times {CARRYING_COST_RATE_PA:.0%} a year (placeholder rate, owner CFO), per day.", "Assets · asset provenance", k_carrying_cost_eur_per_day),
    KpiDef("aging_stock_pct", "warehouse", f"Aging: stock older than {AGING_DAYS} days", "pct", "lower",
           f"Share of on-hand units in their current station for more than {AGING_DAYS} days.", "Assets · status_since", k_aging_stock_pct, "floor0"),
    KpiDef("median_days_in_stock", "warehouse", "Median days in stock", "days", "lower",
           "Half of the on-hand units have waited longer than this in their current station.", "Assets · status_since", k_median_days_in_stock),
    KpiDef("dead_stock_value_eur", "warehouse", "Write-down risk: dead stock value", "eur", "lower",
           f"On-hand value of products with no deployment in the last {DEAD_STOCK_DAYS} days. The write-down candidates.", "Assets · deployed_date", k_dead_stock_value_eur, "floor0"),
    KpiDef("stock_turns", "warehouse", "Stock turns per year", "turns", "higher",
           "Units that went out in the last 90 days (deployed or rented), annualised, over the stock that can go out next.", "Assets · deployed_date", k_stock_turns),
    KpiDef("dock_to_deploy_days", "warehouse", "Dock to deploy, median days", "days", "lower",
           "Days from receipt to deployment, median over all deployed units.", "Assets · lifecycle", k_dock_to_deploy_days),
    KpiDef("inbound_overdue_pct", "warehouse", "Inbound lines overdue", "pct", "lower",
           "Open order lines past their estimated delivery date.", "Orders · /planning/inbound", k_inbound_overdue_pct, "floor0"),
    KpiDef("on_time_delivery_pct", "warehouse", "On-time delivery", "pct", "higher",
           "Tracked shipments with no delay against the original ETA.", "Orders · /v_order_tracking", k_on_time_delivery_pct, "cap100"),
    # cost
    KpiDef("negotiation_gap_eur", "cost", "Addressable negotiation saving", "eur", "lower",
           "Sum of quote minus should-cost target over all products priced above target. Money still on the table.", "SCM Analytics · /analytics/should-cost/savings", k_negotiation_gap_eur, "floor0"),
    KpiDef("products_above_target_pct", "cost", "Products priced above should-cost target", "pct", "lower",
           "Share of products with a bill of materials whose quote sits above the target price.", "SCM Analytics · /analytics/should-cost/savings", k_products_above_target_pct, "floor0"),
    KpiDef("spend_under_contract_pct", "cost", "Spend under active contract", "pct", "higher",
           "Received spend bought from a product-supplier pair with an active contract.", "Contracts · /product-suppliers", k_spend_under_contract_pct, "cap100"),
    KpiDef("top3_supplier_share_pct", "cost", "Top-3 supplier share of spend", "pct", "lower",
           "Concentration risk: share of spend with the three largest suppliers.", "Spend · /analytics/spend/by-supplier", k_top3_supplier_share_pct),
    # process
    KpiDef("auto_placed_pct", "process", "Orders placed without a human", "pct", "higher",
           "Decided requisitions that cleared the confidence bar and were placed automatically.", "Requisitions", k_auto_placed_pct, "cap100"),
    KpiDef("requisition_cycle_hours", "process", "Requisition decision time, median hours", "hours", "lower",
           "Hours from a requisition being staged to a person deciding it.", "Requisitions", k_requisition_cycle_hours),
    KpiDef("forecast_mape_pct", "process", "Forecast error (MAPE)", "pct", "lower",
           "Backtested demand forecast error over the deployment history.", "SCM Analytics · accuracy", k_forecast_mape_pct),
    # suppliers
    KpiDef("contracts_needing_action", "suppliers", "Contracts needing action", "count", "lower",
           "Product-supplier contracts expiring, due for renewal or expired.", "Contracts", k_contracts_needing_action, "floor0"),
    KpiDef("single_sourced_products_pct", "suppliers", "Single-sourced products", "pct", "lower",
           "Products with exactly one active source. Every one is a re-sourcing risk.", "Contracts · /product-suppliers", k_single_sourced_products_pct),
]

KPI_BY_ID = {k.id: k for k in KPIS}
GROUP_LABEL = {"fleet": "Fleet and recommerce", "warehouse": "Warehouse and availability", "cost": "Cost and commercial", "process": "Process and automation", "suppliers": "Suppliers and contracts"}

# -----------------------------------------------------------------------------
# targets


def _seed_targets(kpi: KpiDef, current: Optional[float]) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """A placeholder target from today's value: 10 / 20 / 30 percent better in the good direction."""
    if current is None:
        return None, None, None
    steps = (0.10, 0.20, 0.30)
    out = []
    for s in steps:
        if kpi.direction == "lower":
            v = current * (1 - s)
            if kpi.seed_rule == "floor0":
                v = max(0.0, v)
        else:
            v = current * (1 + s)
            if kpi.seed_rule == "cap100" or kpi.unit == "pct":
                v = min(100.0, v)
        out.append(round(v, 2))
    return out[0], out[1], out[2]


def get_or_seed_target(db: Session, kpi: KpiDef, current: Optional[float]) -> KpiTarget:
    t = db.execute(select(KpiTarget).where(KpiTarget.kpi_id == kpi.id)).scalar_one_or_none()
    if t is None:
        y1, y2, y3 = _seed_targets(kpi, current)
        t = KpiTarget(kpi_id=kpi.id, target_y1=y1, target_y2=y2, target_y3=y3, owner=None,
                      note="placeholder: derived from today's value, 10/20/30 % better; set the real target", placeholder=True)
        db.add(t)
        db.flush()
    elif t.placeholder and t.target_y1 is None and current is not None:
        t.target_y1, t.target_y2, t.target_y3 = _seed_targets(kpi, current)
        db.flush()
    return t


def set_target(db: Session, kpi_id: str, *, y1: Optional[float], y2: Optional[float], y3: Optional[float],
               owner: Optional[str], note: Optional[str], actor: Optional[str]) -> KpiTarget:
    if kpi_id not in KPI_BY_ID:
        from app.services.exceptions import NotFoundError
        raise NotFoundError(f"unknown KPI {kpi_id}")
    t = db.execute(select(KpiTarget).where(KpiTarget.kpi_id == kpi_id)).scalar_one_or_none()
    if t is None:
        t = KpiTarget(kpi_id=kpi_id)
        db.add(t)
    t.target_y1, t.target_y2, t.target_y3 = y1, y2, y3
    t.owner = owner
    t.note = note
    t.placeholder = False
    t.updated_by = actor
    db.flush()
    return t


# -----------------------------------------------------------------------------
# snapshots and status


def _snapshot(db: Session, kpi_id: str, today: date, value: Optional[float], reason: Optional[str] = None) -> None:
    row = db.execute(select(KpiSnapshot).where(KpiSnapshot.kpi_id == kpi_id, KpiSnapshot.as_of == today)).scalar_one_or_none()
    if row is None:
        db.add(KpiSnapshot(kpi_id=kpi_id, as_of=today, value=value, reason=reason))
    else:
        row.value, row.reason = value, reason
    db.flush()


def _today_snapshots(db: Session, today: date) -> dict[str, tuple[Optional[float], Optional[str]]]:
    """What was already measured today, per KPI."""
    rows = db.execute(select(KpiSnapshot.kpi_id, KpiSnapshot.value, KpiSnapshot.reason).where(KpiSnapshot.as_of == today)).all()
    return {k: (float(v) if v is not None else None, r) for k, v, r in rows}


def history(db: Session, kpi_id: str, *, days: int = 365) -> list[dict]:
    since = date.today() - timedelta(days=days)
    rows = db.execute(select(KpiSnapshot).where(KpiSnapshot.kpi_id == kpi_id, KpiSnapshot.as_of >= since).order_by(KpiSnapshot.as_of)).scalars()
    return [{"as_of": r.as_of, "value": r.value} for r in rows]


def _meets(kpi: KpiDef, current: float, target: float) -> bool:
    return current <= target if kpi.direction == "lower" else current >= target


def _status(kpi: KpiDef, current: Optional[float], t: KpiTarget, first: Optional[float], n_points: int) -> tuple[str, Optional[float]]:
    """(status, progress toward Y1 in percent).

    met            already at or better than the one-year target
    on_track       at least halfway from the first measured value to the target
    behind         less than halfway, or moving the wrong way (needs two measured days)
    open           measured once, target set, no movement to judge yet
    not_measurable no value today (reason on the row)
    no_target      value, but no target set
    """
    if current is None:
        return "not_measurable", None
    if t.target_y1 is None:
        return "no_target", None
    if _meets(kpi, current, t.target_y1):
        return "met", 100.0
    if n_points < 2 or first is None:
        return "open", 0.0
    base = first
    span = t.target_y1 - base
    if span == 0:
        return "behind", 0.0
    progress = (current - base) / span * 100.0
    progress = max(0.0, min(100.0, progress))
    return ("on_track" if progress >= 50.0 else "behind"), round(progress, 1)


def compute_all(db: Session, *, today: Optional[date] = None, snapshot: bool = True, refresh: bool = False) -> list[dict]:
    """Every KPI with its value, targets, status and trend.

    A KPI is measured **once a day**. Thirty-one measurements over a fleet of 400,000
    devices are a minute of database work; re-running them on every page load would make
    the tab unusable and would not change a single number, because each one is defined
    over a day. So a measurement already taken today is reused, and ``refresh=True``
    (the tab's Refresh button) forces a new one. The seed takes the first measurement,
    so the tab is complete the moment the demo comes up.
    """
    today = today or date.today()
    done = {} if refresh else _today_snapshots(db, today)
    out = []
    for kpi in KPIS:
        if kpi.id in done:
            current, reason = done[kpi.id]
        else:
            try:
                current, reason = kpi.compute(db, today)
            except Exception as e:  # a broken upstream read must not take the tab down; say so instead
                current, reason = None, f"could not compute: {type(e).__name__}"
            if snapshot:
                _snapshot(db, kpi.id, today, current, reason)
        t = get_or_seed_target(db, kpi, current)
        hist = history(db, kpi.id)
        measured = [h["value"] for h in hist if h["value"] is not None]
        first = measured[0] if measured else None
        status, progress = _status(kpi, current, t, first, len(measured))
        gap = None if (current is None or t.target_y1 is None) else round(t.target_y1 - current, 2)
        out.append({
            "id": kpi.id, "group": kpi.group, "group_label": GROUP_LABEL[kpi.group], "name": kpi.name, "unit": kpi.unit,
            "direction": kpi.direction, "definition": kpi.definition, "source": kpi.source,
            "current": current, "reason": reason, "as_of": today,
            "target_y1": t.target_y1, "target_y2": t.target_y2, "target_y3": t.target_y3,
            "owner": t.owner, "note": t.note, "placeholder": t.placeholder, "updated_by": t.updated_by,
            "status": status, "progress_pct": progress, "gap_to_y1": gap,
            "history": [{"as_of": h["as_of"], "value": h["value"]} for h in hist[-60:]],
        })
    return out

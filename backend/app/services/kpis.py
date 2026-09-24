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

4. **The explanation comes from the same place the number comes from.** Every
   ``compute`` function carries, in the ``@explained`` decorator right above it, how the
   number is calculated in words a person can check, which tables and columns it reads,
   what it excludes or assumes, why it matters and what data the measurement needs. The
   words sit next to the lines they describe so a sceptic can read both, and they travel
   with the value through ``/kpis``; neither screen writes its own prose about a KPI.

The registry below is the single list. Adding a KPI means adding one entry with a
``compute`` function; the API, the seeding and the frontend follow.
"""
from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import date, datetime, timedelta, timezone
from statistics import median
from typing import Callable, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.catalog import ProductSupplier
from app.models.flow import DEPLOYABLE_STATUSES, WAREHOUSE_STATUSES, Asset, AssetStatus
from app.models.kpi import KpiSnapshot, KpiTarget
from app.models.procurement import OrderItem, PurchaseOrder
from app.models.rental import ContractStatus, RentalContract
from app.models.requisition import PurchaseRequisition, RequisitionStatus
from app.services import accuracy, analytics, contracts, costing_service, planning, timeshift, tracking
from app.services import fleet as fleet_svc

# -----------------------------------------------------------------------------
# registry


BASES = ("measured", "derived", "placeholder")


@dataclass(frozen=True)
class KpiExplain:
    """What a reader gets when they click a KPI. Written next to the compute function it
    describes, never in a screen, so the words and the code cannot drift apart.

    ``basis`` says how far the number is a fact of the tables:
      measured     every input is a row or a count of rows in the tables under ``reads``;
                   the constants involved are definitions (a window, a threshold) and the
                   caveats name them;
      derived      an input is estimated by a rule in code because the data does not
                   record it (a fixed allowance for stations already passed, today's stock
                   as a proxy for the average, a model's target price);
      placeholder  a design parameter with an owner enters the number (a rate, a service
                   level), so the figure changes the day that person sets the real value.
    Every field is required and non-empty: a KPI without one of them is an error at
    import time, not a blank on the screen.
    """
    basis: str          # measured | derived | placeholder
    calculation: str    # the arithmetic in words: what is divided by what, over which window, counted how
    reads: str          # the tables and columns actually read, as the code reads them
    caveats: str        # what it excludes or assumes, where that changes the reading
    why: str            # one sentence: what a bad number would mean to someone who does not know the domain
    needs: str          # what data the measurement needs; for a KPI that is not measurable, what would make it so


def explained(**words) -> Callable:
    """Attach the explanation to the compute function right below it.

    A decorator rather than six more registry fields, so the prose sits directly above
    the lines it claims to describe: whoever changes a window or a filter changes the
    words in the same screenful. ``KpiDef`` picks it up from the function.
    """
    ex = KpiExplain(**words)
    if ex.basis not in BASES:
        raise ValueError(f"basis must be one of {BASES}, not {ex.basis!r}")
    for f in fields(ex):
        if not str(getattr(ex, f.name)).strip():
            raise ValueError(f"explanation field {f.name!r} is empty")

    def wrap(fn):
        fn.explain = ex
        return fn
    return wrap


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
    explain: Optional[KpiExplain] = None   # taken from the compute function's @explained when not given

    def __post_init__(self):
        if self.explain is None:
            ex = getattr(self.compute, "explain", None)
            if ex is None:
                raise ValueError(f"KPI {self.id!r}: its compute function carries no @explained block")
            object.__setattr__(self, "explain", ex)


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

def _once(db: Session, key: tuple, read: Callable[[], object]):
    """One planning read per measurement. Two KPIs read the capacity flow and two the inventory
    position; each read is about a second on the full fleet, so ``compute_all`` clears this memo
    when it starts and the second KPI of a pair reuses the first one's read. Kept on the session,
    so a measurement never sees a read taken before its own writes."""
    memo = db.info.setdefault("kpis_memo", {})
    if key not in memo:
        memo[key] = read()
    return memo[key]


def _capacity_flow(db, today):
    return _once(db, ("capacity_flow", today), lambda: planning.capacity_flow(db, today=today))


def _inventory_position(db, today):
    return _once(db, ("inventory_position", today), lambda: planning.inventory_position(db, today=today))


@explained(
    basis="measured",
    calculation="Units standing in warehouse-type locations that have a capacity, plus the outstanding units of open "
                "purchase orders whose destination is one of those locations, divided by the sum of those locations' "
                "capacities, times 100. Outstanding is a line's quantity minus the receipts booked against it; open means "
                "the order is PENDING, APPROVED, PLACED or PARTIALLY_RECEIVED. Read from planning.capacity_flow, the same "
                "figure the over-order guard reads.",
    reads="location (location_type, capacity); asset (current_location_id); purchase_order (status, destination_id); "
          "order_item (quantity); receipt_item (quantity_received)",
    caveats="On hand is counted by location, not by status: every unit standing in a station counts whatever its state, "
            "and a location without a capacity is not counted at all. The capacity is the station's stored figure, the "
            "seed's design parameter until a person sets it (location.capacity_set_by says who). Inbound counts only "
            "orders that name a destination. At 85 % a location counts as critical (planning.capacity_diagnosis); the "
            "guard refuses an order that does not fit in the free space net of inbound, whatever the share. Answers "
            "'no warehouse capacity defined' when no warehouse location carries a capacity.",
    why="Above the capacity a delivery has nowhere to land; the share says how much of the warehouse is already spoken "
        "for by stock on the floor and orders on their way.",
    needs="At least one WAREHOUSE-type location with a capacity set.",
)
def k_capacity_committed_pct(db, today):
    f = _capacity_flow(db, today)
    if f["committed_pct"] is None:
        return None, "no warehouse capacity defined"
    return round(f["committed_pct"] * 100, 1), None


@explained(
    basis="measured",
    calculation=f"Units standing in warehouse-type locations with a capacity (the same on hand as Warehouse committed), "
                f"divided by the daily outflow, divided by 7. The daily outflow is the sum over products of the units with a "
                f"deployed_date in the last {settings.demand_window_days} days divided by {settings.demand_window_days}; in "
                f"the fleet, deployed_date is the start of a device's latest rental, so the outflow is rentals started.",
    reads="location (location_type, capacity); asset (current_location_id, status, product_id, deployed_date)",
    caveats="The outflow counts only products that currently have deployable stock (RECEIVED, IN_STORAGE, READY_SECOND) "
            "or units on order, the rows of the inventory plan; a product with deployments but neither is not in the sum. "
            "The numerator counts every unit standing in a capacity-bearing location, the return chain included, so the "
            "cover is against everything on the floor, not only what can go out next. A flat rate over the window, not "
            "the recency-weighted rate the forecast uses. Answers 'no deployments in the trailing window' when the "
            "outflow is zero.",
    why="Weeks of cover is how long the warehouse can supply customers before the next delivery has to land; too few "
        "weeks is a stock-out, too many is capital standing still.",
    needs=f"At least one WAREHOUSE-type location with a capacity, and at least one deployed_date in the last "
          f"{settings.demand_window_days} days on a product that has stock or an open order.",
)
def k_weeks_of_cover(db, today):
    f = _capacity_flow(db, today)
    if f["weeks_of_cover"] is None:
        return None, "no deployments in the trailing window, burn rate unknown"
    return round(float(f["weeks_of_cover"]), 1), None


def _pos(row, key, default=None):
    """inventory_position returns PositionRow dataclasses; be tolerant to dicts too."""
    return row.get(key, default) if isinstance(row, dict) else getattr(row, key, default)


@explained(
    basis="measured",
    calculation=f"The number of products in the inventory position (planning.inventory_position) whose cover runs out "
                f"before their inbound lands: cover days is the deployable units on hand (RECEIVED, IN_STORAGE, "
                f"READY_SECOND) divided by the daily burn (units with a deployed_date in the last "
                f"{settings.demand_window_days} days divided by {settings.demand_window_days}), compared with the days "
                f"until the earliest open order line's estimated delivery date. Counted when cover days is smaller.",
    reads="asset (status, product_id, deployed_date); purchase_order (status); order_item (product_id, quantity, "
          "estimated_delivery_date); receipt_item (quantity_received); product (product_code)",
    caveats="A product with no open inbound at all is never at risk here, whatever its cover: the rule "
            "(recovery.recover_line) needs a burn, units on order and an ETA before it fires, so the products in the "
            "worst position, dry with nothing ordered, are not in this count. Products whose code starts with TCO- or "
            "SCN- (analytics fixtures) are excluded. A count, so zero is a real zero once the plan has rows. Answers "
            "'no products in the plan' when the position is empty.",
    why="Each product counted will run dry before its replenishment arrives; the number is the list a buyer has to "
        "expedite or bridge this week.",
    needs="Products with deployable stock or open orders, a deployment history for the burn, and an estimated delivery "
          "date on the open line.",
)
def k_items_at_risk(db, today):
    rows = _inventory_position(db, today)
    if not rows:
        return None, "no products in the plan"
    return float(sum(1 for r in rows if _pos(r, "at_risk"))), None


@explained(
    basis="derived",
    calculation="Over the products in the inventory position whose safety stock is above zero: those holding at least "
                "that many deployable units (RECEIVED, IN_STORAGE, READY_SECOND), divided by all of them, times 100.",
    reads="asset (status, product_id, deployed_date); product_supplier (active, preference_rank, "
          "standard_lead_time_days, contract_price); purchase_order (status); order_item (product_id, quantity, "
          "estimated_delivery_date); receipt_item (quantity_received)",
    caveats=f"The safety stock is derived by a rule, not a number anyone typed per product: z(service level) times the "
            f"standard deviation of demand over one lead time, from the deployments of the last "
            f"{planning._VARIABILITY_WINDOW_DAYS} days bucketed by the preferred source's lead time, with the service "
            f"level set by ABC class (A {settings.abc_service_level_a:.0%}, B {settings.abc_service_level_b:.0%}, "
            f"C {settings.abc_service_level_c:.0%}, from settings); when the statsforecast engine is configured an "
            f"intermittent product takes the larger of that and a conformal buffer. A product with no lead time or no "
            f"demand variability has a safety stock of zero and is left out of the share. Products whose code starts "
            f"with TCO- or SCN- are excluded. Answers 'no product carries a safety stock yet' when nothing qualifies.",
    why="Safety stock is the buffer against demand and lead-time surprises; the share of products holding it is the "
        "share where a surprise does not become a stock-out.",
    needs="Products with deployable stock or open orders, a preferred active source with a lead time, and a "
          "deployment history with some variability.",
)
def k_safety_stock_coverage_pct(db, today):
    rows = [r for r in _inventory_position(db, today) if (_pos(r, "safety_stock") or 0) > 0]
    if not rows:
        return None, "no product carries a safety stock yet"
    ok = sum(1 for r in rows if (_pos(r, "on_hand") or 0) >= _pos(r, "safety_stock"))
    return round(ok / len(rows) * 100, 1), None


@explained(
    basis="measured",
    calculation="The sum of the order line unit_price over every unit physically in the warehouse (status RECEIVED, "
                "IN_STORAGE, RETURNED, MDM_RELEASE, WIPE_GRADING, REPAIR, REFURB, READY_SECOND, SELLABLE or SWAP_BUFFER) "
                "that traces to an order line with a price. One unit, one price: the price that serial was bought at. "
                "No window.",
    reads="asset (status, source_order_item_id); order_item (unit_price)",
    caveats="Purchase price, not book value: no depreciation, no grade, no age. A unit without an order line or "
            "without a price adds nothing, so the value is a floor where provenance is incomplete. A returned device "
            "counts at its original purchase price however many rentals it has already earned. Answers 'nothing on "
            "hand' or 'on-hand units carry no order price'.",
    why="This is the money standing on the warehouse floor instead of earning rent; every day it stays costs capital, "
        "and every unit of it is a purchase that has not paid off yet.",
    needs="Units in a warehouse status that trace to an order line with a unit_price.",
)
def k_stock_value_eur(db, today):
    value, priced, total = _stock_value(db)
    if total == 0:
        return None, "nothing on hand"
    if priced == 0:
        return None, "on-hand units carry no order price"
    return round(value, 2), None


@explained(
    basis="placeholder",
    calculation=f"Capital tied up in stock (the same read as that KPI) times {CARRYING_COST_RATE_PA:.0%} a year, "
                f"divided by 365.",
    reads="asset (status, source_order_item_id); order_item (unit_price)",
    caveats=f"The {CARRYING_COST_RATE_PA:.0%} is a placeholder rate written in code (CARRYING_COST_RATE_PA), owner "
            f"CFO: the figure is the stock value scaled by that rate and moves with it. Cost of capital only, no rent, "
            f"handling, insurance or shrinkage. Inherits every caveat of the stock value, purchase price and incomplete "
            f"provenance included. Answers 'nothing on hand' or 'on-hand units carry no order price'.",
    why="It turns the stock value into a daily bill, so a week of a stalled station or a late sale has a price a CFO "
        "recognises.",
    needs="A measurable stock value, and the rate set by its owner before the figure is quoted as a cost.",
)
def k_carrying_cost_eur_per_day(db, today):
    value, priced, total = _stock_value(db)
    if total == 0:                       # say which is missing: the stock, or the prices on it
        return None, "nothing on hand"
    if priced == 0:
        return None, "on-hand units carry no order price"
    return round(value * CARRYING_COST_RATE_PA / 365.0, 2), None


@explained(
    basis="measured",
    calculation=f"Over every unit in a warehouse status with a waiting date: those waiting more than {AGING_DAYS} "
                f"days, divided by all of them, times 100. The waiting date is status_since (when the unit entered "
                f"its current station), or received_date where status_since is empty; days waited is today minus that "
                f"date.",
    reads="asset (status, status_since, received_date)",
    caveats=f"Age in the current station, not since purchase: a device that moved from repair to second-life stock "
            f"yesterday is one day old here. Where status_since is missing the receipt date stands in, which is age "
            f"since arrival instead. The {AGING_DAYS}-day line is a definition constant in code (AGING_DAYS), the same "
            f"for every station, not the per-compartment target dwell the Warehouse tab uses. Units with neither date "
            f"are left out of both sides. Answers 'on-hand units have no receipt date' when no unit has a date.",
    why="Stock past the line in one station is stock that has stopped moving; the share says how much of the "
        "warehouse is queue rather than flow.",
    needs="Units in a warehouse status with a status_since or a received_date.",
)
def k_aging_stock_pct(db, today):
    hist = _waiting_histogram(db, today)
    total = sum(hist.values())
    if not total:
        return None, "on-hand units have no receipt date"
    return round(sum(c for d, c in hist.items() if d > AGING_DAYS) / total * 100, 1), None


@explained(
    basis="measured",
    calculation="The median of the days waited over every unit in a warehouse status with a waiting date "
                "(status_since, or received_date where that is empty): the smallest day count at which half of the "
                "units are at or below it, read off a histogram grouped by date, which is exact.",
    reads="asset (status, status_since, received_date)",
    caveats="The same waiting date as the aging share, so it is the age in the current station, not since purchase, "
            "with the receipt date standing in where status_since is empty. A median, not a mean: a few very old "
            "units do not move it; the aging share and the Warehouse tab's oldest unit show those. Answers 'on-hand "
            "units have no receipt date' when no unit has a date.",
    why="Half the warehouse has waited longer than this in its station; it is the typical wait a device sees, where "
        "the aging share is the tail.",
    needs="Units in a warehouse status with a status_since or a received_date.",
)
def k_median_days_in_stock(db, today):
    m = _median_from_histogram(_waiting_histogram(db, today))
    if m is None:
        return None, "on-hand units have no receipt date"
    return m, None


@explained(
    basis="measured",
    calculation=f"The sum of the order line unit_price over units in a warehouse status whose product has had no unit "
                f"with a deployed_date in the last {DEAD_STOCK_DAYS} days; in the fleet, a deployed_date is a rental "
                f"start. Units without a priced order line add nothing.",
    reads="asset (status, product_id, deployed_date, source_order_item_id); order_item (unit_price)",
    caveats=f"The test is per product, not per unit: one rental of a model in the last {DEAD_STOCK_DAYS} days clears "
            f"every unit of it, and a model nobody has rented for that long is counted whole, units that arrived "
            f"yesterday included. Purchase price, not what the stock would fetch. Every warehouse status counts, so a "
            f"returned device waiting for grading is dead stock if its model has not gone out for {DEAD_STOCK_DAYS} "
            f"days. Answers 'nothing on hand' or 'on-hand units carry no order price'.",
    why="This is the value a write-down would have to cover: stock of models that have stopped moving, which no "
        "reorder decision should add to.",
    needs="Units in a warehouse status that trace to a priced order line, and a deployed_date history to tell moving "
          "models from still ones.",
)
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


@explained(
    basis="derived",
    calculation="Units with a deployed_date in the last 90 days up to today (in the fleet: rentals started), times 365 "
                "divided by 90 to annualise, divided by the units on hand today that can go out next (RECEIVED, "
                "IN_STORAGE, READY_SECOND).",
    reads="asset (status, deployed_date)",
    caveats="Today's on hand stands in for the average stock over the window, which is why the figure is derived: a "
            "warehouse that filled up last week shows fewer turns than it made. The denominator leaves out the return "
            "chain, sellable stock and the swap buffer, because none of them can go out next. A unit is counted by its "
            "current deployed_date, so a device rented in the window and already back still counts as one that went "
            "out. Answers 'nothing on hand' or 'no deployments in the last 90 days'.",
    why="Turns say how many times a year the deployable stock is replaced by rentals; low turns mean devices wait for "
        "customers, high turns that stock is tight.",
    needs="Deployable units on hand and at least one deployed_date in the last 90 days.",
)
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


@explained(
    basis="measured",
    calculation="Over units received in the last 365 days that have both a received_date and a deployed_date: the "
                "median of deployed_date minus received_date, in days. Units whose deployment date lies before their "
                "receipt date are left out.",
    reads="asset (received_date, deployed_date)",
    caveats="A window by receipt date, so a fleet that changed its intake is judged on this year's units, not on "
            "units it took in three years ago. In the fleet, deployed_date is the start of the device's latest rental, "
            "so a device received this year and already on its second rental counts its whole first cycle as dock to "
            "deploy. Units still waiting are not in it: this is the time of the units that went out, and a queue that "
            "is growing does not move it. Answers 'no unit received in the last year has both a receipt and a "
            "deployment date'.",
    why="Days from dock to deploy are days between paying for a device and earning from it; the median is the "
        "intake's typical speed.",
    needs="Units received in the last 365 days with both a received_date and a deployed_date.",
)
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


@explained(
    basis="measured",
    calculation="Over the open order lines with units still outstanding: those whose estimated_delivery_date lies "
                "before today, divided by all of them, times 100. Open means the order is PENDING, APPROVED, PLACED or "
                "PARTIALLY_RECEIVED; outstanding is the line's quantity minus the receipts booked against it.",
    reads="purchase_order (status); order_item (quantity, estimated_delivery_date); receipt_item (quantity_received)",
    caveats="Counted by line, not by unit or by value: a late line of two units weighs as much as a line of two "
            "thousand. A line without an estimated delivery date sits in the denominator and is never overdue. A "
            "fully received line drops out even while its order is still open. Answers 'no open inbound lines' when "
            "nothing is outstanding.",
    why="An overdue line is a delivery the plan counted on that has not come; the share says how far the supplier "
        "side can be trusted for the weeks of cover.",
    needs="Open purchase orders with lines not yet fully received.",
)
def k_inbound_overdue_pct(db, today):
    rows = planning.inbound_pipeline(db, as_of=today)
    if not rows:
        return None, "no open inbound lines"
    return round(sum(1 for r in rows if r["overdue"]) / len(rows) * 100, 1), None


@explained(
    basis="measured",
    calculation="Over every shipment in the control tower's tracking tables: those whose current ETA is not later "
                "than the original ETA (delay days at or below zero), divided by all shipments, times 100.",
    reads="trk_shipment (eta_original, eta_current); trk_purchase_order (supplier_id); trk_supplier",
    caveats="The control tower's own tables (the trk_ prefix), not the purchase orders the other KPIs read, and "
            "every shipment they hold, delivered or still moving, with no window. A shipment missing either ETA has a "
            "delay of zero and counts as on time. Promise against current promise: a shipment that slipped and then "
            "landed on its revised date still counts as late. Answers 'no tracked shipments' when the tables are "
            "empty.",
    why="The share of shipments holding their first promise is what the lead times in the plan are worth; when it "
        "falls, every ETA in the inbound pipeline is optimistic.",
    needs="Shipments in the tracking tables, each joined to its tracked order and supplier.",
)
def k_on_time_delivery_pct(db, today):
    rows = tracking.order_tracking(db)
    if not rows:
        return None, "no tracked shipments"
    ok = sum(1 for r in rows if (r.get("delay_days") or 0) <= 0)
    return round(ok / len(rows) * 100, 1), None


# ---- cost and commercial

_BOM_NEEDS = ("A bill of materials per product, written through PUT /products/{id}/bom: bom_line rows each with a "
              "component class, and for teardown lines a commodity price series as of today; plus a contract_price "
              "on the product's preferred active supplier to compare against. The reason 'no product has a bill of "
              "materials yet' means the bom table is empty in this database; the first BOM makes both cost KPIs "
              "measurable.")


@explained(
    basis="derived",
    calculation="For every product with a bill of materials, the should-cost target is rolled up (material, "
                "conversion and overhead per line with commodity index multipliers as of today, then integration, "
                "SG&A and margin from the BOM's parameters) and compared with the preferred active supplier's "
                "contract_price. The KPI is the sum, over the products whose quote is above the target, of quote minus "
                "target, per unit.",
    reads="bom, bom_line, component_class, commodity, commodity_price, cost_params; product_supplier (active, "
          "preference_rank, contract_price)",
    caveats="Per unit, not per year: no volume is multiplied in, so it is the sum of the per-unit gaps across "
            "products, not the money a year's buying would save. A product whose BOM cannot be costed (a teardown "
            "line without a commodity price as of today) is skipped, and one with a BOM but no priced active source "
            "has no gap. The target is a model output, which is why the figure is derived; where a BOM sets no "
            "parameters the defaults are integration 6 %, SG&A 8 %, margin 10 %. Answers 'no product has a bill of "
            "materials yet' when the bom table is empty.",
    why="It is how far the quotes sit above a defensible cost floor, the case a buyer takes into the next "
        "negotiation.",
    needs=_BOM_NEEDS,
)
def k_negotiation_gap_eur(db, today):
    s = costing_service.savings_summary(db, today)
    if not s["products_with_bom"]:
        return None, "no product has a bill of materials yet"
    return round(float(s["total_gap_to_target"]), 2), None


@explained(
    basis="derived",
    calculation="Products with a bill of materials whose preferred active supplier's contract_price is above the "
                "should-cost target, divided by all products with a bill of materials that could be costed, times 100.",
    reads="bom, bom_line, component_class, commodity, commodity_price, cost_params; product_supplier (active, "
          "preference_rank, contract_price)",
    caveats="A product with a BOM but no priced active source is in the denominator and never above target; a "
            "product whose BOM cannot be costed as of today is not in either. The target is the same model output "
            "as the negotiation gap, so the two move together. Answers 'no product has a bill of materials yet' when "
            "the bom table is empty.",
    why="The share of the modelled catalogue bought above its cost floor; the gap KPI says how much, this one says "
        "how widespread.",
    needs=_BOM_NEEDS,
)
def k_products_above_target_pct(db, today):
    s = costing_service.savings_summary(db, today)
    if not s["products_with_bom"]:
        return None, "no product has a bill of materials yet"
    return round(s["products_above_target"] / s["products_with_bom"] * 100, 1), None


@explained(
    basis="measured",
    calculation="Received spend is the order line unit_price summed over every unit that traces to an order line, "
                "grouped by product and supplier, all time. The KPI is the spend of the (product, supplier) pairs whose "
                "product_supplier row derives the status ACTIVE today, divided by all received spend, times 100.",
    reads="asset (source_order_item_id, product_id); order_item (unit_price); purchase_order (supplier_id); product "
          "(product_code); product_supplier (product_id, supplier_id, contract_status, active, term_start, term_end)",
    caveats="ACTIVE is strict: a stored contract_status wins; otherwise an inactive row is EXPIRED, one without term "
            "dates DRAFT, one ending within 30 days EXPIRING, within 60 days RENEWAL_DUE, and only the rest ACTIVE. So "
            "spend on a contract in its last two months counts as not under contract. Spend is all time at purchase "
            "price, every unit ever received with provenance; products whose code starts with TCO- or SCN- are "
            "excluded, and a unit without an order line is not spend. Answers 'no received spend with provenance' or "
            "'received spend has no prices'.",
    why="Spend outside an active contract is bought at whatever price the day gave; the share is how much of the "
        "money sits on agreed terms.",
    needs="Received units tracing to priced order lines, and product_supplier rows with term dates or a stored "
          "status.",
)
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


@explained(
    basis="measured",
    calculation="Received spend per supplier (the order line unit_price summed over every unit that traces to an "
                "order line, all time), sorted largest first; the three largest divided by the total, times 100.",
    reads="asset (source_order_item_id); order_item (unit_price); purchase_order (supplier_id); product (product_code)",
    caveats="All time, not a year: the cockpit's Spend tab can slice by year, this figure cannot. Purchase price per "
            "received unit, so an order not yet received is not spend. Products whose code starts with TCO- or SCN- "
            "are excluded, and with them the synthetic vendor that supplies only those. Answers 'no spend recorded' "
            "or 'spend is zero'.",
    why="Three suppliers holding most of the spend is a concentration a price rise or an outage can exploit; the "
        "share is that exposure in one number.",
    needs="Received units tracing to priced order lines on orders with a supplier.",
)
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

@explained(
    basis="measured",
    calculation="Over requisitions with status PLACED or REJECTED that carry a decided_at or the auto_placed flag: "
                "those with auto_placed true, divided by all of them, times 100. All time, no window.",
    reads="purchase_requisition (status, decided_at, auto_placed)",
    caveats="A requisition is auto-placed when its calibrated confidence cleared the bar in the purchasing run; the "
            "bar itself learns from approvals and rejections, so the share can move without the demand changing. "
            "Staged requisitions waiting in the cart are not decided and not counted. Answers 'no requisition decided "
            "yet' when nothing has been placed or rejected.",
    why="The share of buys the system placed without a person is the automation the agent actually delivers; the "
        "rest is what a buyer still has to sign.",
    needs="Requisitions staged by the purchasing run (POST /requisitions/run) and then placed by the gate or approved "
          "or rejected by a person. The reason 'no requisition decided yet' means none has reached PLACED or REJECTED "
          "in this database; one decision, by the gate or by a person, makes it measurable.",
)
def k_auto_placed_pct(db, today):
    reqs = list(db.execute(select(PurchaseRequisition).where(PurchaseRequisition.status.in_([RequisitionStatus.PLACED, RequisitionStatus.REJECTED]))).scalars())
    decided = [r for r in reqs if r.decided_at is not None or r.auto_placed]
    if not decided:
        return None, "no requisition decided yet"
    return round(sum(1 for r in decided if r.auto_placed) / len(decided) * 100, 1), None


@explained(
    basis="measured",
    calculation="Over requisitions decided by a person (decided_at set and auto_placed false): the hours from "
                "date_created to decided_at, and the median of those. Rows where the decision stamp precedes the "
                "creation stamp are left out. All time, no window.",
    reads="purchase_requisition (date_created, decided_at, auto_placed)",
    caveats="Auto-placed requisitions are excluded on purpose: the gate decides them in the same run that stages "
            "them, at about zero hours, and would drag the median to nothing. Both stamps are wall-clock and do not "
            "move with the simulation's calendar. A median, so one requisition that waited a month does not move it. "
            "Answers 'no requisition decided by a person yet'.",
    why="Hours between the system proposing a buy and a person deciding it are the human latency in the loop; if "
        "the gate is fast and this is slow, the cart is the bottleneck.",
    needs="Requisitions approved or rejected by a person, through the Requisitions tab or the API; one the gate "
          "placed on its own does not count. The reason 'no requisition decided by a person yet' means no such row "
          "exists in this database; the first human approval or rejection makes it measurable.",
)
def k_requisition_cycle_hours(db, today):
    # Only a person's decisions. The gate writes decided_at too, in the same run that staged
    # the requisition, and until 24.09.2026 those rows were counted: a median of hours "to a
    # person deciding" that the agent's own zero-hour decisions could pull to nothing.
    reqs = db.execute(select(PurchaseRequisition.date_created, PurchaseRequisition.decided_at)
                      .where(PurchaseRequisition.decided_at.is_not(None), PurchaseRequisition.auto_placed.is_(False))).all()
    hours = [(d - c).total_seconds() / 3600.0 for c, d in reqs if d and c and d >= c]
    if not hours:
        return None, "no requisition decided by a person yet"
    return round(float(median(hours)), 1), None


@explained(
    basis="derived",
    calculation=f"A backtest: standing at month-spaced dates across the last {accuracy.BACKTEST_MAX_DAYS} days of "
                f"deployment history, the demand forecast (a recency-weighted rate over the trailing "
                f"{settings.demand_window_days} days, {settings.demand_halflife_days}-day half-life, times a "
                f"{settings.demand_horizon_days}-day horizon, plus end-of-life replacements) is run per product and "
                f"compared with the units actually deployed in the following {settings.demand_horizon_days} days. Per "
                f"row the absolute percentage error is |predicted minus actual| over actual; the KPI is the plain mean "
                f"of those, times 100.",
    reads="asset (product_id, deployed_date, status); product (product_code); product_supplier (active, "
          "preference_rank, min_order_quantity, standard_lead_time_days); purchase_order (status); order_item "
          "(product_id, quantity); receipt_item (quantity_received)",
    caveats="Rows where nothing was deployed (actual zero) have no percentage error and are excluded, so the products "
            "that went quiet do not count against the forecast. An unweighted mean: a small product's 300 % miss "
            "weighs as much as a large one's 5 %, which is why the cockpit's Forecast Accuracy (1 minus WMAPE, "
            "volume-weighted) can disagree with it. A backtest of the configured method on history, not the error "
            "of a forecast anyone acted on. In the fleet, deployed means a rental started. Answers 'not enough "
            "deployment history for a backtest' when the history is too short.",
    why="The forecast sizes safety stock and the purchasing run; its error is how much of that stock is insurance "
        "against the forecast itself.",
    needs=f"At least {settings.demand_window_days + settings.demand_horizon_days} days of deployed_date history: a "
          f"{settings.demand_window_days}-day window to forecast from plus a {settings.demand_horizon_days}-day "
          f"horizon to score against.",
)
def k_forecast_mape_pct(db, today):
    s = accuracy.accuracy_summary(db)
    if s.get("mape") is None:
        return None, "not enough deployment history for a backtest"
    return round(float(s["mape"]) * 100, 1), None


# ---- suppliers and contracts

@explained(
    basis="measured",
    calculation="The number of product_supplier rows whose derived status is EXPIRING, RENEWAL_DUE or EXPIRED: a "
                "stored contract_status wins; otherwise an inactive row is EXPIRED, a term_end in the past EXPIRED, "
                "within 30 days EXPIRING, within 60 days RENEWAL_DUE. A count, never null: zero means nothing needs "
                "action.",
    reads="product_supplier (contract_status, active, term_start, term_end)",
    caveats="Every inactive row counts as expired and needing action, sources switched off on purpose included; a "
            "row with a stored status such as SUPERSEDED does not. Rows without term dates are DRAFT and not counted. "
            "One row per product and supplier, so one framework agreement covering five products is five contracts "
            "here.",
    why="Each one is a contract that lapses or has lapsed while spend may still flow through it; the count is the "
        "renewal work due in the next two months.",
    needs="product_supplier rows with term dates or a stored contract_status; on an empty catalogue it measures as "
          "zero.",
)
def k_contracts_needing_action(db, today):
    n = 0
    for ps in db.execute(select(ProductSupplier)).scalars():
        if contracts.derive_status(ps, today=today) in ("EXPIRING", "RENEWAL_DUE", "EXPIRED"):
            n += 1
    return float(n), None


@explained(
    basis="measured",
    calculation="Over products that have at least one product_supplier row with active true: those with exactly one, "
                "divided by all of them, times 100.",
    reads="product_supplier (product_id, active)",
    caveats="A product with no active source at all is not in the denominator, so it does not show as a risk here "
            "although it is a larger one. Active is the row's flag, not the derived contract status: an expired but "
            "still active row counts as a source. Answers 'no active sources' when the table has none.",
    why="A product with one source has no fallback when that supplier fails or raises its price; the share is how "
        "much of the catalogue is exposed.",
    needs="product_supplier rows with active true.",
)
def k_single_sourced_products_pct(db, today):
    rows = db.execute(select(ProductSupplier.product_id, func.count()).where(ProductSupplier.active.is_(True)).group_by(ProductSupplier.product_id)).all()
    if not rows:
        return None, "no active sources"
    return round(sum(1 for _, c in rows if c == 1) / len(rows) * 100, 1), None


# ---- fleet and recommerce (DaaS scenario; each says "no rental fleet" in the datacenter scenario)

def _daas(db) -> bool:
    return fleet_svc.scenario(db) == "daas"


_NO_FLEET = ("Answers 'no rental fleet in this database' when no device has status RENTED, which is the datacenter "
             "scenario.")


@explained(
    basis="measured",
    calculation="Devices with status RENTED and a cycle_no of 2 or more, divided by all devices with status RENTED, "
                "times 100. A count at the moment of measurement, no window.",
    reads="asset (status, cycle_no)",
    caveats="Counts devices, not contracts, each on its current cycle. Growth dilutes the share: every new first "
            "rental lowers it without anything going wrong in the return chain, so read it next to the fleet's "
            "growth. " + _NO_FLEET,
    why="Every device on a second rental earns rent without a new purchase; a share that stays low means returns "
        "are not coming back into service.",
    needs="At least one device with status RENTED; the share is then always measurable.",
)
def k_second_rental_share_pct(db, today):
    if not _daas(db):
        return None, "no rental fleet in this database"
    rented = db.scalar(select(func.count(Asset.id)).where(Asset.status == AssetStatus.RENTED)) or 0
    c2 = db.scalar(select(func.count(Asset.id)).where(Asset.status == AssetStatus.RENTED, Asset.cycle_no >= 2)) or 0
    return round(c2 / rented * 100, 1), None


@explained(
    basis="measured",
    calculation="Over the rental contracts with status ENDED whose actual_end lies within the last 365 days: those "
                "with end_reason early, defect or swap, divided by all of them, times 100.",
    reads="rental_contract (status, actual_end, end_reason)",
    caveats="A contract with no end_reason counts as a planned end. The window is by actual_end and has no upper "
            "bound, so an actual_end dated in the future would be counted. Answers 'no contract ended in the last "
            "twelve months' when the window is empty. " + _NO_FLEET,
    why="Early, defect and swap returns come back before the rent has paid for the device and load the repair chain; "
        "a rising share is a product or customer problem before it is a warehouse problem.",
    needs="At least one contract ENDED with an actual_end in the last 365 days.",
)
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


@explained(
    basis="placeholder",
    calculation=f"Devices with status MDM_RELEASE and a status_since date: those waiting more than "
                f"{fleet_svc.MDM_RELEASE_SLA_DAYS} days (today minus status_since), divided by all of them, times 100.",
    reads="asset (status, status_since)",
    caveats=f"The {fleet_svc.MDM_RELEASE_SLA_DAYS}-day service level is a placeholder constant in code "
            f"(fleet.MDM_RELEASE_SLA_DAYS), owner Head of Customer Success (the MDM compartment's target owner in "
            f"warehouse.py); the share moves the day that person sets the real one. Devices in the station without "
            f"a status_since are left out of both sides. Answers 'nothing waiting for an MDM release' when the "
            f"station is empty. " + _NO_FLEET,
    why="A device locked in the old customer's device management cannot be wiped or rented again; the share says "
        "how much of the return chain waits on customers rather than on the warehouse.",
    needs="At least one device in MDM_RELEASE with a status_since date.",
)
def k_mdm_release_over_sla_pct(db, today):
    if not _daas(db):
        return None, "no rental fleet in this database"
    days = [(today - d).days for (d,) in db.execute(select(Asset.status_since).where(Asset.status == AssetStatus.MDM_RELEASE, Asset.status_since.is_not(None))).all()]
    if not days:
        return None, "nothing waiting for an MDM release"
    return round(sum(1 for d in days if d > fleet_svc.MDM_RELEASE_SLA_DAYS) / len(days) * 100, 1), None


@explained(
    basis="derived",
    calculation="For every device in RETURNED, MDM_RELEASE, WIPE_GRADING, REPAIR or REFURB with a status_since: the "
                "days in its current station (today minus status_since) plus a fixed allowance for the stations it "
                "has already passed (RETURNED 0, MDM_RELEASE 10, WIPE_GRADING 22, REPAIR 24, REFURB 30 days). The "
                "median of those sums over the devices still in the chain.",
    reads="asset (status, status_since)",
    caveats="Derived, not measured: the time a device actually spent in its earlier stations is not recorded (there "
            "is no movement log), so the allowance per station, written in code, stands in for it, the same for every "
            "device. It is the age of work still in the chain, not the finished time of devices that came out of it, "
            "so it leans short while the chain fills. Devices without status_since are left out. Answers 'no device "
            "in the return chain' when the five stations are empty. " + _NO_FLEET,
    why="Days from return to ready are days a paid-for device earns nothing; the number sizes the second-life "
        "pipeline and the room the chain needs.",
    needs="At least one device in the five chain stations with a status_since date.",
)
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


@explained(
    basis="measured",
    calculation="Devices with status SELLABLE, divided by the devices with status SOLD whose sold_date lies within "
                "the last 90 days divided by 3 (the monthly sales pace). Months of stock at that pace.",
    reads="asset (status, sold_date)",
    caveats="The pace is the last 90 days only, treated as three months of 30 days; the window is by sold_date and "
            "has no upper bound. A device counts as sold by its sold_date whatever the channel or price. Answers 'no "
            "sale in the last 90 days' when the pace is zero, so a stock with nothing selling shows no reach rather "
            "than an infinite one. " + _NO_FLEET,
    why="Sellable devices lose value every month and tie up capital; a long reach means the resale channel is not "
        "keeping up with what grading clears.",
    needs="At least one device SOLD with a sold_date in the last 90 days.",
)
def k_sellable_reach_months(db, today):
    if not _daas(db):
        return None, "no rental fleet in this database"
    sellable = db.scalar(select(func.count(Asset.id)).where(Asset.status == AssetStatus.SELLABLE)) or 0
    sold_90 = db.scalar(select(func.count(Asset.id)).where(Asset.status == AssetStatus.SOLD, Asset.sold_date >= today - timedelta(days=90))) or 0
    if sold_90 == 0:
        return None, "no sale in the last 90 days"
    return round(sellable / (sold_90 / 3.0), 1), None


@explained(
    basis="measured",
    calculation="Devices with status SWAP_BUFFER, divided by the rental contracts with end_reason defect or swap "
                "whose actual_end lies within the last 90 days divided by 3 (the monthly defect pace). Months the "
                "buffer covers at that pace.",
    reads="asset (status); rental_contract (end_reason, actual_end)",
    caveats="The pace counts contracts, not devices, and only those ended for a defect or a swap; the window is by "
            "actual_end and has no upper bound, and a month is 30 days here. Answers 'no defect return in the last 90 "
            "days' when the pace is zero. " + _NO_FLEET,
    why="The buffer is the reserve a customer defect is served from; too few months means a defect waits for a "
        "device, too many is stock that could be renting.",
    needs="At least one contract ended with end_reason defect or swap in the last 90 days.",
)
def k_swap_buffer_months(db, today):
    if not _daas(db):
        return None, "no rental fleet in this database"
    buffer = db.scalar(select(func.count(Asset.id)).where(Asset.status == AssetStatus.SWAP_BUFFER)) or 0
    defects_90 = db.scalar(select(func.count()).select_from(RentalContract).where(RentalContract.end_reason.in_(("defect", "swap")), RentalContract.actual_end >= today - timedelta(days=90))) or 0
    if defects_90 == 0:
        return None, "no defect return in the last 90 days"
    return round(buffer / (defects_90 / 3.0), 1), None


@explained(
    basis="measured",
    calculation="Devices with status READY_SECOND, divided by the rental contracts with a cycle_no of 2 or more whose "
                "start_date lies within the last 90 days up to today, divided by 3 (the monthly pace of second "
                "rentals). Months of second-life stock at that pace.",
    reads="asset (status); rental_contract (cycle_no, start_date)",
    caveats="The pace is second rentals started, not devices refurbished, and a month is 30 days here. Only the last "
            "90 days count, so a pace that has just picked up or dropped shows at once. Answers 'no second rental "
            "started in the last 90 days' when the pace is zero. " + _NO_FLEET,
    why="Refurbished stock is capital spent twice; a long reach means second-life devices are not finding "
        "customers, a short one that refurbishment cannot keep up.",
    needs="At least one contract with a cycle_no of 2 or more started in the last 90 days.",
)
def k_second_life_reach_months(db, today):
    """Months the refurbished second-life stock lasts at the pace second rentals started in the last 90 days."""
    if not _daas(db):
        return None, "no rental fleet in this database"
    stock = db.scalar(select(func.count(Asset.id)).where(Asset.status == AssetStatus.READY_SECOND)) or 0
    started_90 = db.scalar(select(func.count()).select_from(RentalContract)
                           .where(RentalContract.cycle_no >= 2, RentalContract.start_date >= today - timedelta(days=90),
                                  RentalContract.start_date <= today)) or 0
    if started_90 == 0:
        return None, "no second rental started in the last 90 days"
    return round(stock / (started_90 / 3.0), 1), None


@explained(
    basis="measured",
    calculation="Over devices with status SOLD, a sold_date within the last 365 days, a sale_price and an order line "
                "with a unit_price: the sum of sale_price divided by the sum of those order lines' unit_price, times "
                "100. Serial by serial, through the order line each device was received against.",
    reads="asset (status, sold_date, sale_price, source_order_item_id); order_item (unit_price)",
    caveats="A sold device without a sale price or without a priced order line is left out of both sums. sale_price "
            "is the net proceeds after the channel fee, as the model records it; unit_price is the purchase price of "
            "that serial, not a list price. The window is by sold_date and has no upper bound. Answers 'no sale with "
            "a purchase price in the last twelve months' when nothing qualifies. " + _NO_FLEET,
    why="This is the share of the purchase price the second life gives back; it decides what a rental price has to "
        "cover and whether resale or recycling is the better exit.",
    needs="Devices sold in the last 365 days that carry a sale_price and trace to an order line with a unit_price.",
)
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


@explained(
    basis="measured",
    calculation="Devices with status RECYCLED whose sold_date lies within the last 365 days, divided by those plus "
                "the devices with status SOLD in the same window, times 100.",
    reads="asset (status, sold_date)",
    caveats="A recycled device's exit date is stored in the same sold_date column as a sale; a recycled device "
            "without one is not counted. Sold and recycled are the only exits counted, so a device decommissioned or "
            "disposed the datacenter way is in neither side. The window has no upper bound. Answers 'nothing left the "
            "fleet in the last twelve months' when both counts are zero. " + _NO_FLEET,
    why="Every recycled device brought back nothing; the share says how much of the fleet's exit value is lost to "
        "grade D and defects.",
    needs="At least one device SOLD or RECYCLED with a sold_date in the last 365 days.",
)
def k_recycling_share_pct(db, today):
    if not _daas(db):
        return None, "no rental fleet in this database"
    since = today - timedelta(days=365)
    sold = db.scalar(select(func.count(Asset.id)).where(Asset.status == AssetStatus.SOLD, Asset.sold_date >= since)) or 0
    rec = db.scalar(select(func.count(Asset.id)).where(Asset.status == AssetStatus.RECYCLED, Asset.sold_date >= since)) or 0
    if sold + rec == 0:
        return None, "nothing left the fleet in the last twelve months"
    return round(rec / (sold + rec) * 100, 1), None


@explained(
    basis="measured",
    calculation="The number of rental contracts with status RUNNING whose planned_end lies before today. A count, "
                "not a share, and never null in the fleet: zero means no contract is overdue.",
    reads="rental_contract (status, planned_end)",
    caveats="A contract is overdue from the day after its planned end; a device already back but not yet booked as "
            "ended still counts. A contract extended by writing a new planned_end drops out at once. Counts "
            "contracts, so it equals devices only while each device holds one running contract. " + _NO_FLEET,
    why="An overdue return is a device the next customer cannot have and rent that may not be paid; the count is "
        "the first sign the return process needs chasing.",
    needs="Running contracts with a planned_end; in the fleet it is always measurable.",
)
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
           "Days a returned device has spent in the return chain so far: measured in its current station, a fixed allowance for the stations before it.", "Warehouse · stations", k_return_to_ready_days),
    KpiDef("sellable_reach_months", "fleet", "Sellable stock reach, months", "count", "lower",
           "Months the sellable stock lasts at the sales pace of the last 90 days. Long reach is capital and aging.", "Recommerce · sales", k_sellable_reach_months),
    KpiDef("swap_buffer_months", "fleet", "Swap buffer reach, months", "count", "higher",
           "Months the swap buffer covers at the defect pace of the last 90 days.", "Warehouse · swap buffer", k_swap_buffer_months),
    KpiDef("second_life_reach_months", "fleet", "Second-life stock reach, months", "count", "lower",
           "Months the refurbished second-life stock lasts at the pace second rentals started in the last 90 days. Long reach is refurbished capital not earning rent.", "Warehouse · second-life stock", k_second_life_reach_months),
    KpiDef("resale_share_of_purchase_pct", "fleet", "Resale proceeds as share of purchase price", "pct", "higher",
           "Net sale proceeds of the last twelve months over what those devices cost to buy, serial by serial.", "Recommerce · sales and provenance", k_resale_share_of_purchase_pct),
    KpiDef("recycling_share_pct", "fleet", "Recycling share of fleet exits", "pct", "lower",
           "Devices recycled over devices sold plus recycled, last twelve months.", "Recommerce", k_recycling_share_pct, "floor0"),
    # warehouse
    KpiDef("capacity_committed_pct", "warehouse", "Warehouse committed", "pct", "lower",
           "On hand plus inbound as a share of warehouse capacity. At 85 % a location counts as critical; an order that does not fit in the free space is refused.", "Capacity · /planning/capacity-flow", k_capacity_committed_pct),
    KpiDef("weeks_of_cover", "warehouse", "Weeks of cover (availability)", "weeks", "higher",
           "How long today's on-hand lasts at the trailing deployment rate.", "Inventory · /planning/capacity-flow", k_weeks_of_cover),
    KpiDef("items_at_risk", "warehouse", "Products at stock-out risk", "count", "lower",
           "Products that run dry before their open inbound lands.", "Inventory · /planning/inventory-position", k_items_at_risk, "floor0"),
    KpiDef("safety_stock_coverage_pct", "warehouse", "Safety stock coverage (availability)", "pct", "higher",
           "Share of products holding at least their service-level safety stock.", "Inventory · /planning/inventory-position", k_safety_stock_coverage_pct, "cap100"),
    KpiDef("stock_value_eur", "warehouse", "Capital tied up in stock", "eur", "lower",
           "Order price of every unit physically in the warehouse, whatever its station. What the warehouse costs to hold.", "Assets · asset provenance", k_stock_value_eur),
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
           "Days from receipt to deployment, median over the units received in the last year.", "Assets · lifecycle", k_dock_to_deploy_days),
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


def _snapshot(db: Session, kpi_id: str, today: date, value: Optional[float], reason: Optional[str] = None,
              measured_at: Optional[datetime] = None) -> None:
    """Write the day's measurement. The row's audit stamp is set to the measurement's own instant, so a
    measurement reused later reports exactly the time it was taken, not the time the row was flushed."""
    row = db.execute(select(KpiSnapshot).where(KpiSnapshot.kpi_id == kpi_id, KpiSnapshot.as_of == today)).scalar_one_or_none()
    if row is None:
        row = KpiSnapshot(kpi_id=kpi_id, as_of=today, value=value, reason=reason)
        db.add(row)
    else:
        row.value, row.reason = value, reason
    if measured_at is not None:
        row.last_updated = measured_at
    db.flush()


def _utc(t: Optional[datetime]) -> Optional[datetime]:
    """The audit columns store UTC without a zone; say so, or a browser reads the time as local."""
    if t is None:
        return None
    return t if t.tzinfo is not None else t.replace(tzinfo=timezone.utc)


def _latest_snapshots(db: Session) -> dict[str, tuple[Optional[float], Optional[str], Optional[datetime], Optional[date]]]:
    """The most recent measurement of each KPI: (value, reason, real time it was taken, the world-day it describes)."""
    latest = select(KpiSnapshot.kpi_id, func.max(KpiSnapshot.as_of).label("as_of")).group_by(KpiSnapshot.kpi_id).subquery()
    rows = db.execute(
        select(KpiSnapshot.kpi_id, KpiSnapshot.value, KpiSnapshot.reason, KpiSnapshot.last_updated, KpiSnapshot.as_of)
        .join(latest, (latest.c.kpi_id == KpiSnapshot.kpi_id) & (latest.c.as_of == KpiSnapshot.as_of))).all()
    return {k: (float(v) if v is not None else None, r, _utc(t), _as_date(d)) for k, v, r, t, d in rows}


def _current_snapshots(db: Session, today: date) -> dict[str, tuple[Optional[float], Optional[str], Optional[datetime], Optional[date]]]:
    """The measurement that still stands for each KPI, if one does.

    "Once a day" is a day of the world's calendar. A measurement describes the world-day
    it was taken on (``as_of``). When the simulation moves the world by N days, that day
    moves back by N with every other date, and the measurement still stands: nothing real
    happened in between, it is simply N simulated days old, and the row says so. The
    moment the gap between today and the world-day is larger than what the calendar moved
    since the measurement, a real day has passed, and the KPI is measured anew. No
    wall-clock comparison anywhere: the calendar's own log answers the question.
    """
    log = timeshift.moves(db)
    out = {}
    for k, (v, r, t, d) in _latest_snapshots(db).items():
        if d is not None and (today - d).days == timeshift.advanced_since(log, t):
            out[k] = (v, r, t, d)
    return out


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


def compute_all(db: Session, *, today: Optional[date] = None, snapshot: bool = True, refresh: bool = False,
                only: Optional[set[str]] = None) -> list[dict]:
    """Every KPI with its value, targets, status and trend.

    A KPI is measured **once a day**. Thirty-two measurements over a fleet of 400,000
    devices are a quarter of a minute of database work; re-running them on every page
    load would make the tab unusable and would not change a single number, because each
    one is defined over a day. So a measurement that still stands is reused (see
    ``_current_snapshots`` for what "still stands" means once the simulation moves the
    calendar), and ``refresh=True`` (the tab's Measure again) forces a new one. The seed
    takes the first measurement, so the tab is complete the moment the demo comes up.

    ``only`` narrows a refresh to the KPIs named. The simulation tab measures again the
    KPIs a fleet event can move and leaves the rest on their standing measurement, instead
    of paying for a forecast backtest that no rental touches. Every row says when it was
    measured (``measured_at``, real time), which world-day it describes (``measured_on``)
    and how many simulated days ago that was (``stale_days``, zero for today's), so a
    figure measured before the calendar moved never looks like today's.
    """
    today = today or date.today()
    db.info.pop("kpis_memo", None)      # every measurement reads afresh; see _once
    done = _current_snapshots(db, today)
    if refresh:
        done = {} if only is None else {k: v for k, v in done.items() if k not in only}
    out = []
    for kpi in KPIS:
        if kpi.id in done:
            current, reason, measured_at, measured_on = done[kpi.id]
        else:
            try:
                current, reason = kpi.compute(db, today)
            except Exception as e:  # a broken upstream read must not take the tab down; say so instead
                current, reason = None, f"could not compute: {type(e).__name__}"
            measured_at, measured_on = datetime.now(timezone.utc), today
            if snapshot:
                _snapshot(db, kpi.id, today, current, reason, measured_at)
        t = get_or_seed_target(db, kpi, current)
        hist = history(db, kpi.id)
        measured = [h["value"] for h in hist if h["value"] is not None]
        first = measured[0] if measured else None
        status, progress = _status(kpi, current, t, first, len(measured))
        gap = None if (current is None or t.target_y1 is None) else round(t.target_y1 - current, 2)
        ex = kpi.explain
        out.append({
            "id": kpi.id, "group": kpi.group, "group_label": GROUP_LABEL[kpi.group], "name": kpi.name, "unit": kpi.unit,
            "direction": kpi.direction, "definition": kpi.definition, "source": kpi.source,
            # The explanation rides with the value: the same registry entry, never a screen's own prose.
            "basis": ex.basis, "calculation": ex.calculation, "reads": ex.reads, "caveats": ex.caveats,
            "why": ex.why, "needs": ex.needs,
            "current": current, "reason": reason, "as_of": today, "measured_at": measured_at,
            "measured_on": measured_on, "stale_days": max(0, (today - measured_on).days),
            "target_y1": t.target_y1, "target_y2": t.target_y2, "target_y3": t.target_y3,
            "owner": t.owner, "note": t.note, "placeholder": t.placeholder, "updated_by": t.updated_by,
            "status": status, "progress_pct": progress, "gap_to_y1": gap,
            "history": [{"as_of": h["as_of"], "value": h["value"]} for h in hist[-60:]],
        })
    return out

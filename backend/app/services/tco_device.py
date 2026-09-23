"""What a rented device costs over its life, per month of service, and what the second life gives back.

The datacenter TCO in ``tco.py`` follows one asset through five stored cost layers:
power, cooling, racking, the things a server costs. A rented phone costs none of
those. It costs what it was bought for, getting it to the warehouse and to the user,
the licence and the support it needs for every month it is out, the repairs and the
refurbishment between two rentals, the days it waits on a shelf with money tied up in
it, and at the end it gives some of that back when it is sold. That is the layer set
here, one row per layer, one component per source of the number.

**Where every number comes from is the load-bearing part.** Quantities are measured:
prices from the order line every serial traces to, months in service from the rental
contracts, repairs and refurbishments with their invoiced cost from the service
events, dwell days from the compartments, resale proceeds from the sold serials. Rates
that no data can supply (a licence per month, freight per device) are design
parameters, each a placeholder with the role that owns it, exactly like the target
dwell of a compartment. Every component says which of the two it is; a parameter is
never presented as a measurement.

**Two populations, because a fleet mid-scale-up has two truths.** *Finished lives* are
the devices that were sold or recycled: their acquisition, every rental, every repair
and their resale are all known, so cost per device and per month in service is a
whole-life figure. That is the number that decides whether a rental price works. *The
whole fleet to date* is every serial with what it has cost so far: right for the
running layers (licence, support, warehouse), and honest about the fact that
acquisition is paid on day one while most of a young fleet's resale credit has not
arrived yet. Both are served; the screen leads with the finished lives.

**Everything aggregates in the database, and every read over the fleet is an index
scan.** The asset table is grouped by status and by order line; the contracts by
(model, cycle, start) and (model, cycle, end, reason), which is why the contract
carries the model; the service events by (model, kind, cost). Grouping by the date
itself (the ``fleet.py`` pattern) keeps every result at a few thousand rows however
large the fleet, and months in service come out of the sum of ends minus the sum of
starts, so there is no date arithmetic in SQL over the 500,000 contracts. The counts
are ``count(*)``, on purpose: ``id`` is a text primary key, not SQLite's rowid, so
``count(table.id)`` has to visit the row behind every index entry and turns an index
scan into a table walk. The finished cohort (31,200 devices) is joined, once per
table, from a covering index on each side, with the day count done in SQL because
those rows are touched anyway; ``_days_between`` is the one dialect-aware line.
Every shape here was measured against the full fleet before it was chosen.

**No fake zeros.** A layer the data cannot support is ``None`` with a ``reason``; a
class with no device says so.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, fields
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

from sqlalchemy import Date, func, literal, select
from sqlalchemy.orm import Session

from app.models.catalog import Product
from app.models.flow import WAREHOUSE_STATUSES, Asset, AssetStatus
from app.models.procurement import OrderItem
from app.models.rental import RentalContract
from app.models.tco import ServiceEvent, ServiceKind
from app.services import kpis

DAYS_PER_MONTH = 30.4375
FAMILIES = ("Smartphone", "Tablet", "Laptop")            # the device classes, in the order the screen shows them
FINISHED_STATUSES = (AssetStatus.SOLD, AssetStatus.RECYCLED)

_ZERO = Decimal("0.00")
_CENT = Decimal("0.01")


# ---------------------------------------------------------------------------
# the rates: design parameters, each with the role that would set the real number


@dataclass(frozen=True)
class Rate:
    id: str
    label: str
    unit: str                                   # what one unit of the measured quantity is
    owner: str                                  # the role that owns the number
    note: str
    value: Optional[float] = None               # one value for every device class ...
    by_family: Optional[dict[str, float]] = None  # ... or one per class

    def for_family(self, family: Optional[str]) -> Optional[float]:
        if self.by_family is not None:
            return self.by_family.get(family or "")
        return self.value


# EUR net. Placeholders until the named role sets them; the screen shows them as such.
RATES: dict[str, Rate] = {r.id: r for r in (
    Rate("inbound", "Inbound logistics and handling", "per device", "Head of Supply",
         "freight, import handling and receiving, once per device delivered",
         by_family={"Smartphone": 4.0, "Tablet": 5.0, "Laptop": 9.0}),
    Rate("enrolment", "Enrolment and configuration", "per rental start", "Head of Operations",
         "MDM enrolment, staging and shipping to the user, once per rental",
         by_family={"Smartphone": 15.0, "Tablet": 15.0, "Laptop": 25.0}),
    Rate("software", "Software and management", "per device-month", "Head of IT",
         "MDM licence and support tooling, per month in service", value=2.5),
    Rate("support", "Support", "per device-month", "Head of Customer Success",
         "first-level support, per month in service", value=1.5),
    Rate("swap", "Defect and swap handling", "per event", "Head of Service Operations",
         "return of the defect device and shipping of the replacement, per contract ended by a defect or a swap", value=35.0),
    Rate("warehouse_day", "Warehousing", "per device-day", "Head of Operations",
         "space and handling, per device and day on hand", value=0.04),
    Rate("capital", "Cost of capital", "per EUR of stock value and year", "CFO",
         "the KPI tab's carrying-cost rate, on the order-line value of the stock on hand", value=kpis.CARRYING_COST_RATE_PA),
    Rate("recycling", "Recycling", "per device", "Head of Recommerce",
         "WEEE handling and data destruction of a device that is not sold", value=6.0),
)}

# The layers, in the order of a device's life. The eighth is the credit.
LAYERS = (
    ("acquisition", "Acquisition", "what the device was bought for: the unit price of the order line it traces to"),
    ("inbound", "Inbound logistics", "freight, import handling and receiving, per device delivered"),
    ("enrolment", "Enrolment and configuration", "MDM enrolment, staging and shipping to the user, once per rental start"),
    ("software", "Software and management", "MDM licence and support tooling, per month in service"),
    ("support", "Support and damage", "first-level support per month in service, plus every contract ended by a defect or a swap"),
    ("service", "Repair and refurbishment", "the partner's invoice per repair and per refurbishment, from the service events"),
    ("warehouse", "Warehousing and tied-up capital", "days on hand across the compartments, and the cost of the money sitting in that stock"),
    ("eol", "End of life", "resale proceeds as a credit, from the sold serials; recycling cost where there are none"),
)

COHORTS = {
    "finished": ("Finished lives", "Devices sold or recycled: acquisition, every rental, every repair and the resale are all known. "
                 "The whole-life cost per device and per month in service, the number a rental price has to cover."),
    "fleet": ("Whole fleet to date", "Every serial with what it has cost so far. Right for the running layers; acquisition is "
              "paid on day one and most of a young fleet's resale credit has not arrived yet."),
}

BASIS = ("Quantities are measured: prices from the order lines, months in service from the rental contracts, repairs and "
         "refurbishments from the service events, dwell from the compartments, proceeds from the sold serials. Rates are "
         "placeholders with an owner. Everything is computed in the database from every serial.")


def _money(x) -> Decimal:
    return Decimal(str(x if x is not None else 0)).quantize(_CENT, rounding=ROUND_HALF_UP)


def _as_date(v) -> date:
    """SQLite hands a date back as text on some paths; Postgres gives a date."""
    return v if isinstance(v, date) else date.fromisoformat(str(v)[:10])


def _is_daas(db: Session) -> bool:
    """A database with a rented device is a DaaS fleet: an existence probe, not a count."""
    return db.scalar(select(Asset.id).where(Asset.status == AssetStatus.RENTED).limit(1)) is not None


def _days_between(db: Session, later, earlier):
    """``later - earlier`` in whole days, as a SQL expression.

    Used only where the read touches the rows anyway (the finished cohort's join, the stock
    on hand). This is the one place the two dialects differ: Postgres subtracts two dates to
    an integer, SQLite needs julianday. The fleet-wide contract reads stay grouped by the
    date itself instead, because over 503,000 index entries a function call per row costs
    more than the grouped scan (408 ms against 141 ms, measured on the full fleet).
    """
    if db.get_bind().dialect.name == "sqlite":
        return func.julianday(later) - func.julianday(earlier)
    return later - earlier


# ---------------------------------------------------------------------------
# the measured quantities of one population of devices


@dataclass
class _Q:
    """Everything the data says about a set of devices, summed. A model has one, a class
    is the sum of its models, the portfolio the sum of everything; ``merge`` adds."""
    devices: int = 0
    priced: int = 0                      # devices that trace to an order line with a price
    acquisition: Decimal = _ZERO
    rented: int = 0
    on_hand: int = 0
    sold: int = 0
    sold_priced: int = 0                 # sold with recorded proceeds
    proceeds: Decimal = _ZERO
    acquisition_of_sold: Decimal = _ZERO  # what the sold-with-proceeds devices were bought for
    recycled: int = 0
    in_repair: int = 0
    in_refurb: int = 0
    contracts: int = 0
    contracts_cycle2: int = 0
    # Device-days in service come by two routes. Fleet-wide, from the index: a sum of start
    # ordinals and a sum of end ordinals, with the contracts still running ending today. For
    # the finished cohort, straight from the joined rows as a day count. ``days()`` adds both.
    ord_contracts: int = 0               # contracts accounted for through the ordinal sums
    ord_contracts_cycle2: int = 0
    ended: int = 0
    ended_cycle2: int = 0
    start_ord: int = 0
    start_ord_cycle2: int = 0
    end_ord: int = 0
    end_ord_cycle2: int = 0
    direct_days: int = 0
    direct_days_cycle2: int = 0
    swap_events: int = 0                 # contracts ended by a defect or a swap
    repairs: int = 0
    repair_cost: Decimal = _ZERO
    refurbs: int = 0
    refurb_cost: Decimal = _ZERO
    on_hand_dated: int = 0               # on hand with a dwell date
    on_hand_days: int = 0                # device-days on hand, to date
    on_hand_value_days: Decimal = _ZERO  # EUR-days: days times the order-line price, for the priced ones
    on_hand_unpriced: int = 0
    inbound: Decimal = _ZERO             # the two per-class rates, applied per model and summed upward
    enrolment: Decimal = _ZERO

    def merge(self, other: "_Q") -> None:
        for f in fields(self):
            setattr(self, f.name, getattr(self, f.name) + getattr(other, f.name))

    def days(self, today: date, cycle2: bool = False) -> int:
        """Device-days in service: every contract's end (today for a running one) minus its start."""
        if cycle2:
            running = self.ord_contracts_cycle2 - self.ended_cycle2
            return self.end_ord_cycle2 + running * today.toordinal() - self.start_ord_cycle2 + self.direct_days_cycle2
        running = self.ord_contracts - self.ended
        return self.end_ord + running * today.toordinal() - self.start_ord + self.direct_days


def _add_starts(acc: dict[str, _Q], rows) -> None:
    for pid, cycle, start, n in rows:
        q, n, o = acc[pid], int(n), _as_date(start).toordinal()
        q.contracts += n
        q.ord_contracts += n
        q.start_ord += n * o
        if int(cycle) >= 2:
            q.contracts_cycle2 += n
            q.ord_contracts_cycle2 += n
            q.start_ord_cycle2 += n * o


def _add_ends(acc: dict[str, _Q], rows) -> None:
    for pid, cycle, end, reason, n in rows:
        q, n, o = acc[pid], int(n), _as_date(end).toordinal()
        q.ended += n
        q.end_ord += n * o
        if int(cycle) >= 2:
            q.ended_cycle2 += n
            q.end_ord_cycle2 += n * o
        if reason in ("defect", "swap"):
            q.swap_events += n


def _add_joined(acc: dict[str, _Q], rows) -> None:
    """Contracts already summed to days in SQL: (model, cycle, end reason, how many, device-days)."""
    for pid, cycle, reason, n, days in rows:
        q, n, days = acc[pid], int(n), max(0, int(round(float(days or 0))))
        q.contracts += n
        q.direct_days += days
        if int(cycle) >= 2:
            q.contracts_cycle2 += n
            q.direct_days_cycle2 += days
        if reason in ("defect", "swap"):
            q.swap_events += n


def _add_events(acc: dict[str, _Q], rows) -> None:
    for pid, kind, n, cost in rows:
        q = acc[pid]
        if kind == ServiceKind.REPAIR or kind == "REPAIR":
            q.repairs += int(n)
            q.repair_cost += _money(cost)
        else:
            q.refurbs += int(n)
            q.refurb_cost += _money(cost)


# ---------------------------------------------------------------------------
# the reads: index scans over the fleet, joins only over the finished cohort


def _read(db: Session, today: date) -> tuple[dict, dict, dict[str, _Q], dict[str, _Q]]:
    """Every grouped query, once. Returns (products, lines, fleet per model, finished per model)."""
    products = {pid: (code, name, cat) for pid, code, name, cat in
                db.execute(select(Product.id, Product.product_code, Product.name, Product.category)).all()}
    lines = {lid: (pid, (Decimal(str(price)) if price is not None else None)) for lid, pid, price in
             db.execute(select(OrderItem.id, OrderItem.product_id, OrderItem.unit_price)).all()}
    fleet: dict[str, _Q] = defaultdict(_Q)
    fin: dict[str, _Q] = defaultdict(_Q)

    # Every population count of the asset table from one covering scan by status and order
    # line: devices per model (the line names the model), the priced ones and their
    # acquisition, and for the sold ones the recorded proceeds against what those same
    # devices were bought for. A device without a line (none in a seeded fleet, a handful in
    # a hand-built one) is read by model in a second, tiny query.
    asset_cols = (func.count(), func.count(Asset.sale_price), func.coalesce(func.sum(Asset.sale_price), 0))

    def add_devices(status, pid, price, n, n_priced, proceeds):
        n, n_priced = int(n), int(n_priced)
        for q in ((fleet[pid], fin[pid]) if status in FINISHED_STATUSES else (fleet[pid],)):
            q.devices += n
            if status == AssetStatus.RENTED:
                q.rented += n
            elif status in WAREHOUSE_STATUSES:
                q.on_hand += n
            elif status == AssetStatus.SOLD:
                q.sold += n
            elif status == AssetStatus.RECYCLED:
                q.recycled += n
            if status == AssetStatus.REPAIR:
                q.in_repair += n
            elif status == AssetStatus.REFURB:
                q.in_refurb += n
            if price is not None:
                q.priced += n
                q.acquisition += price * n
            if status == AssetStatus.SOLD:
                q.sold_priced += n_priced
                q.proceeds += _money(proceeds)
                if price is not None:
                    q.acquisition_of_sold += price * n_priced

    for status, lid, n, n_priced, proceeds in db.execute(
            select(Asset.status, Asset.source_order_item_id, *asset_cols)
            .where(Asset.source_order_item_id.is_not(None))
            .group_by(Asset.status, Asset.source_order_item_id)).all():
        pid, price = lines.get(lid, (None, None))
        if pid is not None:   # the line is a foreign key; a serial cannot point at a line that is gone
            add_devices(status, pid, price, n, n_priced, proceeds)
    for status, pid, n, n_priced, proceeds in db.execute(
            select(Asset.status, Asset.product_id, *asset_cols)
            .where(Asset.source_order_item_id.is_(None))
            .group_by(Asset.status, Asset.product_id)).all():
        add_devices(status, pid, None, n, n_priced, proceeds)
    finished = Asset.status.in_(FINISHED_STATUSES)

    # Dwell on hand, summed to device-days per order line in SQL from the covering index (the
    # rows are visited anyway, so the day count is done there); a device with a dwell date in
    # the future would count negative, so the sum is floored at zero.
    today_lit = literal(today, Date)
    on_hand = (Asset.status.in_(tuple(WAREHOUSE_STATUSES)), Asset.status_since.is_not(None))
    dwell_days = func.sum(_days_between(db, today_lit, Asset.status_since))

    def add_dwell(pid, price, n, days):
        n, days, q = int(n), max(0, int(round(float(days or 0)))), fleet[pid]
        q.on_hand_dated += n
        q.on_hand_days += days
        if price is not None:
            q.on_hand_value_days += price * days
        else:
            q.on_hand_unpriced += n

    for lid, n, days in db.execute(select(Asset.source_order_item_id, func.count(), dwell_days)
                                   .where(*on_hand, Asset.source_order_item_id.is_not(None))
                                   .group_by(Asset.source_order_item_id)).all():
        pid, price = lines.get(lid, (None, None))
        if pid is not None:
            add_dwell(pid, price, n, days)
    for pid, n, days in db.execute(select(Asset.product_id, func.count(), dwell_days)
                                   .where(*on_hand, Asset.source_order_item_id.is_(None))
                                   .group_by(Asset.product_id)).all():
        add_dwell(pid, None, n, days)

    # contracts, from the (model, cycle, start) and (model, cycle, end, reason) indexes; a
    # contract written without its model (an older path) is joined to its device instead
    rc = RentalContract
    _add_starts(fleet, db.execute(select(rc.product_id, rc.cycle_no, rc.start_date, func.count())
                                  .where(rc.product_id.is_not(None))
                                  .group_by(rc.product_id, rc.cycle_no, rc.start_date)).all())
    _add_ends(fleet, db.execute(select(rc.product_id, rc.cycle_no, rc.actual_end, rc.end_reason, func.count())
                                .where(rc.product_id.is_not(None), rc.actual_end.is_not(None))
                                .group_by(rc.product_id, rc.cycle_no, rc.actual_end, rc.end_reason)).all())
    _add_starts(fleet, db.execute(select(Asset.product_id, rc.cycle_no, rc.start_date, func.count())
                                  .select_from(rc).join(Asset, Asset.id == rc.asset_id)
                                  .where(rc.product_id.is_(None))
                                  .group_by(Asset.product_id, rc.cycle_no, rc.start_date)).all())
    _add_ends(fleet, db.execute(select(Asset.product_id, rc.cycle_no, rc.actual_end, rc.end_reason, func.count())
                                .select_from(rc).join(Asset, Asset.id == rc.asset_id)
                                .where(rc.product_id.is_(None), rc.actual_end.is_not(None))
                                .group_by(Asset.product_id, rc.cycle_no, rc.actual_end, rc.end_reason)).all())
    # the finished cohort's contracts: one join driven from its devices, which are few, with
    # the days summed in SQL; a contract still running on a finished device would end today
    service_days = func.sum(_days_between(db, func.coalesce(rc.actual_end, today_lit), rc.start_date))
    _add_joined(fin, db.execute(select(Asset.product_id, rc.cycle_no, rc.end_reason, func.count(), service_days)
                                .select_from(Asset).join(rc, rc.asset_id == Asset.id).where(finished)
                                .group_by(Asset.product_id, rc.cycle_no, rc.end_reason)).all())

    # service events: the fleet from the (model, kind, cost) index, the finished cohort by join
    se = ServiceEvent
    _add_events(fleet, db.execute(select(se.product_id, se.kind, func.count(), func.coalesce(func.sum(se.cost), 0))
                                  .group_by(se.product_id, se.kind)).all())
    _add_events(fin, db.execute(select(Asset.product_id, se.kind, func.count(), func.coalesce(func.sum(se.cost), 0))
                                .select_from(Asset).join(se, se.asset_id == Asset.id).where(finished)
                                .group_by(Asset.product_id, se.kind)).all())

    # the per-class rates, applied where the class is known: per model
    for acc in (fleet, fin):
        for pid, q in acc.items():
            family = products.get(pid, (None, None, None))[2]
            q.inbound = _money((RATES["inbound"].for_family(family) or 0) * q.devices)
            q.enrolment = _money((RATES["enrolment"].for_family(family) or 0) * q.contracts)
    return products, lines, fleet, fin


# ---------------------------------------------------------------------------
# from quantities to figures


def _component(cid: str, label: str, *, quantity, unit: str, rate: Optional[Rate], rate_value, total,
               measured: bool, reason: Optional[str] = None, note: Optional[str] = None) -> dict:
    return {
        "id": cid, "label": label,
        "basis": "measured" if measured else "quantity measured, rate placeholder",
        "quantity": (None if quantity is None else round(float(quantity), 1)), "unit": unit,
        "rate": (None if rate_value is None else float(rate_value)), "rate_id": (rate.id if rate else None),
        "total": (None if total is None else float(total)), "reason": reason, "note": note,
    }


def _blank(layers: list[dict], reason: str) -> None:
    """An empty population has no figure in any layer, only the reason. A rate times a
    count of zero devices is not a measured zero, it is nothing."""
    for lay in layers:
        lay.update(total=None, per_device=None, per_month=None, reason=reason)
        for c in lay["components"]:
            c.update(total=None, reason=reason)


def _layer(lid: str, label: str, components: list[dict], devices: int, months: float) -> dict:
    known = [c["total"] for c in components if c["total"] is not None]
    total = sum(known) if known else None
    reason = None
    if total is None:
        reason = "; ".join(dict.fromkeys(c["reason"] for c in components if c["reason"])) or "no data"
    return {
        "id": lid, "label": label, "total": (None if total is None else round(total, 2)),
        "per_device": (round(total / devices, 2) if total is not None and devices else None),
        "per_month": (round(total / months, 2) if total is not None and months > 0 else None),
        "reason": reason, "components": components,
    }


def _figures(q: _Q, today: date, *, finished: bool) -> tuple[list[dict], dict]:
    """The eight layers of one population, and the totals over them."""
    devices = q.devices
    days = q.days(today)
    months = days / DAYS_PER_MONTH
    labels = dict((lid, label) for lid, label, _ in LAYERS)
    R = RATES

    unpriced = devices - q.priced
    acquisition = [_component(
        "acquisition", "Order-line unit price", quantity=q.priced, unit="devices with an order-line price", rate=None,
        rate_value=None, total=(q.acquisition if q.priced else None), measured=True,
        reason=(None if q.priced else "no device traces to a priced order line"),
        note=(f"{unpriced:,} devices carry no order-line price and are not in this figure" if 0 < unpriced else None))]
    inbound = [_component("inbound", R["inbound"].label, quantity=devices, unit="devices delivered", rate=R["inbound"],
                          rate_value=(q.inbound / devices if devices else None), total=q.inbound, measured=False)]
    # Zero rental starts and zero months are counts, not missing data: a device bought and
    # never rented has cost nothing in enrolment or licences yet. Only the per-month figures
    # are undefined then, and the group says so.
    enrolment = [_component("enrolment", R["enrolment"].label, quantity=q.contracts, unit="rental starts", rate=R["enrolment"],
                            rate_value=(q.enrolment / q.contracts if q.contracts else R["enrolment"].for_family(None)),
                            total=q.enrolment, measured=False)]
    software = [_component("software", R["software"].label, quantity=months, unit="device-months in service", rate=R["software"],
                           rate_value=R["software"].value, total=_money(R["software"].value * months), measured=False)]
    support = [
        _component("support_month", R["support"].label, quantity=months, unit="device-months in service", rate=R["support"],
                   rate_value=R["support"].value, total=_money(R["support"].value * months), measured=False),
        _component("swaps", R["swap"].label, quantity=q.swap_events, unit="contracts ended by a defect or a swap", rate=R["swap"],
                   rate_value=R["swap"].value, total=_money(R["swap"].value * q.swap_events), measured=False),
    ]
    service = [
        _component("repairs", "Repairs", quantity=q.repairs, unit="repairs invoiced", rate=None,
                   rate_value=(q.repair_cost / q.repairs if q.repairs else None), total=q.repair_cost, measured=True,
                   note=(f"{q.in_repair:,} devices at the repair partner now, not yet invoiced" if q.in_repair else None)),
        _component("refurbs", "Refurbishments", quantity=q.refurbs, unit="refurbishments invoiced", rate=None,
                   rate_value=(q.refurb_cost / q.refurbs if q.refurbs else None), total=q.refurb_cost, measured=True,
                   note=(f"{q.in_refurb:,} devices on the bench now, not yet invoiced" if q.in_refurb else None)),
    ]
    if finished:
        wh_reason = "the warehouse days of a finished life are not logged; a measured figure needs the movement log"
    elif q.on_hand == 0:
        wh_reason = "no device of this group on hand"
    elif q.on_hand_dated == 0:
        wh_reason = "no dwell recorded: status_since is empty for every unit on hand"
    else:
        wh_reason = None
    undated = q.on_hand - q.on_hand_dated
    warehouse = [
        _component("warehouse_days", R["warehouse_day"].label, quantity=q.on_hand_days, unit="device-days on hand, to date",
                   rate=R["warehouse_day"], rate_value=R["warehouse_day"].value,
                   total=(None if wh_reason else _money(R["warehouse_day"].value * q.on_hand_days)), measured=False, reason=wh_reason,
                   note=(None if wh_reason or not undated else f"{undated:,} units on hand carry no dwell date and are not in this figure")),
        _component("capital", R["capital"].label, quantity=(float(q.on_hand_value_days) if not wh_reason else None), unit="EUR-days of stock value",
                   rate=R["capital"], rate_value=R["capital"].value,
                   total=(None if wh_reason else _money(float(q.on_hand_value_days) * R["capital"].value / 365.0)), measured=False,
                   reason=wh_reason,
                   note=(None if wh_reason or not q.on_hand_unpriced else f"{q.on_hand_unpriced:,} units on hand have no order-line price and tie up nothing here")),
    ]
    eol = [
        _component("resale", "Resale proceeds (credit)", quantity=q.sold_priced, unit="devices sold with recorded proceeds", rate=None,
                   rate_value=(q.proceeds / q.sold_priced if q.sold_priced else None), total=(-q.proceeds if q.sold else None), measured=True,
                   reason=(None if q.sold else "no device of this group has been sold yet"),
                   note=(f"{q.sold - q.sold_priced:,} sold devices have no recorded proceeds" if q.sold > q.sold_priced else None)),
        _component("recycling", R["recycling"].label, quantity=q.recycled, unit="devices recycled", rate=R["recycling"],
                   rate_value=R["recycling"].value, total=_money(R["recycling"].value * q.recycled), measured=False),
    ]
    parts = {"acquisition": acquisition, "inbound": inbound, "enrolment": enrolment, "software": software,
             "support": support, "service": service, "warehouse": warehouse, "eol": eol}
    layers = [_layer(lid, labels[lid], parts[lid], devices, months) for lid, _l, _d in LAYERS]
    if not devices:
        _blank(layers, "no device in this group")

    credit = float(q.proceeds)
    costs = [c["total"] for lay in layers for c in lay["components"] if c["total"] is not None and c["id"] != "resale"]
    gross = round(sum(costs), 2) if devices else None
    net = (round(gross - credit, 2) if gross is not None else None)
    per = lambda v, d: (None if v is None or not d else round(v / d, 2))  # noqa: E731 - a two-line helper, not a function
    months_reason = None if months > 0 else ("no month in service recorded for this group" if devices else "no device in this group")
    resale_reason = None
    if q.sold == 0:
        resale_reason = "no device of this group has been sold yet"
    elif q.acquisition_of_sold == 0:
        resale_reason = "the sold devices trace to no priced order line, so no share can be given"
    totals = {
        "devices": devices, "rented": q.rented, "on_hand": q.on_hand, "sold": q.sold, "recycled": q.recycled, "priced": q.priced,
        "contracts": q.contracts, "contracts_cycle2": q.contracts_cycle2,
        "device_months": round(months, 1), "device_months_cycle2": round(q.days(today, cycle2=True) / DAYS_PER_MONTH, 1),
        "months_per_device": (round(months / devices, 1) if devices and q.contracts else None),
        "second_life_share_of_months": (round(q.days(today, cycle2=True) / days, 4) if days > 0 else None),
        "repairs": q.repairs, "refurbs": q.refurbs, "in_repair": q.in_repair, "in_refurb": q.in_refurb, "swap_events": q.swap_events,
        "gross": gross, "credit": round(credit, 2), "net": net,
        "per_device": {"gross": per(gross, devices), "credit": per(credit, devices), "net": per(net, devices)},
        "per_month": {"gross": per(gross, months), "credit": per(credit, months), "net": per(net, months)},
        "per_month_reason": months_reason,
        "resale": {
            "sold": q.sold, "sold_priced": q.sold_priced, "proceeds": round(credit, 2),
            "acquisition_of_sold": float(q.acquisition_of_sold),
            "credit_share_of_acquisition": (round(float(q.proceeds / q.acquisition_of_sold), 4) if q.acquisition_of_sold else None),
            "reason": resale_reason,
        },
        "unmeasured": [lay["id"] for lay in layers if lay["total"] is None],
    }
    return layers, totals


def _group(q: _Q, *, kind: str, key: str, label: str, today: date, finished: bool,
           family: Optional[str] = None, product_code: Optional[str] = None) -> dict:
    row = {"kind": kind, "key": key, "label": label, "family": family, "product_code": product_code}
    if q.devices == 0:
        where = "in the fleet" if not finished else "has finished its life yet"
        what = {"class": f"no device of this class {where}", "model": f"no device of this model {where}",
                "portfolio": f"no device {where}"}[kind]
        layers, totals = _figures(q, today, finished=finished)
        row.update(totals, layers=layers, reason=what)
        return row
    layers, totals = _figures(q, today, finished=finished)
    row.update(totals, layers=layers, reason=None)
    return row


def _cohort(cid: str, per_model: dict[str, _Q], products: dict, today: date) -> dict:
    finished = cid == "finished"
    by_family: dict[str, _Q] = {f: _Q() for f in FAMILIES}
    everything = _Q()
    models = []

    def model_order(pid: str) -> tuple[int, str]:
        """Classes in their fixed order, models by name within a class, unknown classes last."""
        _code, name, family = products.get(pid, ("", "", None))
        return (FAMILIES.index(family) if family in FAMILIES else len(FAMILIES), name)

    for pid, q in sorted(per_model.items(), key=lambda kv: model_order(kv[0])):
        code, name, family = products.get(pid, (pid, pid, None))
        by_family.setdefault(family or "other", _Q()).merge(q)
        everything.merge(q)
        models.append(_group(q, kind="model", key=pid, label=name, today=today, finished=finished, family=family, product_code=code))
    classes = [_group(by_family[f], kind="class", key=f, label=f, today=today, finished=finished, family=f) for f in by_family]
    label, description = COHORTS[cid]
    return {"id": cid, "label": label, "description": description,
            "portfolio": _group(everything, kind="portfolio", key="all", label="All devices", today=today, finished=finished),
            "classes": classes, "models": models}


def overview(db: Session, *, today: Optional[date] = None) -> dict:
    """The device TCO: both populations, each per class, per model and rolled up. See the module docstring."""
    today = today or date.today()
    head = {
        "scenario": "daas", "as_of": today, "reason": None, "basis": BASIS,
        "rates": [{"id": r.id, "label": r.label, "unit": r.unit, "owner": r.owner, "placeholder": True, "note": r.note,
                   "value": r.value, "by_family": r.by_family} for r in RATES.values()],
        "layers": [{"id": lid, "label": label, "description": desc} for lid, label, desc in LAYERS],
    }
    if not _is_daas(db):
        head.update(scenario="datacenter", cohorts={},
                    reason="the device TCO exists in the device-as-a-service scenario; this database holds the datacenter operation, "
                           "whose per-asset TCO is at /tco/portfolio and /tco/by-class")
        return head
    products, _lines, fleet, fin = _read(db, today)
    head["cohorts"] = {cid: _cohort(cid, acc, products, today) for cid, acc in (("finished", fin), ("fleet", fleet))}
    return head

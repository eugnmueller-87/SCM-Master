"""The simulation tab: the day-to-day of a device fleet, fired at the running system.

The fleet is a seeded snapshot. Its dates were computed on the day it was generated, so
it ages one day per day and nothing ever moves: the numbers drift toward stale, and a
demo can only assert how the operation behaves instead of showing it. This module fires
the events of a fleet's day at the live database, and every read (the warehouse, the
capacity plan, the KPIs) answers from what actually happened.

**The rule that makes this worth building.** Every simulated event goes through the
same service calls the application uses: ``asset_service.transition`` by way of
``cycle.py`` for every step of a device, ``asset_service.receive`` for a delivery. A
move the state machine forbids is refused here too, with the machine's own reason, and
the dwell clock is stamped the way it is in real use. A simulator that wrote rows
directly would prove nothing and could create states the application cannot produce.

**Quantities are the operator's; grades, terms, prices are the fleet's rules.** Where
an event needs a grade, a term, a rent, a price or a partner's invoice, the value comes
from the rules the seed already encodes (``seed_daas``: the grade mix, the term mix, the
rent formula, the residual curve, the channel fees, the repair and refurbishment
invoices) and from the fleet's next-step rule (``fleet.NEXT_STEP_SHARE``), applied in
proportion and deterministically: a batch of 100 graded devices carries the mix as
exactly as whole units allow, never a second set of numbers and never a dice roll that
makes two runs differ.

**A day of normal operation** runs the whole rotation once at today's rates, and the
rates are measured from the fleet, not typed in: returns from the calendar of running
contracts, first rentals from the contracts started in the last full months, second
rentals and sales from the last 90 days, the chain from the returns under the next-step
rule, deliveries from the order lines whose date has come. Run once a day, it keeps the
dataset alive at the pace of the business it describes.

**Time passes for real.** A day of operation and a hold let the days pass before they
move a single device: ``timeshift.advance`` moves every date the fleet carries back by
N days, which every read (all compare stored dates to the real today) sees as the same
world N days later. So the stock already waiting ages, contracts come due, a delivery's
date arrives, and a 17-day-old unit in the MDM hold does cross the 21-day service level
on the fifth day of a hold. The shift is once per action, by N, and each simulated day's
moves are stamped on their day (``today - (N - k)``), the same end state as a day at a
time. The answer says which event moved the world, by how many days, and that the
dataset now stands N days later than when it was seeded; a calendar label anywhere on
the screens is the real date minus that offset.

**Performance.** An action on N devices touches N devices, never the fleet: the units
are picked oldest first from the ``(status, status_since)`` index with a LIMIT, each
moves through the asset service (under a millisecond on a 431,200-serial fleet), and a
batch is capped at ``MAX_UNITS``. The reads an answer carries are the existing ones,
each a fraction of a second, and the KPI measurement after an action re-measures only
the KPIs a fleet event can move.

**Safety.** This writes to the demo database. It refuses in production the way the
seeders do (``assert_demo_write_allowed``), the API gates every write by role, and the
way back to a known state is the boot's own rebuild, run in a process of its own.
"""
from __future__ import annotations

import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import seed_daas as rules
from app.core.safety import assert_demo_write_allowed
from app.models.catalog import Organization, Product
from app.models.flow import Asset, AssetStatus, Location, LocationType, ReceiptItem
from app.models.procurement import OrderItem, OrderStatus, PurchaseOrder
from app.models.rental import ContractStatus, RentalContract
from app.services import capacity_plan, cycle, fleet, kpis, lifecycle, timeshift, warehouse
from app.services.asset import asset_service
from app.services.exceptions import NotFoundError, ValidationError

MAX_UNITS = 5_000            # a batch touches N devices, never the fleet
MAX_DAYS = 30                # a hold runs the rotation once per day
DEFAULT_UNITS = 100
ACTOR = "simulation"
DAYS_PER_MONTH = cycle.DAYS_PER_MONTH
OPEN_ORDER = (OrderStatus.PLACED, OrderStatus.PARTIALLY_RECEIVED)   # an order not yet placed cannot arrive
NEW_STOCK = (AssetStatus.IN_STORAGE, AssetStatus.RECEIVED)
RECYCLE_SHARE = fleet.NEXT_STEP_SHARE[1]["recycling"]                # the share of graded returns recycled, whatever the grade

# The KPIs a fleet event can move. The rest (should-cost, requisitions, contracts, the
# forecast backtest) read tables no rental, return, repair or sale touches, and the
# backtest alone costs as much as every fleet and warehouse KPI together.
SIM_KPIS = tuple(k.id for k in kpis.KPIS if k.group in ("fleet", "warehouse") and k.id != "on_time_delivery_pct")
KEPT_REASON = ("not measured by this event: these KPIs read tables no rental, return, repair or sale touches (should-cost, "
               "requisitions, contracts, the forecast backtest), and the backtest alone costs more than the fleet and warehouse "
               "KPIs together. When days passed they did change with the calendar: each row says how many simulated days ago it "
               "was measured, and Measure again on the KPIs tab, or the daily boot, brings them current.")

CALENDAR_NOTE = ("A day of operation or a hold lets time pass for real: every date in the dataset moves back by the days that "
                 "passed, so the stock that did not move ages, contracts come due and deliveries arrive. A calendar label on "
                 "the screens is the real date minus the days the dataset stands ahead of its seed.")


def _world_note(days: int, world: dict) -> str:
    return (f"The world moved {days} day{'s' if days != 1 else ''}: every date in the dataset moved back by {days}; the dataset "
            f"now stands {world['days_advanced']} day{'s' if world['days_advanced'] != 1 else ''} later than when it was seeded.")

# Which flow leaves a station, so a hold knows what to throttle. The swap buffer is a reserve and has no flow.
OUTFLOW_OF = {"ST-NEW": "first_rentals", "ST-RETURNS": "intake", "ST-MDM": "mdm_release", "ST-WIPE": "grading",
              "ST-REPAIR": "repair_done", "ST-REFURB": "refurb_done", "ST-SECOND": "second_rentals", "ST-SELL": "sales"}

_CATALOGUE_RRP = {code: rrp for code, _name, _family, _oem, _launch, rrp, _url in rules.CATALOGUE}
_EXTRA_LABEL = {"ORDERED": "On order", "RENTED": "At customers", "SOLD": "Sold", "RECYCLED": "Recycled"}


def _label(code: str) -> str:
    """A status as the operator names it: the compartment, or where the device is instead."""
    if code in _EXTRA_LABEL:
        return _EXTRA_LABEL[code]
    comp = warehouse.STATION_OF_STATUS.get(AssetStatus(code)) if code in AssetStatus.__members__ else None
    return comp.name if comp else code


def _is_daas(db: Session) -> bool:
    """A database with a rented device is a DaaS fleet: an existence probe, not a count."""
    return db.scalar(select(Asset.id).where(Asset.status == AssetStatus.RENTED).limit(1)) is not None


def _spread(n: int, mix: dict) -> list:
    """``n`` picks from a mix, deterministic and in proportion.

    At every step the key furthest behind its share gets the next pick, so a batch of
    any size carries the mix as exactly as whole units allow, a partial batch still
    resembles it, and two runs pick the same. A mix is a rule, not a lottery.
    """
    keys = list(mix)
    if not keys or n <= 0:
        return []
    total = float(sum(mix.values())) or 1.0
    given = {k: 0 for k in keys}
    out = []
    for i in range(1, n + 1):
        k = max(keys, key=lambda x: (mix[x] / total * i - given[x], -keys.index(x)))
        given[k] += 1
        out.append(k)
    return out


# ---------------------------------------------------------------------------
# what one action did


@dataclass
class Run:
    """What an action moved and refused, accumulated as it goes, plus what it wants to say."""

    action: str
    moved: Counter = field(default_factory=Counter)      # (from status code, to status code) -> units
    refused: Counter = field(default_factory=Counter)    # (what, reason) -> units
    notes: list = field(default_factory=list)
    extra: dict = field(default_factory=dict)
    quiet: bool = False                                  # a rotation silences the steps' own lines, see note()

    def move(self, frm: str, to: str, n: int = 1) -> None:
        self.moved[(frm, to)] += n

    def refuse(self, what: str, reason: str, n: int = 1) -> None:
        self.refused[(what, reason)] += n

    def note(self, text: str, *, always: bool = False) -> None:
        """A line for the answer. A day or a hold runs every step once per day and answers with one
        summary instead of the same grading and rental lines once per day; a line that names what
        arrived against which order is kept whatever the mode."""
        if always or not self.quiet:
            self.notes.append(text)

    @property
    def moved_total(self) -> int:
        return sum(self.moved.values())


def _try(run: Run, what: str, frm: AssetStatus, fn: Callable[[], AssetStatus]) -> bool:
    """One device through the services. A refusal is counted with the service's own reason and the batch goes on."""
    try:
        to = fn()
    except (ValidationError, NotFoundError) as e:
        run.refuse(what, str(e))
        return False
    run.move(frm.value, to.value)
    return True


def _short(run: Run, what: str, wanted: int, got: int, where: str) -> None:
    """The part of a batch the source could not supply, said plainly."""
    if got < wanted:
        run.refuse(what, (f"only {got:,} in {where}" if got else f"nothing in {where}"), wanted - got)


def _pick(db: Session, statuses: tuple, n: int, *, product_id: Optional[str] = None) -> list[str]:
    """The oldest ``n`` units of a status, from the (status, status_since) index. First in, first out."""
    if n <= 0:
        return []
    stmt = select(Asset.id).where(Asset.status.in_(tuple(statuses)))
    if product_id is not None:
        stmt = stmt.where(Asset.product_id == product_id)
    return list(db.execute(stmt.order_by(Asset.status_since.asc().nulls_last()).limit(n)).scalars())


def _customers(db: Session) -> list[str]:
    """The role-only customer accounts, in a fixed order, so rentals spread over them deterministically."""
    return list(db.execute(select(Organization.id)
                           .where(Organization.is_supplier.is_(False), Organization.is_manufacturer.is_(False), Organization.active.is_(True))
                           .order_by(Organization.code)).scalars())


# ---------------------------------------------------------------------------
# the rules the seed encodes, applied per device


def _rent(family: Optional[str], price, term: int, cycle_no: int) -> Optional[float]:
    """The seed's rent formula: contract price times the monthly share per family times the term factor; a second rental at its share of the first."""
    if price is None:
        return None
    share = rules.RENT_SHARE_PER_MONTH.get(family or "")
    factor = rules.TERM_RATE_FACTOR.get(term)
    if share is None or factor is None:
        return None
    return round(float(price) * share * factor * (rules.RENT2_SHARE if cycle_no >= 2 else 1.0), 2)


def _terms(families: list, cycle_no: int) -> list[int]:
    """A term per device from the seed's term mix, in proportion within each family."""
    out: list = [None] * len(families)
    for fam in set(families):
        idx = [i for i, f in enumerate(families) if f == fam]
        mix = rules.TERM_MIX_CYCLE2 if cycle_no >= 2 else rules.TERM_MIX.get(fam or "", {24: 1.0})
        for i, term in zip(idx, _spread(len(idx), mix)):
            out[i] = int(term)
    return out


def _sale_price(code: Optional[str], family: Optional[str], grade: Optional[str], received, deployed, unit_price, channel: str,
                today: date) -> tuple[Optional[float], Optional[str]]:
    """Net proceeds of one sale from the fleet's own residual curve, or why there is no price.

    The curve wants the net launch price and the age: the launch RRP from the catalogue
    when the model is in it, else the order line's purchase price stands in and the
    answer says so. Without a purchase date there is no age and no price: an unpriced
    sale is honest, an invented one is not.
    """
    rrp = _CATALOGUE_RRP.get(code or "")
    if rrp is not None:
        base = rrp / (1 + rules.VAT)
    elif unit_price is not None:
        base = float(unit_price)
    else:
        return None, "no launch price in the catalogue and no purchase price on the order line"
    since = received or deployed
    if since is None:
        return None, "no purchase date: the age, and with it the residual value, is unknown"
    age_months = max(0.0, (today - since).days / DAYS_PER_MONTH)
    share = rules._residual_share(family or "", age_months, grade or "B")
    return round(base * share * (1 - rules.CHANNEL_FEE.get(channel, 0.0)), 2), None


def _service_cost(table: dict, family: Optional[str]) -> Optional[float]:
    """The midpoint of the seed's invoice range for the device class: a placeholder with an owner, like the seed's own."""
    rng = table.get(family or "")
    return None if rng is None else (rng[0] + rng[1]) / 2.0


# ---------------------------------------------------------------------------
# the measured rates of a day


def _open_lines(db: Session, *, product_id: Optional[str] = None, due_only: bool = False, today: Optional[date] = None) -> list[dict]:
    """Open order lines with units still outstanding, earliest delivery date first."""
    stmt = (select(OrderItem.id, OrderItem.order_id, OrderItem.product_id, OrderItem.quantity, OrderItem.estimated_delivery_date,
                   PurchaseOrder.order_number)
            .join(PurchaseOrder, PurchaseOrder.id == OrderItem.order_id)
            .where(PurchaseOrder.status.in_(OPEN_ORDER)))
    if product_id is not None:
        stmt = stmt.where(OrderItem.product_id == product_id)
    if due_only:
        stmt = stmt.where(OrderItem.estimated_delivery_date <= today)
    rows = db.execute(stmt.order_by(OrderItem.estimated_delivery_date.asc().nulls_last())).all()
    ids = [r[0] for r in rows]
    got: dict = {}
    if ids:
        got = {k: int(v or 0) for k, v in db.execute(
            select(ReceiptItem.order_item_id, func.sum(ReceiptItem.quantity_received))
            .where(ReceiptItem.order_item_id.in_(ids)).group_by(ReceiptItem.order_item_id)).all()}
    out = []
    for lid, oid, pid, qty, eta, po in rows:
        left = int(qty) - got.get(lid, 0)
        if left > 0:
            out.append({"line_id": lid, "order_id": oid, "product_id": pid, "outstanding": left, "eta": eta, "order_number": po})
    return out


def measure_rates(db: Session, today: date) -> dict:
    """Today's flows, measured from the fleet. Each carries its basis; a flow the data cannot give is None with the reason."""
    running = RentalContract.status == ContractStatus.RUNNING
    due30 = int(db.scalar(select(func.count()).select_from(RentalContract)
                          .where(running, RentalContract.planned_end <= today + timedelta(days=30))) or 0)
    overdue = int(db.scalar(select(func.count()).select_from(RentalContract).where(running, RentalContract.planned_end < today)) or 0)
    first_pm, first_n = capacity_plan._first_rentals(db, today)
    started90 = int(db.scalar(select(func.count()).select_from(RentalContract)
                              .where(RentalContract.cycle_no >= 2, RentalContract.start_date > today - timedelta(days=90),
                                     RentalContract.start_date <= today)) or 0)
    sold90 = int(db.scalar(select(func.count(Asset.id)).where(Asset.status == AssetStatus.SOLD, Asset.sold_date > today - timedelta(days=90))) or 0)
    first_c, second_c = capacity_plan._rented_by_cycle(db)
    fleet_now = first_c + second_c
    c2 = (second_c / fleet_now) if fleet_now else None
    shares = capacity_plan._shares(c2) if c2 is not None else None
    returns = due30 / 30.0
    due_lines = _open_lines(db, due_only=True, today=today)
    chain = {code: int(round(returns * (shares[code] if shares else 1.0))) for code in ("ST-RETURNS", "ST-MDM", "ST-WIPE", "ST-REPAIR", "ST-REFURB")}
    return {
        "returns_per_day": int(round(returns)),
        "returns_basis": f"measured: {due30:,} running contracts end in the next 30 days ({overdue:,} of them overdue), over 30",
        "first_rentals_per_day": (int(round(first_pm / DAYS_PER_MONTH)) if first_pm else None),
        "first_rentals_basis": (f"measured: {first_n:,} first rentals started in the last {capacity_plan.MEASURED_MONTHS} full months"
                                if first_pm else f"no first rental started in the last {capacity_plan.MEASURED_MONTHS} full months"),
        "second_rentals_per_day": int(round(started90 / 90.0)),
        "second_rentals_basis": f"measured: {started90:,} second rentals started in the last 90 days, over 90",
        "sales_per_day": int(round(sold90 / 90.0)),
        "sales_basis": f"measured: {sold90:,} devices sold in the last 90 days, over 90",
        "chain": chain,
        "chain_basis": ("derived: the return flow times the share of returns each station sees under the next-step rule "
                        f"(fleet.NEXT_STEP_SHARE) over {c2:.1%} second rentals" if c2 is not None else "no rented device: no return flow"),
        "deliveries_due": sum(line["outstanding"] for line in due_lines),
        "deliveries_basis": f"measured: {len(due_lines)} open order lines whose delivery date has come",
        "cycle2_share": (round(c2, 4) if c2 is not None else None),
    }


# ---------------------------------------------------------------------------
# the actions: each one a thing that happens in this business


def act_delivery(db: Session, run: Run, *, units: int, product_code: Optional[str] = None, due_only: bool = False,
                 today: date, actor: str, stations: dict) -> None:
    """An inbound delivery arrives: units are received against the open order lines, earliest date first, partial receipts allowed."""
    product_id = None
    if product_code:
        product_id = db.scalar(select(Product.id).where(Product.product_code == product_code))
        if product_id is None:
            raise NotFoundError(f"no product with code {product_code!r}")
    into = stations.get("ST-NEW") or db.scalar(select(Location.id).where(Location.location_type == LocationType.WAREHOUSE).limit(1))
    if into is None:
        run.refuse("delivery", "no warehouse location to receive into", units)
        return
    left = units
    for line in _open_lines(db, product_id=product_id, due_only=due_only, today=today):
        if left <= 0:
            break
        qty = min(left, line["outstanding"])
        try:
            asset_service.receive(db, line["order_id"], location_id=into, lines=[{"order_item_id": line["line_id"], "quantity": qty}],
                                  receipt_date=today, actor=actor)
        except (ValidationError, NotFoundError) as e:
            run.refuse("delivery", str(e), qty)
            continue
        run.move("ORDERED", AssetStatus.RECEIVED.value, qty)
        left -= qty
        rest = line["outstanding"] - qty
        run.note(f"{qty:,} received against {line['order_number']}" + (f", {rest:,} still outstanding" if rest else ", line complete"), always=True)
    if left > 0:
        what = "due for delivery" if due_only else "on order"
        run.refuse("delivery", f"only {units - left:,} {what}" + (f" for {product_code}" if product_code else ""), left)


def _rentals(db: Session, run: Run, *, statuses: tuple, units: int, what: str, where: str, today: date, actor: str) -> None:
    """Rentals start from a stock, oldest units first, spread over the customers; term and rent from the seed's rules."""
    customers = _customers(db)
    if not customers:
        run.refuse(what, "no customer account to rent to", units)
        return
    rows = db.execute(
        select(Asset.id, Asset.cycle_no, Product.category, OrderItem.unit_price)
        .join(Product, Product.id == Asset.product_id).outerjoin(OrderItem, OrderItem.id == Asset.source_order_item_id)
        .where(Asset.status.in_(statuses)).order_by(Asset.status_since.asc().nulls_last()).limit(units)).all()
    _short(run, what, units, len(rows), where)
    if not rows:
        return
    next_cycle = int(rows[0][1] or 0) + 1
    terms = _terms([r[2] for r in rows], next_cycle)
    frm = AssetStatus(db.scalar(select(Asset.status).where(Asset.id == rows[0][0])))
    for i, ((aid, _cyc, family, price), term) in enumerate(zip(rows, terms)):
        cust = customers[i % len(customers)]
        rent = _rent(family, price, term, next_cycle)
        _try(run, what, frm, lambda: (cycle.rent(db, aid, customer_id=cust, term_months=term, rent_eur_month=rent, actor=actor, today=today),
                                      AssetStatus.RENTED)[1])
    run.note(f"{len(rows):,} {what} over {min(len(rows), len(customers))} customer accounts; terms from the seed's term mix, "
             "rent from its formula [placeholder, Head of Sales]")


def act_first_rentals(db: Session, run: Run, *, units: int, today: date, actor: str, stations: dict) -> None:
    """Devices go out to customers: first rentals from new stock."""
    _rentals(db, run, statuses=NEW_STOCK, units=units, what="first rentals", where="new stock", today=today, actor=actor)


def act_second_rentals(db: Session, run: Run, *, units: int, today: date, actor: str, stations: dict) -> None:
    """Second rentals start from the second-life stock."""
    _rentals(db, run, statuses=(AssetStatus.READY_SECOND,), units=units, what="second rentals", where="second-life stock", today=today, actor=actor)


def act_returns(db: Session, run: Run, *, units: int, today: date, actor: str, stations: dict) -> None:
    """Rentals come back: the contracts nearest their end (overdue first) end, the devices land in returns intake."""
    rows = list(db.execute(select(RentalContract).where(RentalContract.status == ContractStatus.RUNNING)
                           .order_by(RentalContract.planned_end.asc(), RentalContract.start_date.asc()).limit(units)).scalars())
    _short(run, "returns", units, len(rows), "running contracts")
    for c in rows:
        reason = "planned" if c.planned_end <= today else "early"
        _try(run, "returns", AssetStatus.RENTED,
             lambda: (cycle.take_back(db, c.asset_id, reason=reason, contract=c, location_id=stations.get("ST-RETURNS"), actor=actor, today=today),
                      AssetStatus.RETURNED)[1])


def act_intake(db: Session, run: Run, *, units: int, today: date, actor: str, stations: dict) -> None:
    """Returns are booked in and wait for the old customer's MDM release."""
    ids = _pick(db, (AssetStatus.RETURNED,), units)
    _short(run, "intake", units, len(ids), "returns intake")
    for aid in ids:
        _try(run, "intake", AssetStatus.RETURNED, lambda: cycle.book_in(db, aid, location_id=stations.get("ST-MDM"), actor=actor, today=today).status)


def act_mdm_release(db: Session, run: Run, *, units: int, today: date, actor: str, stations: dict) -> None:
    """The old customer releases devices from its MDM: on to wipe and grading, the longest-waiting first."""
    ids = _pick(db, (AssetStatus.MDM_RELEASE,), units)
    _short(run, "MDM release", units, len(ids), "the MDM release hold")
    for aid in ids:
        _try(run, "MDM release", AssetStatus.MDM_RELEASE, lambda: cycle.release(db, aid, location_id=stations.get("ST-WIPE"), actor=actor, today=today).status)


def act_grading(db: Session, run: Run, *, units: int, today: date, actor: str, stations: dict) -> None:
    """Wipe and grading clears a batch: grades in the seed's mix, the next step by the fleet's rule, a share recycled."""
    rows = db.execute(select(Asset.id, Asset.cycle_no).where(Asset.status == AssetStatus.WIPE_GRADING)
                      .order_by(Asset.status_since.asc().nulls_last()).limit(units)).all()
    _short(run, "grading", units, len(rows), "wipe and grading")
    grades = _spread(len(rows), rules.GRADE_MIX)
    fate = _spread(len(rows), {"keep": 1.0 - RECYCLE_SHARE, "recycle": RECYCLE_SHARE})
    for (aid, _cyc), g, f in zip(rows, grades, fate):
        _try(run, "grading", AssetStatus.WIPE_GRADING,
             lambda: cycle.grade(db, aid, grade=g, recycle=(f == "recycle"), actor=actor, today=today).status)
    if rows:
        mix = ", ".join(f"{g} {grades.count(g)}" for g in rules.GRADE_MIX)
        run.note(f"grades {mix} from the grade mix [placeholder, Head of Recommerce]; after a first rental A and B go to refurbishment, "
                 f"C to repair, D to sale; after a second rental everything is cleared for sale; {fate.count('recycle')} recycled")


def _service_batch(db: Session, run: Run, *, status: AssetStatus, units: int, what: str, where: str, table: dict, step: Callable,
                   owner: str, today: date, actor: str, location_id: Optional[str]) -> None:
    rows = db.execute(select(Asset.id, Product.category).join(Product, Product.id == Asset.product_id)
                      .where(Asset.status == status).order_by(Asset.status_since.asc().nulls_last()).limit(units)).all()
    _short(run, what, units, len(rows), where)
    unpriced = 0
    for aid, family in rows:
        cost = _service_cost(table, family)
        if cost is None:
            cost, unpriced = 0.0, unpriced + 1
        _try(run, what, status, lambda: (step(db, aid, cost=cost, location_id=location_id, actor=actor, today=today), None)[1] or _after(db, aid))
    if rows:
        run.note(f"invoices at the midpoint of the seed's range per device class [placeholder, {owner}]"
                 + (f"; {unpriced} device classes without a range invoiced at zero" if unpriced else ""))


def _after(db: Session, asset_id: str) -> AssetStatus:
    return AssetStatus(db.scalar(select(Asset.status).where(Asset.id == asset_id)))


def act_repair_done(db: Session, run: Run, *, units: int, today: date, actor: str, stations: dict) -> None:
    """The repair partner finishes a batch: an invoice per device, then refurbishment."""
    _service_batch(db, run, status=AssetStatus.REPAIR, units=units, what="repair", where="repair", table=rules.REPAIR_COST,
                   step=cycle.repair_done, owner="Head of Service Operations", today=today, actor=actor, location_id=stations.get("ST-REFURB"))


def act_refurb_done(db: Session, run: Run, *, units: int, today: date, actor: str, stations: dict) -> None:
    """Refurbishment finishes: an invoice per device, then the second-life stock."""
    _service_batch(db, run, status=AssetStatus.REFURB, units=units, what="refurbishment", where="refurbishment", table=rules.REFURB_COST,
                   step=cycle.refurb_done, owner="Head of Recommerce", today=today, actor=actor, location_id=stations.get("ST-SECOND"))


def act_sales(db: Session, run: Run, *, units: int, today: date, actor: str, stations: dict) -> None:
    """Sales complete: the oldest sellable stock first, channels in the seed's mix, the price from the fleet's residual curve."""
    rows = db.execute(
        select(Asset.id, Asset.grade, Asset.received_date, Asset.deployed_date, Product.product_code, Product.category, OrderItem.unit_price)
        .join(Product, Product.id == Asset.product_id).outerjoin(OrderItem, OrderItem.id == Asset.source_order_item_id)
        .where(Asset.status == AssetStatus.SELLABLE).order_by(Asset.status_since.asc().nulls_last()).limit(units)).all()
    _short(run, "sales", units, len(rows), "sellable stock")
    channels = _spread(len(rows), rules.CHANNEL_MIX)
    proceeds, priced, reasons = 0.0, 0, Counter()
    for (aid, grade, received, deployed, code, family, unit_price), ch in zip(rows, channels):
        price, why = _sale_price(code, family, grade, received, deployed, unit_price, ch, today)
        if why:
            reasons[why] += 1
        if _try(run, "sales", AssetStatus.SELLABLE, lambda: cycle.sell(db, aid, channel=ch, price=price, actor=actor, today=today).status) and price is not None:
            proceeds, priced = proceeds + price, priced + 1
    if priced:
        run.note(f"{priced:,} priced by the residual curve (seed_daas._residual_share) net of the channel fee: "
                 f"{proceeds:,.0f} EUR, {proceeds / priced:,.0f} EUR a device")
    for why, n in reasons.items():
        run.note(f"{n:,} sold without a price: {why}")
    run.extra["proceeds_eur"] = round(run.extra.get("proceeds_eur", 0.0) + proceeds, 2)   # a rotation sells every day; the answer carries the total


def act_recycling(db: Session, run: Run, *, units: int, today: date, actor: str, stations: dict) -> None:
    """Recycling: the sellable stock nobody bought, oldest first."""
    ids = _pick(db, (AssetStatus.SELLABLE,), units)
    _short(run, "recycling", units, len(ids), "sellable stock")
    for aid in ids:
        _try(run, "recycling", AssetStatus.SELLABLE, lambda: cycle.recycle(db, aid, actor=actor, today=today).status)


# The steps that carry a fact the raw transition cannot write. A free move into them would
# create the very inconsistency cycle.py exists to prevent: a rented device without a
# contract, a sale without a price.
_FACT_STEPS = {AssetStatus.RENTED: "first rentals or second rentals", AssetStatus.RETURNED: "returns",
               AssetStatus.SOLD: "sales", AssetStatus.RECYCLED: "recycling"}


def act_move(db: Session, run: Run, *, from_status: str, to_status: str, units: int, today: date, actor: str, stations: dict) -> None:
    """Move devices between two compartments and let the state machine decide.

    The point of this action is the refusal: a move the lifecycle forbids is refused with
    the machine's own reason, before a single row is touched. A legal warehouse move goes
    through the asset service like every other.
    """
    try:
        frm, to = AssetStatus(from_status), AssetStatus(to_status)
    except ValueError:
        raise ValidationError(f"unknown status: from {from_status!r} to {to_status!r}")
    try:
        lifecycle.assert_transition(frm, to)
    except ValidationError as e:
        run.refuse("move", str(e), units)
        return
    if to in _FACT_STEPS:
        run.refuse("move", f"{_label(to.value)} carries facts a free move cannot write (a contract, a price): use {_FACT_STEPS[to]}", units)
        return
    ids = _pick(db, (frm,), units)
    _short(run, "move", units, len(ids), _label(frm.value))
    comp = warehouse.STATION_OF_STATUS.get(to)
    loc = stations.get(comp.code) if comp else None
    for aid in ids:
        _try(run, "move", frm, lambda: asset_service.transition(db, aid, to, location_id=loc, actor=actor, note="simulation: free move",
                                                                 effective_date=today).status)


def rotation(db: Session, run: Run, *, rates: dict, today: date, actor: str, stations: dict, held: Optional[dict] = None,
             deliveries: bool = True) -> None:
    """One day of the whole cycle at the measured rates; a held station's outflow is scaled by its factor."""
    held = held or {}
    run.quiet = True     # the steps' own lines would repeat once per day; the day and the hold write their summary instead

    def n(code: str, base: Optional[int]) -> int:
        return int(round((base or 0) * held.get(code, 1.0)))

    if deliveries and rates["deliveries_due"]:
        act_delivery(db, run, units=min(MAX_UNITS, rates["deliveries_due"]), due_only=True, today=today, actor=actor, stations=stations)
    if n("ST-NEW", rates["first_rentals_per_day"]):
        act_first_rentals(db, run, units=n("ST-NEW", rates["first_rentals_per_day"]), today=today, actor=actor, stations=stations)
    if rates["returns_per_day"]:
        act_returns(db, run, units=rates["returns_per_day"], today=today, actor=actor, stations=stations)
    chain = rates["chain"]
    if n("ST-RETURNS", chain["ST-RETURNS"]):
        act_intake(db, run, units=n("ST-RETURNS", chain["ST-RETURNS"]), today=today, actor=actor, stations=stations)
    if n("ST-MDM", chain["ST-MDM"]):
        act_mdm_release(db, run, units=n("ST-MDM", chain["ST-MDM"]), today=today, actor=actor, stations=stations)
    if n("ST-WIPE", chain["ST-WIPE"]):
        act_grading(db, run, units=n("ST-WIPE", chain["ST-WIPE"]), today=today, actor=actor, stations=stations)
    if n("ST-REPAIR", chain["ST-REPAIR"]):
        act_repair_done(db, run, units=n("ST-REPAIR", chain["ST-REPAIR"]), today=today, actor=actor, stations=stations)
    if n("ST-REFURB", chain["ST-REFURB"]):
        act_refurb_done(db, run, units=n("ST-REFURB", chain["ST-REFURB"]), today=today, actor=actor, stations=stations)
    if n("ST-SECOND", rates["second_rentals_per_day"]):
        act_second_rentals(db, run, units=n("ST-SECOND", rates["second_rentals_per_day"]), today=today, actor=actor, stations=stations)
    if n("ST-SELL", rates["sales_per_day"]):
        act_sales(db, run, units=n("ST-SELL", rates["sales_per_day"]), today=today, actor=actor, stations=stations)
    run.quiet = False


def act_day(db: Session, run: Run, *, today: date, actor: str, stations: dict) -> None:
    """A day of normal operation: one day passes, then the whole rotation runs once at the rates
    measured when the day began. Run once a day, this is what keeps the dataset from ageing into
    staleness: the fleet moves, and the calendar with it."""
    rates = measure_rates(db, today)
    run.extra["rates"] = rates
    run.notes = [rates["returns_basis"], rates["first_rentals_basis"], rates["second_rentals_basis"], rates["sales_basis"],
                 rates["chain_basis"], rates["deliveries_basis"]]
    t = time.perf_counter()
    world = timeshift.advance(db, 1, action="day")
    run.extra["world"] = {"advanced_days": 1, "advance_ms": round((time.perf_counter() - t) * 1000), **world}
    rotation(db, run, rates=rates, today=today, actor=actor, stations=stations)
    run.notes.insert(0, _world_note(1, world))


def _station_state(db: Session, comp: warehouse.Compartment, today: date) -> dict:
    """On hand, units past the target dwell and the dwell histogram of one compartment, from the grouped read."""
    hist, on_hand, _undated = warehouse._stock(db, today)
    h: dict = {}
    for st in comp.statuses:
        for days, n in hist.get(st, {}).items():
            h[days] = h.get(days, 0) + n
    return {"on_hand": sum(on_hand[st] for st in comp.statuses), "hist": h,
            "past_target": sum(n for d, n in h.items() if d > comp.target_dwell_days)}


def _oldest_past(db: Session, comp: warehouse.Compartment, k: int, cutoff: date) -> int:
    """How many of the ``k`` longest-waiting units of a compartment are past a cutoff date: an ordered index read of k rows."""
    if k <= 0:
        return 0
    sub = (select(Asset.status_since).where(Asset.status.in_(tuple(comp.statuses)), Asset.status_since.is_not(None))
           .order_by(Asset.status_since.asc()).limit(k)).subquery()
    return int(db.scalar(select(func.count()).select_from(sub).where(sub.c.status_since < cutoff)) or 0)


def act_hold(db: Session, run: Run, *, station: str, days: int, factor: float, today: date, actor: str, stations: dict) -> None:
    """A bottleneck: N days pass, and this station does not drain (factor 0), or drains at a fraction of its flow (0.5 halves it).

    Time passes for real. The world moves back by N days once (``timeshift.advance``), so
    the stock already waiting ages N days, the contracts of those days come due and the
    dwell clocks run; then each of the N days runs the rotation with the station's outflow
    throttled, its moves stamped on their own day (``today - (N - k)``), so the arrivals
    are staggered across the hold the way they were. One shift of N is the same end state
    as N shifts of one, at a fraction of the cost of a fleet-wide UPDATE per day. The rates
    are the ones measured when the hold began. Deliveries are left out: they follow order
    dates, not the chain. Beyond the moves, the answer says how many units arrived, how
    many the hold kept that a normal N days would have released, and how many of the
    waiting units crossed the station's target dwell while they waited.
    """
    comp = warehouse.COMPARTMENT_BY_CODE.get(station)
    if comp is None:
        raise NotFoundError(f"No warehouse compartment with code {station!r}")
    flow = OUTFLOW_OF.get(station)
    if flow is None:
        run.refuse("hold", f"{comp.name} is a reserve, not a queue: it has no flow to hold", 1)
        return
    rates = measure_rates(db, today)
    run.extra["rates"] = rates
    before = _station_state(db, comp, today)
    target = comp.target_dwell_days
    # the units that cross the target by waiting alone: those between (target - N) and target days old when the hold begins
    crossed = sum(n for d, n in before["hist"].items() if target - days < d <= target)
    t = time.perf_counter()
    world = timeshift.advance(db, days, action="hold")
    run.extra["world"] = {"advanced_days": days, "advance_ms": round((time.perf_counter() - t) * 1000), **world}
    for k in range(1, days + 1):
        rotation(db, run, rates=rates, today=today - timedelta(days=days - k), actor=actor, stations=stations,
                 held={station: factor}, deliveries=False)
    after = _station_state(db, comp, today)
    rate = {"first_rentals": rates["first_rentals_per_day"], "second_rentals": rates["second_rentals_per_day"],
            "sales": rates["sales_per_day"]}.get(flow, rates["chain"].get(station))
    rate = int(rate or 0)
    would = rate * days
    did = int(round(rate * factor)) * days
    held_back = max(0, min(would - did, after["on_hand"]))
    held_back_past = _oldest_past(db, comp, held_back, today - timedelta(days=target))
    arrived = sum(n for (_f, t), n in run.moved.items() if t in {s.value for s in comp.statuses})
    run.extra["hold"] = {
        "station": station, "name": comp.name, "days": days, "factor": factor, "outflow": flow, "outflow_per_day": rate,
        "on_hand_before": before["on_hand"], "on_hand_after": after["on_hand"], "arrived": arrived,
        "past_target_before": before["past_target"], "past_target_after": after["past_target"], "target_dwell_days": target,
        "crossed_target": crossed, "held_back": held_back, "held_back_past_target": held_back_past,
    }
    what = "did not drain" if factor == 0 else f"drained at {factor:.0%} of its flow"
    run.notes.insert(0, f"{comp.name}: {days} days passed and the station {what}. {arrived:,} arrived, {held_back:,} that a normal "
                        f"{days} days would have released stayed ({held_back_past:,} of them now past the {target}-day target), and "
                        f"{crossed:,} of the units already waiting crossed the target as they waited: {before['past_target']:,} past it "
                        f"before, {after['past_target']:,} after.")
    run.notes.insert(1, _world_note(days, world))


# ---------------------------------------------------------------------------
# the catalogue


@dataclass(frozen=True)
class Param:
    name: str
    kind: str                 # int | float | choice
    label: str
    default: Any = None       # a value, or a function of the measured rates
    lo: Optional[float] = None
    hi: Optional[float] = None
    choices: Optional[str] = None   # stations | statuses | products
    hint: str = ""


@dataclass(frozen=True)
class ActionDef:
    id: str
    name: str
    description: str
    params: tuple
    run: Callable


def _units(default, hint: str = "") -> Param:
    return Param("units", "int", "Devices", default, 1, MAX_UNITS, None, hint)


ACTIONS: tuple = (
    ActionDef("delivery", "An inbound delivery arrives",
              "Units of a model land in new stock, received against the open order lines with the earliest date. A partial delivery is a partial receipt.",
              (_units(lambda r: min(MAX_UNITS, r["deliveries_due"]) or DEFAULT_UNITS, "default: what is due for delivery today"),
               Param("product_code", "choice", "Model", None, choices="products", hint="left empty, the earliest open line of any model")),
              act_delivery),
    ActionDef("first_rentals", "Devices go out to customers", "First rentals start from new stock, the oldest stock first. Each one gets a contract.",
              (_units(lambda r: r["first_rentals_per_day"] or DEFAULT_UNITS, "default: the measured first rentals a day"),), act_first_rentals),
    ActionDef("returns", "Rentals come back", "The contracts nearest their end (overdue first) end, the devices arrive in returns intake.",
              (_units(lambda r: r["returns_per_day"] or DEFAULT_UNITS, "default: what the return calendar says comes back a day"),), act_returns),
    ActionDef("intake", "Returns are booked in", "Intake is done; the devices wait in the MDM release hold for the old customer.",
              (_units(lambda r: r["chain"]["ST-RETURNS"] or DEFAULT_UNITS, "default: the chain's daily flow"),), act_intake),
    ActionDef("mdm_release", "The old customer releases from its MDM", "The longest-waiting devices leave the hold for wipe and grading.",
              (_units(lambda r: r["chain"]["ST-MDM"] or DEFAULT_UNITS, "default: the chain's daily flow"),), act_mdm_release),
    ActionDef("grading", "Wipe and grading clears a batch", "Grades in the fleet's mix; A and B on to refurbishment, C to repair, D to sale, a share recycled.",
              (_units(lambda r: r["chain"]["ST-WIPE"] or DEFAULT_UNITS, "default: the chain's daily flow"),), act_grading),
    ActionDef("repair_done", "The repair partner finishes a batch", "An invoice per device; the devices go on to refurbishment.",
              (_units(lambda r: r["chain"]["ST-REPAIR"] or DEFAULT_UNITS, "default: the chain's daily flow"),), act_repair_done),
    ActionDef("refurb_done", "Refurbishment finishes", "An invoice per device; the devices wait in the second-life stock.",
              (_units(lambda r: r["chain"]["ST-REFURB"] or DEFAULT_UNITS, "default: the chain's daily flow"),), act_refurb_done),
    ActionDef("second_rentals", "Second rentals start", "From the second-life stock, the oldest first. Each one gets a contract.",
              (_units(lambda r: r["second_rentals_per_day"] or DEFAULT_UNITS, "default: the measured second rentals a day"),), act_second_rentals),
    ActionDef("sales", "Sales complete", "The oldest sellable stock is sold at the price the fleet's own residual curve gives, net of the channel fee.",
              (_units(lambda r: r["sales_per_day"] or DEFAULT_UNITS, "default: the measured sales a day"),), act_sales),
    ActionDef("recycling", "Recycling", "The sellable stock nobody bought, oldest first, leaves the fleet.",
              (_units(10),), act_recycling),
    ActionDef("day", "A day of normal operation",
              "The whole rotation once at today's rates, every rate measured from the fleet. Run it once a day and the dataset lives at the pace of the business.",
              (), act_day),
    ActionDef("hold", "A bottleneck",
              "Hold a station for N days: the MDM partner stops releasing, the repair partner halves its throughput. A queue builds; watch what it does.",
              (Param("station", "choice", "Station", "ST-MDM", choices="stations"),
               Param("days", "int", "Days", 5, 1, MAX_DAYS),
               Param("factor", "float", "Outflow kept", 0.0, 0.0, 1.0, hint="0 = stopped, 0.5 = halved")),
              act_hold),
    ActionDef("move", "Move devices, the state machine decides",
              "Any compartment to any other. A move the lifecycle forbids is refused with its reason before a row is touched.",
              (Param("from_status", "choice", "From", "READY_SECOND", choices="statuses"),
               Param("to_status", "choice", "To", "RETURNED", choices="statuses"),
               _units(10)),
              act_move),
)
ACTION_BY_ID = {a.id: a for a in ACTIONS}
CYCLE_STATUSES = [s.value for s in (AssetStatus.RECEIVED, AssetStatus.IN_STORAGE, AssetStatus.RENTED, AssetStatus.RETURNED,
                                    AssetStatus.MDM_RELEASE, AssetStatus.WIPE_GRADING, AssetStatus.REPAIR, AssetStatus.REFURB,
                                    AssetStatus.READY_SECOND, AssetStatus.SELLABLE, AssetStatus.SWAP_BUFFER, AssetStatus.SOLD, AssetStatus.RECYCLED)]


def _resolve(p: Param, rates: Optional[dict]):
    if callable(p.default):
        return p.default(rates) if rates is not None else None
    return p.default


def catalogue(db: Session, *, today: Optional[date] = None) -> dict:
    """The actions with their defaults filled from today's measured rates, and the choices the forms need."""
    today = today or date.today()
    daas = _is_daas(db)
    rates = measure_rates(db, today) if daas else None
    products = [{"code": code, "name": name} for code, name in db.execute(
        select(Product.product_code, Product.name).where(Product.id.in_(
            select(OrderItem.product_id).join(PurchaseOrder, PurchaseOrder.id == OrderItem.order_id).where(PurchaseOrder.status.in_(OPEN_ORDER))))
        .order_by(Product.name)).all()]
    choices = {
        "stations": [{"code": c.code, "name": c.name} for c in warehouse.COMPARTMENTS if c.code in OUTFLOW_OF],
        "statuses": [{"code": s, "name": _label(s)} for s in CYCLE_STATUSES],
        "products": products,
    }
    actions = [{
        "id": a.id, "name": a.name, "description": a.description,
        "params": [{"name": p.name, "kind": p.kind, "label": p.label, "default": _resolve(p, rates), "min": p.lo, "max": p.hi,
                    "choices": p.choices, "hint": p.hint} for p in a.params],
    } for a in ACTIONS]
    return {"scenario": ("daas" if daas else "datacenter"), "as_of": today, "max_units": MAX_UNITS, "max_days": MAX_DAYS,
            "calendar_note": CALENDAR_NOTE, "world": timeshift.state(db), "rates": rates, "choices": choices, "actions": actions,
            "kpis": list(SIM_KPIS),
            "reason": (None if daas else "the simulation runs on the device fleet; this database holds the datacenter operation")}


# ---------------------------------------------------------------------------
# fire one action and answer with what it changed


def _kpi_delta(k: kpis.KpiDef, before: dict, after: dict) -> dict:
    b, a = before["current"], after["current"]
    delta = (round(a - b, 4) if (a is not None and b is not None) else None)
    better = None
    if delta:
        better = (delta < 0) if k.direction == "lower" else (delta > 0)
    return {"id": k.id, "name": k.name, "unit": k.unit, "direction": k.direction, "before": b, "after": a, "delta": delta, "better": better,
            "before_reason": before["reason"], "after_reason": after["reason"],
            "before_measured_at": before["measured_at"], "after_measured_at": after["measured_at"],
            "after_measured_on": after["measured_on"], "after_stale_days": after["stale_days"]}


def _compartment_delta(c0: dict, c1: dict, p0: Optional[dict], p1: Optional[dict]) -> dict:
    return {
        "code": c0["code"], "name": c0["name"], "step": c0["step"], "capacity": c1["capacity"],
        "on_hand_before": c0["on_hand"], "on_hand_after": c1["on_hand"], "delta": c1["on_hand"] - c0["on_hand"],
        "over_capacity_before": c0["over_capacity"], "over_capacity_after": c1["over_capacity"],
        "past_target_before": c0["past_target_units"], "past_target_after": c1["past_target_units"], "target_dwell_days": c0["target_dwell_days"],
        "median_before": c0["median_days"], "median_after": c1["median_days"],
        "verdict_before": c0["verdict"], "verdict_after": c1["verdict"],
        "breach_before": (p0["breach_state"] if p0 else None), "breach_after": (p1["breach_state"] if p1 else None),
        "breach_month_before": (p0["breach_month"] if p0 else None), "breach_month_after": (p1["breach_month"] if p1 else None),
    }


def fire(db: Session, action_id: str, params: Optional[dict] = None, *, actor: Optional[str] = None, today: Optional[date] = None) -> dict:
    """Fire one action and answer with what it changed: the moves, the refusals, the compartments,
    the capacity plan's breach per compartment, whether and how far the world moved, and the KPIs
    before and after. An event re-measures what an event can move, the fleet and warehouse KPIs,
    whether or not days passed; the rest are deliberately not re-measured inside the action (the
    forecast backtest alone would cost more than all of them together), and the answer lists
    them, with the world-day each was measured on, so a figure measured before the calendar moved
    never looks like today's. Measure again on the KPIs tab, or the daily boot, brings them current."""
    assert_demo_write_allowed("a simulated event")
    today = today or date.today()
    a = ACTION_BY_ID.get(action_id)
    if a is None:
        raise NotFoundError(f"no simulation action {action_id!r}")
    if not _is_daas(db):
        raise ValidationError("the simulation runs on the device fleet; this database holds the datacenter operation")
    params = params or {}
    rates = measure_rates(db, today) if any(callable(p.default) for p in a.params) else None
    kw: dict = {}
    for p in a.params:
        v = params.get(p.name)
        if v is None:
            v = _resolve(p, rates)
        if v is None and p.kind != "choice":
            raise ValidationError(f"{a.name}: {p.label} is needed and the fleet gives no default for it")
        if p.kind in ("int", "float") and v is not None:
            if (p.lo is not None and v < p.lo) or (p.hi is not None and v > p.hi):
                raise ValidationError(f"{a.name}: {p.label} has to be between {p.lo:g} and {p.hi:g} (an action touches N devices, never the fleet)")
            v = int(v) if p.kind == "int" else float(v)
        kw[p.name] = v

    ran_at = datetime.now(timezone.utc)
    t0 = time.perf_counter()
    W0 = warehouse.compartments(db, today=today)
    P0 = capacity_plan.plan(db, today=today)
    K0 = {r["id"]: r for r in kpis.compute_all(db, today=today)}      # today's measurement, taken now where it is missing
    t1 = time.perf_counter()
    run = Run(action_id)
    a.run(db, run, today=today, actor=actor or ACTOR, stations=cycle.stations(db), **kw)
    db.flush()
    t2 = time.perf_counter()
    W1 = warehouse.compartments(db, today=today)
    P1 = capacity_plan.plan(db, today=today)
    t3 = time.perf_counter()
    K1 = {r["id"]: r for r in kpis.compute_all(db, today=today, refresh=True, only=set(SIM_KPIS))}
    t4 = time.perf_counter()
    world = run.extra.get("world") or {"advanced_days": 0, **timeshift.state(db)}
    kept = [{"id": k.id, "name": k.name, "measured_on": K1[k.id]["measured_on"], "stale_days": K1[k.id]["stale_days"]}
            for k in kpis.KPIS if k.id not in SIM_KPIS]

    p0 = {c["code"]: c for c in P0["compartments"]}
    p1 = {c["code"]: c for c in P1["compartments"]}
    comps = [_compartment_delta(c0, c1, p0.get(c0["code"]), p1.get(c0["code"])) for c0, c1 in zip(W0["compartments"], W1["compartments"])]
    return {
        "action": a.id, "name": a.name, "requested": kw, "as_of": today, "ran_at": ran_at, "actor": actor or ACTOR,
        "moved": [{"from_status": f, "to_status": t, "from_name": _label(f), "to_name": _label(t), "units": n} for (f, t), n in run.moved.items()],
        "moved_total": run.moved_total,
        "refused": [{"what": w, "reason": r, "units": n} for (w, r), n in run.refused.items()],
        "refused_total": sum(run.refused.values()),
        "notes": run.notes,
        "rates": run.extra.get("rates"),
        "hold": run.extra.get("hold"),
        "world": world,
        "proceeds_eur": run.extra.get("proceeds_eur"),
        "compartments": comps,
        "warehouse": {"on_hand_before": W0["on_hand"], "on_hand_after": W1["on_hand"],
                      "over_capacity_before": W0["over_capacity"], "over_capacity_after": W1["over_capacity"]},
        "plan": {"first_breach_before": P0["first_breach"], "first_breach_after": P1["first_breach"],
                 "fleet_before": P0["fleet_now"], "fleet_after": P1["fleet_now"]},
        "kpis": [_kpi_delta(k, K0[k.id], K1[k.id]) for k in kpis.KPIS],
        "kpis_measured": list(SIM_KPIS),
        "kpis_kept": kept,
        "kpis_kept_reason": KEPT_REASON,
        "timing_ms": {"reads_before": round((t1 - t0) * 1000), "action": round((t2 - t1) * 1000),
                      "reads_after": round((t3 - t2) * 1000), "kpis": round((t4 - t3) * 1000)},
        "calendar_note": CALENDAR_NOTE,
    }

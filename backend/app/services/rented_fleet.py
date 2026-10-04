"""The rented fleet by customer, and what lands at the warehouse when.

The business owner's question: how many devices are out, at which customers, and how
many come back to us in which month, beside what arrives from suppliers in the same
month, so a reorder can be timed. Every figure here is counted in the database from the
rows that exist; nothing is estimated, and what the model cannot tell is named as such.

**At a customer** means what the rest of the fleet module means by "rented": the
asset's status is ``RENTED``. The asset carries the customer while it is rented
(``Asset.customer_id``, set by ``cycle.rent`` and cleared by ``cycle.take_back``), and
the device has one running rental contract. The contract figures (active, ending in 90
days, overdue, the month a device comes back) are read from ``rental_contract`` with
``status = RUNNING``; the two counts should agree, and the totals carry both so a
mismatch would show rather than hide.

**A customer** is an organisation that is neither a supplier nor a manufacturer. The
second condition matters: the datacenter seed records manufacturers that are not
suppliers (``seed.py``), and they are not customers.

**Since** is the start of the customer's first rental contract. The model stores no
onboarding date for a customer: the seed draws one to place the first contract, but
writes only the contract, and the onboarding columns on ``organization`` belong to the
supplier gate.

**Comes back** is the planned end of a running contract. A running contract whose
planned end has passed is overdue: the device is still out after its contract ended. It
is counted once, in its own bucket, never again in the current month.

**Arrives from suppliers** is the outstanding quantity of open order lines
(``planning.inbound_pipeline``, the same lines the purchasing agent and the guard net),
bucketed by the line's estimated delivery date. A line whose date has passed is late and
gets its own bucket, as does a line without a date. Requisitions that are staged but not
ordered are not purchase orders and are not counted.

**Defects** are read from what the model records: a rental that ended for a defect or a
swap (``rental_contract.end_reason``), devices in repair now by the day they entered it,
repairs the partner invoiced (``service_event``), and devices recycled, the exit without
proceeds. The replacement device a defect consumed is not recorded: no row links a
defect return to the swap-buffer device that replaced it, and the movement log that
would show swap-buffer devices going out holds no seeded history. That figure is left
out and named in ``omissions``.

Three limits of those figures are said wherever they are read. **Devices in repair now**
are every device in REPAIR, whatever sent it there: a defect or a grade C return (the
device row does not say which). The key is ``in_repair_now``, not a defect count.
**Defect returns in the seeded data** exist only for the devices that are in repair on
the seed day (``app.seed_daas`` marks a share of them as defects, and their rentals ended
in the weeks before it); the seed writes no defect history before that, and the demo
simulation ends rentals as planned or early only, so every other month reads 0. **Repairs
invoiced in the seeded data** stop short of the seed day: the seed writes a repair invoice
only for a grade C device that went on to a second rental, dated at least eight days
before that rental began (the seed day at the latest), and the devices in repair on the
seed day carry none yet. A current month that began less than eight days before the seed
day therefore reads 0 until the simulation's repair-done event writes new invoices. Those
zeros are a property of the simulated data, not of the business, and ``omissions`` and
the ``defects`` basis say so. The last month of the defects grid is the current one, up
to ``as_of``.

**What lands is not cover.** The month grid adds returns and inbound as ``landing``: the
volume that physically arrives at the warehouse, not stock that covers demand. The
ordering mask (``order_mask._recommend``) and the agent's ``planning.inventory_position``
subtract stock on hand, open orders and staged requisitions, not devices still to come
back from customers; the capacity plan (``capacity_plan._throughput``) does count
returns, the fleet divided by the term, as rented out again unless they are sold or
recycled. This module does not say how many returns will be rented out again, and
whether the ordering mask should count them is not settled here.

**Everything that grows with the fleet aggregates in the database**, grouped by
customer, by date or by category, so the answer is a few thousand rows whatever the size
of the fleet. The open order lines are bucketed here, one row per line as
``planning.inbound_pipeline`` returns them: as many rows as open lines, not devices.

**Which headline counts what.** ``customers()`` counts the rented devices and the running
contracts per customer once. Its totals of customers, devices at customers, running
contracts, contracts ending in 90 days and overdue returns are counted over its rows, so
such a headline and the column beneath it are one definition. ``rented_total`` is counted
apart: every asset with status RENTED, at a listed customer or not, and
``devices_without_customer`` is the difference. The month grid counts every running
contract, whichever organisation holds it, so its overdue row exceeds the customers'
overdue total when an organisation flagged as a supplier or a manufacturer holds a running
contract; the page shows that difference. ``overview()`` carries only what lands at the
warehouse and the defects, and does not count the fleet a second time for the same page.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import and_, case, func, select
from sqlalchemy.orm import Session

from app.models.catalog import Organization, Product
from app.models.flow import Asset, AssetStatus
from app.models.rental import ContractStatus, RentalContract
from app.models.tco import ServiceEvent, ServiceKind
from app.services import fleet, planning
from app.services.exceptions import NotFoundError

MONTHS = 12                     # the time grid of the overview and of the stream
ENDING_WINDOW_DAYS = 90         # "contracts ending in 90 days", the window of fleet.summary's returns_due_90d
DEFECT_REASONS = ("defect", "swap")   # the end reasons order_mask counts as defects
DETAIL_LIMIT = 500              # rows of a customer's contracts ending in the window, soonest first

DEFINITION = ("A device is at a customer while its asset status is RENTED: it carries that customer on the asset "
              "and has one running rental contract.")

SEEDED_DEFECTS = ("in the seeded data (app.seed_daas) a rental ends for a defect only for the devices that are in repair "
                  "on the seed day, and those rentals ended in the weeks before it; the seed writes no defect history before "
                  "that, and the demo simulation ends rentals as planned or early only, so every other month reads 0. "
                  "Those zeros are a property of the simulated data, not of the business")

SEEDED_REPAIRS = ("in the seeded data (app.seed_daas) a repair invoice exists only for a grade C device that went on to a "
                  "second rental, dated at least eight days before that rental began, and the devices in repair on the seed "
                  "day carry none yet; so a current month that began less than eight days before the seed day reads 0 until "
                  "the simulation's repair-done event writes new invoices. Those zeros are a property of the simulated data, "
                  "not of the business")

BASIS = {
    "customers": ("organisations that are neither a supplier nor a manufacturer, whether or not they hold a device now; "
                  "one row each"),
    "since": "start of the customer's first rental contract; the model stores no onboarding date for a customer",
    "devices_at_customer": "assets with status RENTED, by the customer on the asset",
    "contracts_active": "rental contracts with status RUNNING",
    "contracts_ending_90d": (f"running contracts whose planned end is between today and today + {ENDING_WINDOW_DAYS} days; "
                             "the total is the sum over the customers' rows"),
    "returns_overdue": ("running contracts whose planned end has passed: the device is still out after its contract ended; "
                        "the total is the sum over the customers' rows"),
    "returns_by_month": "a customer's running contracts by the month of their planned end, from today; overdue ones are not repeated",
    "returns_schedule": ("every running contract, whichever organisation holds it, by the month of its planned end, from today; "
                         "overdue ones are in returns_overdue_total"),
    "returns_overdue_total": "every running contract whose planned end has passed, whichever organisation holds it",
    "returns_after_window": "every running contract whose planned end is after the last month of the grid",
    "rented_total": "every asset with status RENTED, whether or not its customer is one of the rows",
    "customers_holding_devices": "customers with at least one asset with status RENTED",
    "by_category": "product category of the device",
    "inbound_open_pos": ("outstanding units (ordered minus received) of open purchase order lines "
                         "(PENDING, APPROVED, PLACED, PARTIALLY_RECEIVED), by the month of the line's estimated delivery date"),
    "inbound_open_pos_late": "the same lines whose estimated delivery date is before as_of",
    "inbound_open_pos_no_eta": "the same lines without an estimated delivery date",
    "inbound_open_pos_after_window": "the same lines whose estimated delivery date is after the last month of the grid",
    "inbound_open_total": "every outstanding unit on the open lines: the months, late, no date and after the grid together",
    "defects": ("reported: rentals ended for a defect or a swap, by the end date; in_repair: devices in repair now, by the day "
                "they entered repair; repairs_invoiced: repair partner invoices, by the invoice date; written_off: devices "
                "recycled, the exit without proceeds, by the exit date. The last month is the current one, up to as_of. "
                "Reported: " + SEEDED_DEFECTS + ". Repairs invoiced: " + SEEDED_REPAIRS),
    "in_repair_now": ("every asset with status REPAIR, whatever sent it there (a defect or a grade C return): not a count of "
                      "defects. defects[].in_repair spreads it over the months the devices entered repair; "
                      "in_repair_before_window and in_repair_undated hold the rest"),
    "in_repair_before_window": "devices in repair now that entered repair before the first month of the defects grid",
    "in_repair_undated": "devices in repair now without the date they entered it: in the total, in no month",
}

# The overview grid's "landing" column, explained beside it. The data stream carries no
# landing: it keeps returns and inbound apart.
LANDING = ("returns plus inbound in the month: the volume that physically arrives at the warehouse, not stock that covers "
           "demand. The ordering mask subtracts stock on hand, open orders and staged requisitions, not devices still to "
           "come back from customers; the capacity plan counts returns as rented out again unless they are sold or recycled. "
           "This figure does not say how many returns will be rented out again")

OMISSIONS = [
    "defects[].replacements_consumed: no row links a defect return to the replacement device, and the movement log "
    "(asset_event) that would show swap-buffer devices going out holds no seeded history, so seeded months would read "
    "as false zeros",
    "defects[].reported, the history: " + SEEDED_DEFECTS,
    "defects[].repairs_invoiced, the newest days: " + SEEDED_REPAIRS,
]


# ---------------------------------------------------------------------------
# the month grid


def _month_key(d) -> str:
    return fleet._as_date(d).strftime("%Y-%m")


def _add_months(first: date, n: int) -> date:
    y, m = divmod(first.month - 1 + n, 12)
    return date(first.year + y, m + 1, 1)


def month_keys(today: date, months: int = MONTHS) -> list[str]:
    """The current month and the ``months - 1`` after it, as YYYY-MM."""
    first = date(today.year, today.month, 1)
    return [_add_months(first, i).strftime("%Y-%m") for i in range(months)]


def past_month_keys(today: date, months: int = MONTHS) -> list[str]:
    """The current month and the ``months - 1`` before it, oldest first."""
    first = date(today.year, today.month, 1)
    return [_add_months(first, i - months + 1).strftime("%Y-%m") for i in range(months)]


def _horizon(today: date, months: int) -> date:
    """The first day after the grid: the first of the month after its last month."""
    return _add_months(date(today.year, today.month, 1), months)


def _cat(c: Optional[str]) -> str:
    return c or "Uncategorised"


def _calendar_months(cal: list[dict]) -> list[dict]:
    """fleet.return_calendar's months as {month, devices, by_category}, its "?" family under this module's label."""
    out = []
    for m in cal:
        by: dict[str, int] = {}
        for fam, n in m["by_family"].items():
            k = _cat(None if fam == "?" else fam)
            by[k] = by.get(k, 0) + int(n)
        out.append({"month": m["month"], "devices": m["total"], "by_category": by})
    return out


# ---------------------------------------------------------------------------
# per customer


def _customer_orgs(db: Session) -> list[tuple[str, Optional[str], str]]:
    return db.execute(
        select(Organization.id, Organization.code, Organization.name)
        .where(Organization.is_supplier.is_(False), Organization.is_manufacturer.is_(False))
    ).all()


def _contract_counts(db: Session, today: date, customer_id: Optional[str] = None) -> dict[str, dict[str, int]]:
    """Per customer: running contracts, those ending in the window, and the overdue ones. One grouped query."""
    until = today + timedelta(days=ENDING_WINDOW_DAYS)
    crit = [RentalContract.status == ContractStatus.RUNNING]
    if customer_id is not None:
        crit.append(RentalContract.customer_id == customer_id)
    rows = db.execute(
        select(RentalContract.customer_id, func.count(RentalContract.id),
               func.sum(case((and_(RentalContract.planned_end >= today, RentalContract.planned_end <= until), 1), else_=0)),
               func.sum(case((RentalContract.planned_end < today, 1), else_=0)))
        .where(*crit).group_by(RentalContract.customer_id)
    ).all()
    return {cid: {"active": int(n or 0), "ending": int(e or 0), "overdue": int(o or 0)} for cid, n, e, o in rows}


def _first_start(db: Session, customer_id: Optional[str] = None) -> dict[str, date]:
    stmt = select(RentalContract.customer_id, func.min(RentalContract.start_date)).group_by(RentalContract.customer_id)
    if customer_id is not None:
        stmt = stmt.where(RentalContract.customer_id == customer_id)
    return {cid: fleet._as_date(d) for cid, d in db.execute(stmt).all() if d is not None}


def _devices_by_category(db: Session, customer_id: Optional[str] = None) -> dict[Optional[str], dict[str, int]]:
    """Rented devices per customer and category; the key None collects rented devices without a customer."""
    stmt = (select(Asset.customer_id, Product.category, func.count(Asset.id))
            .join(Product, Product.id == Asset.product_id)
            .where(Asset.status == AssetStatus.RENTED)
            .group_by(Asset.customer_id, Product.category))
    if customer_id is not None:
        stmt = stmt.where(Asset.customer_id == customer_id)
    out: dict[Optional[str], dict[str, int]] = defaultdict(dict)
    for cid, cat, n in db.execute(stmt).all():
        k = _cat(cat)
        out[cid][k] = out[cid].get(k, 0) + int(n)
    return out


def _returns_by_month(db: Session, today: date, months: int, customer_id: Optional[str] = None) -> dict[str, dict[str, int]]:
    """Per customer, running contracts by the month of their planned end, from today to the end of the grid."""
    crit = [RentalContract.status == ContractStatus.RUNNING,
            RentalContract.planned_end >= today, RentalContract.planned_end < _horizon(today, months)]
    if customer_id is not None:
        crit.append(RentalContract.customer_id == customer_id)
    out: dict[str, dict[str, int]] = defaultdict(dict)
    for cid, d, n in db.execute(
        select(RentalContract.customer_id, RentalContract.planned_end, func.count(RentalContract.id))
        .where(*crit).group_by(RentalContract.customer_id, RentalContract.planned_end)
    ).all():
        k = _month_key(d)
        out[cid][k] = out[cid].get(k, 0) + int(n)
    return out


def _row(org_id: str, code: Optional[str], name: str, *, devices: dict[str, int], counts: dict[str, int],
         since: Optional[date], returns: dict[str, int], keys: list[str]) -> dict:
    return {
        "customer_code": code,
        "customer": name,
        "since": since,
        "devices_at_customer": sum(devices.values()),
        "contracts_active": counts.get("active", 0),
        "contracts_ending_90d": counts.get("ending", 0),
        "returns_overdue": counts.get("overdue", 0),
        "by_category": dict(sorted(devices.items(), key=lambda kv: (-kv[1], kv[0]))),
        "returns_by_month": {k: returns.get(k, 0) for k in keys},
    }


def customers(db: Session, today: Optional[date] = None, *, months: int = MONTHS) -> dict:
    """Every customer with the devices it holds now and the contracts behind them, most devices first.

    ``devices_at_customer`` counts assets with status RENTED by the customer on the asset
    (see the module docstring); the contract figures count running contracts.
    """
    today = today or date.today()
    keys = month_keys(today, months)
    devices = _devices_by_category(db)
    counts = _contract_counts(db, today)
    since = _first_start(db)
    returns = _returns_by_month(db, today, months)
    rows = [_row(oid, code, name, devices=devices.get(oid, {}), counts=counts.get(oid, {}), since=since.get(oid),
                 returns=returns.get(oid, {}), keys=keys)
            for oid, code, name in _customer_orgs(db)]
    rows.sort(key=lambda r: (-r["devices_at_customer"], r["customer_code"] or "", r["customer"]))
    rented_total = int(db.scalar(select(func.count(Asset.id)).where(Asset.status == AssetStatus.RENTED)) or 0)
    return {
        "as_of": today,
        "definition": DEFINITION,
        "months": keys,
        "totals": {
            "customers": len(rows),
            "customers_holding_devices": sum(1 for r in rows if r["devices_at_customer"]),
            "devices_at_customer": sum(r["devices_at_customer"] for r in rows),
            "contracts_active": sum(r["contracts_active"] for r in rows),
            "contracts_ending_90d": sum(r["contracts_ending_90d"] for r in rows),
            "returns_overdue": sum(r["returns_overdue"] for r in rows),
            "rented_total": rented_total,
            # rented devices that carry no customer, or a customer that is not one of the rows
            "devices_without_customer": rented_total - sum(r["devices_at_customer"] for r in rows),
        },
        "rows": rows,
    }


def customer_detail(db: Session, code: str, today: Optional[date] = None, *, months: int = MONTHS,
                    limit: int = DETAIL_LIMIT) -> dict:
    """One customer: its row, its devices by model, its returns by month and the contracts ending in 90 days."""
    today = today or date.today()
    org = db.execute(
        select(Organization.id, Organization.code, Organization.name)
        .where(Organization.code == code, Organization.is_supplier.is_(False), Organization.is_manufacturer.is_(False))
    ).first()
    if org is None:
        raise NotFoundError(f"No customer with code {code!r}")
    oid = org.id
    keys = month_keys(today, months)
    row = _row(oid, org.code, org.name, devices=_devices_by_category(db, oid).get(oid, {}),
               counts=_contract_counts(db, today, oid).get(oid, {}), since=_first_start(db, oid).get(oid),
               returns=_returns_by_month(db, today, months, oid).get(oid, {}), keys=keys)

    by_model = [
        {"product_code": pc, "product": name, "category": cat, "devices": int(n)}
        for pc, name, cat, n in db.execute(
            select(Product.product_code, Product.name, Product.category, func.count(Asset.id))
            .join(Product, Product.id == Asset.product_id)
            .where(Asset.status == AssetStatus.RENTED, Asset.customer_id == oid)
            .group_by(Product.product_code, Product.name, Product.category)
        ).all()
    ]
    by_model.sort(key=lambda r: (-r["devices"], r["product"]))

    cal = fleet.return_calendar(db, today=today, months=months, since=today, customer_id=oid)
    returns = _calendar_months(cal)

    until = today + timedelta(days=ENDING_WINDOW_DAYS)
    ending = [
        {"contract_id": c.id, "serial_number": a.serial_number, "product": p.name, "category": p.category,
         "cycle_no": c.cycle_no, "start_date": c.start_date, "term_months": c.term_months, "planned_end": c.planned_end,
         "days_left": (fleet._as_date(c.planned_end) - today).days}
        for c, a, p in db.execute(
            select(RentalContract, Asset, Product)
            .join(Asset, Asset.id == RentalContract.asset_id)
            .join(Product, Product.id == Asset.product_id)
            .where(RentalContract.customer_id == oid, RentalContract.status == ContractStatus.RUNNING,
                   RentalContract.planned_end >= today, RentalContract.planned_end <= until)
            .order_by(RentalContract.planned_end, Asset.serial_number).limit(limit)
        ).all()
    ]
    return {
        "as_of": today, "definition": DEFINITION, **row,
        "by_model": by_model,
        "returns_schedule": returns,
        "contracts_ending": ending,
        "contracts_ending_shown": len(ending),
        "contracts_ending_limit": limit,
    }


# ---------------------------------------------------------------------------
# what lands at the warehouse when: back from customers, in from suppliers


def _running_by_category(db: Session, *crit) -> dict[str, int]:
    out: dict[str, int] = {}
    for cat, n in db.execute(
        select(Product.category, func.count(RentalContract.id))
        .join(Asset, Asset.id == RentalContract.asset_id)
        .join(Product, Product.id == Asset.product_id)
        .where(RentalContract.status == ContractStatus.RUNNING, *crit)
        .group_by(Product.category)
    ).all():
        out[_cat(cat)] = out.get(_cat(cat), 0) + int(n)
    return out


def _bucket() -> dict:
    return {"units": 0, "by_category": {}}


def _add(b: dict, cat: str, n: int) -> None:
    b["units"] += n
    b["by_category"][cat] = b["by_category"].get(cat, 0) + n


def inbound(db: Session, today: Optional[date] = None, *, months: int = MONTHS) -> dict:
    """Open purchase order lines by the month they are expected, with late, undated and later lines apart."""
    today = today or date.today()
    keys = month_keys(today, months)
    horizon = _horizon(today, months)
    lines = planning.inbound_pipeline(db, as_of=today)
    cats = dict(db.execute(select(Product.id, Product.category)
                           .where(Product.id.in_({r["product_id"] for r in lines}))).all()) if lines else {}
    grid = {k: _bucket() for k in keys}
    late, undated, later = _bucket(), _bucket(), _bucket()
    for r in lines:
        n = int(r["outstanding"])
        cat = _cat(cats.get(r["product_id"]))
        eta = r["estimated_delivery_date"]
        if eta is None:
            _add(undated, cat, n)
        elif fleet._as_date(eta) < today:
            _add(late, cat, n)
        elif fleet._as_date(eta) >= horizon:
            _add(later, cat, n)
        else:
            _add(grid[_month_key(eta)], cat, n)
    return {
        "months": [{"month": k, **grid[k]} for k in keys],
        "late": late, "no_eta": undated, "after_window": later,
        "total": sum(int(r["outstanding"]) for r in lines),
        "lines": len(lines),
    }


def returns(db: Session, today: Optional[date] = None, *, months: int = MONTHS) -> dict:
    """Devices coming back from customers: overdue now, then by the month of the planned end, then later.

    The three parts partition the running contracts by planned end, so they add up to the
    contracts running. Contracts ending after the grid are counted, not split by category:
    that is most of the fleet, and the split would cost a join over it for a figure the
    overview does not show.
    """
    today = today or date.today()
    cal = fleet.return_calendar(db, today=today, months=months, since=today)
    overdue = _running_by_category(db, RentalContract.planned_end < today)
    later = int(db.scalar(select(func.count(RentalContract.id)).where(
        RentalContract.status == ContractStatus.RUNNING, RentalContract.planned_end >= _horizon(today, months))) or 0)
    return {
        "months": _calendar_months(cal),
        "overdue": {"devices": sum(overdue.values()), "by_category": overdue},
        "after_window": {"devices": later},
    }


def defects(db: Session, today: Optional[date] = None, *, months: int = MONTHS) -> dict:
    """Per month of the last ``months``: defects reported, devices in repair, repairs invoiced, devices written off."""
    today = today or date.today()
    keys = past_month_keys(today, months)
    start = date.fromisoformat(keys[0] + "-01")
    grid = {k: {"month": k, "reported": 0, "in_repair": 0, "repairs_invoiced": 0, "written_off": 0} for k in keys}

    def put(field: str, rows) -> int:
        """Bucket (date, count) rows into the grid; return the units dated before the window."""
        before = 0
        for d, n in rows:
            k = _month_key(d)
            if k in grid:
                grid[k][field] += int(n)
            elif fleet._as_date(d) < start:
                before += int(n)
        return before

    put("reported", db.execute(
        select(RentalContract.actual_end, func.count(RentalContract.id))
        .where(RentalContract.status == ContractStatus.ENDED, RentalContract.end_reason.in_(DEFECT_REASONS),
               RentalContract.actual_end >= start, RentalContract.actual_end <= today)
        .group_by(RentalContract.actual_end)).all())
    repair_rows = db.execute(
        select(Asset.status_since, func.count(Asset.id))
        .where(Asset.status == AssetStatus.REPAIR).group_by(Asset.status_since)).all()
    in_repair_undated = sum(int(n) for d, n in repair_rows if d is None)
    in_repair_before = put("in_repair", [(d, n) for d, n in repair_rows if d is not None])
    put("repairs_invoiced", db.execute(
        select(ServiceEvent.event_date, func.count(ServiceEvent.id))
        .where(ServiceEvent.kind == ServiceKind.REPAIR, ServiceEvent.event_date >= start, ServiceEvent.event_date <= today)
        .group_by(ServiceEvent.event_date)).all())
    put("written_off", db.execute(
        select(Asset.sold_date, func.count(Asset.id))
        .where(Asset.status == AssetStatus.RECYCLED, Asset.sold_date >= start, Asset.sold_date <= today)
        .group_by(Asset.sold_date)).all())
    return {
        "months": [grid[k] for k in keys],
        "in_repair_now": sum(int(n) for _, n in repair_rows),
        "in_repair_before_window": in_repair_before,
        "in_repair_undated": in_repair_undated,
        "basis": BASIS["defects"],
        "omissions": list(OMISSIONS),
    }


def overview(db: Session, today: Optional[date] = None, *, months: int = MONTHS) -> dict:
    """What lands at the warehouse in which month, from either side, and the defects.

    The headline counts (rented out now, customers holding devices, contracts ending in 90
    days, overdue) are not counted here a second time: they are ``customers()``'s totals,
    so the full-fleet group-bys behind them run once for the page. The grid is the whole
    fleet: every running contract, whichever organisation holds it, so its overdue row can
    be larger than the customers' overdue total (see the module docstring).

    Every key carries one type wherever it appears: ``returns_overdue`` and
    ``returns_after_window`` are device counts here as in the customers table and the data
    stream; the overdue devices by category are ``returns_overdue_by_category``.
    """
    today = today or date.today()
    R = returns(db, today, months=months)
    I = inbound(db, today, months=months)  # noqa: E741 - the inbound side, beside R for returns
    grid = [{"month": r["month"], "returns": r["devices"], "returns_by_category": r["by_category"],
             "inbound": i["units"], "inbound_by_category": i["by_category"], "landing": r["devices"] + i["units"]}
            for r, i in zip(R["months"], I["months"])]
    return {
        "as_of": today, "definition": DEFINITION, "basis": {**BASIS, "landing": LANDING},
        "returns_overdue": R["overdue"]["devices"], "returns_overdue_by_category": R["overdue"]["by_category"],
        "returns_after_window": R["after_window"]["devices"],
        "inbound_late": I["late"], "inbound_no_eta": I["no_eta"], "inbound_after_window": I["after_window"],
        "inbound_open_total": I["total"], "inbound_open_lines": I["lines"],
        "grid": grid,
        "defects": defects(db, today, months=months),
    }


# ---------------------------------------------------------------------------
# the data stream, for a downstream consumer


def stream(db: Session, today: Optional[date] = None, *, months: int = MONTHS) -> dict:
    """The ``devices_by_customer`` stream: the customers table plus the month grid, as plain JSON types."""
    today = today or date.today()
    C = customers(db, today, months=months)
    R = returns(db, today, months=months)
    I = inbound(db, today, months=months)  # noqa: E741
    D = defects(db, today, months=months)
    iso = lambda d: d.isoformat() if d else None  # noqa: E731
    return {
        "stream": "devices_by_customer",
        "source_system": "SCM-Master",
        "owner_role": "Fleet Operations",
        "stage": 1,
        "as_of": today.isoformat(),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "definition": DEFINITION,
        "totals": {k: C["totals"][k] for k in ("customers", "devices_at_customer", "contracts_ending_90d", "returns_overdue")},
        "rows": [{**{k: r[k] for k in ("customer_code", "customer")}, "since": iso(r["since"]),
                  **{k: r[k] for k in ("devices_at_customer", "contracts_active", "contracts_ending_90d", "returns_overdue",
                                       "by_category", "returns_by_month")}}
                 for r in C["rows"]],
        "rented_total": C["totals"]["rented_total"],
        "customers_holding_devices": C["totals"]["customers_holding_devices"],
        "returns_schedule": R["months"],
        "returns_overdue_total": R["overdue"]["devices"],
        "returns_after_window": R["after_window"]["devices"],
        "inbound_open_pos": [{"month": m["month"], "units": m["units"], "by_category": m["by_category"]} for m in I["months"]],
        "inbound_open_pos_late": I["late"],
        "inbound_open_pos_no_eta": I["no_eta"],
        "inbound_open_pos_after_window": I["after_window"],
        "inbound_open_total": I["total"],
        "defects": D["months"],
        # every device in REPAIR, not a defect count; with the two below it reconciles the months' in_repair
        "in_repair_now": D["in_repair_now"],
        "in_repair_before_window": D["in_repair_before_window"],
        "in_repair_undated": D["in_repair_undated"],
        "basis": BASIS,
        "omissions": D["omissions"],
    }

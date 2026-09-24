"""The warehouse in the DaaS scenario, one compartment at a time.

The business owner's instruction: returns, first-life stock, second-life stock and the
rest of the warehouse are tracked separately, never mixed, and for each of them the
console says how much room there is, how fast it rotates, and how well it is doing.

A compartment is a category of stock the owner names, not a shelf. It is defined by
the asset statuses it holds, and the seed gives every compartment exactly one station
location of the same code. Capacity is read from that location; on hand is counted by
status, because a device is in "second-life stock" by what it is, wherever it sits.

Three measures per compartment, all from ``asset`` alone:

  how full   on hand against the station's capacity;
  how fast   dwell, from ``status_since``: median, 90th percentile, share past the
             compartment's target dwell, the oldest unit;
  how well   a verdict from those two against the target dwell.

Throughput is DERIVED, not measured. Little's law says a stock of L units with a mean
residence time of W days is fed and drained by L / W units a day. The W we can read is
the age of the stock still here, which is shorter than a finished stay, so the derived
flow is an upper bound and every row says so. A measured flow needs the movement log.

Target dwell per compartment is a placeholder with the role that owns it, exactly like
a seeded KPI target: the number is a design parameter until that person sets it.

**Everything aggregates in the database**, the ``fleet.py`` pattern: the read is one
grouped query over (status, status_since), which is a few hundred rows however large
the fleet, plus one lookup of the station capacities. The scenario check is an
existence probe, not a count. Against 431,200 serials the whole read answers in about
a quarter of a second.

**No fake zeros.** A measure the data cannot support is ``None`` with a ``reason``.

**Opening one compartment** (``contents``, 24.09.2026). The owner, looking at the bar
chart: "we need to be able to click on each individually and know what is inside, which
items and how many." A bar that says 83 % committed does not say what is in there. The
read answers the questions a person opening a compartment asks, in this order: which
devices and how many (by model, rolled up by device class), how old against the target
dwell (within it, past it, far past it), what condition (the grade mix, first life
against second life, battery where read), a handful of the oldest serials so the
abstraction can be checked against real devices, and, for a station that receives
deliveries, what is on its way in: the open order lines destined here, by model, with
their ETA and which are late. That last part is what the "inbound reserved" band on
the cockpit's bar is made of. Every breakdown is a grouped query; no compartment's rows
are loaded into Python, and the read sits behind its own endpoint so the overview stays
as fast as it is.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.models.catalog import Product
from app.models.flow import WAREHOUSE_STATUSES, Asset, AssetStatus, Location, ReceiptItem
from app.models.procurement import OrderItem, PurchaseOrder
from app.services import fleet, planning
from app.services.exceptions import NotFoundError

# ---------------------------------------------------------------------------
# the compartments, in the order of the chain


@dataclass(frozen=True)
class Compartment:
    code: str                          # station location code; the seed creates one location per compartment
    name: str
    statuses: tuple[AssetStatus, ...]  # what counts as being in this compartment; the first is the primary status
    holds: str                         # one sentence: what is in here
    stage: str                         # its place in the chain
    target_dwell_days: int             # placeholder until the owner sets it, like a seeded KPI target
    target_owner: str


COMPARTMENTS: tuple[Compartment, ...] = (
    Compartment("ST-NEW", "New stock", (AssetStatus.IN_STORAGE, AssetStatus.RECEIVED),
                "New devices, bought and not yet rented: the first life starts here.",
                "first life", 30, "Head of Supply"),
    Compartment("ST-RETURNS", "Returns intake", (AssetStatus.RETURNED,),
                "Devices back from a customer: received and locked, not yet released or graded.",
                "return chain", 10, "Head of Operations"),
    Compartment("ST-MDM", "MDM release hold", (AssetStatus.MDM_RELEASE,),
                "Returned devices waiting for the old customer to release them from its device management.",
                "return chain", fleet.MDM_RELEASE_SLA_DAYS, "Head of Customer Success"),
    Compartment("ST-WIPE", "Wipe and grading", (AssetStatus.WIPE_GRADING,),
                "Certified wipe, function test and a grade from A to D: the decision point of the chain.",
                "return chain", 3, "Head of Operations"),
    Compartment("ST-REPAIR", "Repair", (AssetStatus.REPAIR,),
                "Grade C and defect devices at the repair partner.",
                "return chain", 20, "Head of Service Operations"),
    Compartment("ST-REFURB", "Refurbishment", (AssetStatus.REFURB,),
                "Devices being prepared for a second rental: the process, not the stock.",
                "return chain", 12, "Head of Recommerce"),
    Compartment("ST-SECOND", "Second-life stock", (AssetStatus.READY_SECOND,),
                "Refurbished grade A and B devices waiting for their second customer. Kept apart from new stock on purpose.",
                "second life", 45, "Head of Recommerce"),
    Compartment("ST-SELL", "Sellable stock", (AssetStatus.SELLABLE,),
                "Graded and cleared for resale, waiting for a channel.",
                "exit", 60, "Head of Recommerce"),
    Compartment("ST-SWAP", "Swap buffer", (AssetStatus.SWAP_BUFFER,),
                "Replacement devices held ready for a customer defect. A reserve, not a queue.",
                "reserve", 120, "Head of Service Operations"),
)
COMPARTMENT_BY_CODE = {c.code: c for c in COMPARTMENTS}
STATION_OF_STATUS = {st: c for c in COMPARTMENTS for st in c.statuses}

# verdict rules [placeholder, Head of Operations]
SLOW_MOVING_SHARE = 0.25       # slow moving: more than this share of the stock is past the target dwell
STALLED_MEDIAN_FACTOR = 1.5    # stalled: the median dwell is past this multiple of the target, half the stock is well overdue
OFFENDER_LIMIT = 25
CONTENTS_OLDEST_LIMIT = 10     # the oldest serials shown when a compartment is opened: a handful, not a list
FAR_PAST_FACTOR = 2            # far past the target: more than twice the target dwell [placeholder, Head of Operations]

# The age bands, relative to the compartment's target dwell T: within (up to T), past
# (over T up to 2T), far past (over 2T). "83 % full" and "half of it has been here twice
# as long as it should" are different problems, and the bands keep them apart.
AGE_BANDS = (("within_target", "Within target"), ("past_target", "Past target"), ("far_past_target", "Far past target"))
AGE_BASIS = ("days in the compartment from status_since against the target dwell; shares are of the dated units, "
             "units without a dwell date are counted separately and never guessed")
CYCLE_LABEL = {"0": "New, never rented", "1": "After a first rental", "2+": "After a second rental or later"}
GRADE_BASIS = "grade A to D as recorded at wipe and grading; a unit that has not reached that step carries none"
INBOUND_BASIS = ("open order lines (pending, approved, placed, partially received) whose order is destined for this "
                 "compartment's station; outstanding = ordered minus received; late = the estimated delivery date is "
                 "before today; committed = on hand plus outstanding inbound, against the station's capacity")
# The same order statuses the inbound pipeline and the over-order guard read, so the band
# a person sees here and the guard that refuses an order cannot disagree.
OPEN_ORDER_STATUSES = planning.OPEN_ORDER_STATUSES

THROUGHPUT_BASIS = ("derived, upper bound: on hand over the mean age of the stock still here (Little's law), "
                    "not measured from movements; a measured flow needs the movement log")


def _as_date(v) -> date:
    """SQLite hands a date back as text on some paths; Postgres gives a date."""
    return v if isinstance(v, date) else date.fromisoformat(str(v)[:10])


def _quantile_from_histogram(hist: dict[int, int], q: float) -> Optional[float]:
    """The day count below which a share ``q`` of the units sits, exact, from {days: how many}.

    The same reading as ``fleet._median_from_histogram`` at q = 0.5: the first day at
    which the running count reaches the share.
    """
    total = sum(hist.values())
    if not total:
        return None
    want = total * q
    acc = 0
    last = 0
    for days in sorted(hist):
        acc += hist[days]
        last = days
        if acc >= want:
            return float(days)
    return float(last)


def _is_daas(db: Session) -> bool:
    """A database with a rented device is a DaaS fleet. An existence probe: a count of
    300,000 rented devices costs a quarter of a second, this costs nothing."""
    return db.scalar(select(Asset.id).where(Asset.status == AssetStatus.RENTED).limit(1)) is not None


def _stock(db: Session, today: date) -> tuple[dict[AssetStatus, dict[int, int]], Counter, Counter]:
    """One grouped read over everything in the warehouse.

    Grouping by ``status_since`` itself keeps the result at a few hundred rows however
    large the fleet. Units without a dwell date are counted but kept out of the
    histogram, so on hand is always complete and dwell is never guessed.

    Returns (histogram per status as {days: count}, on hand per status, undated per status).
    """
    rows = db.execute(
        select(Asset.status, Asset.status_since, func.count(Asset.id))
        .where(Asset.status.in_(tuple(WAREHOUSE_STATUSES)))
        .group_by(Asset.status, Asset.status_since)
    ).all()
    hist: dict[AssetStatus, dict[int, int]] = defaultdict(dict)
    on_hand: Counter = Counter()
    undated: Counter = Counter()
    for status, since, n in rows:
        n = int(n)
        on_hand[status] += n
        if since is None:
            undated[status] += n
            continue
        days = max(0, (today - _as_date(since)).days)
        hist[status][days] = hist[status].get(days, 0) + n
    return hist, on_hand, undated


def _measure(c: Compartment, step: int, on_hand: int, undated: int, hist: dict[int, int],
             capacity: Optional[int], has_station: bool) -> dict:
    """Every figure of one compartment from its histogram, or the reason there is none."""
    target = c.target_dwell_days
    row = {
        "code": c.code, "name": c.name, "holds": c.holds, "stage": c.stage, "step": step,
        "statuses": [s.value for s in c.statuses],
        # how full
        "on_hand": on_hand, "capacity": capacity,
        "free": (max(0, capacity - on_hand) if capacity is not None else None),
        "overflow": (max(0, on_hand - capacity) if capacity is not None else 0),
        "utilisation": (round(on_hand / capacity, 4) if capacity else None),
        "over_capacity": bool(capacity is not None and on_hand > capacity),
        "capacity_reason": (None if capacity is not None
                            else (f"station {c.code} has no capacity set" if has_station
                                  else f"no station location with code {c.code}")),
        # the target: a placeholder with an owner, like a seeded KPI target
        "target_dwell_days": target, "target_placeholder": True, "target_owner": c.target_owner,
        # how fast
        "median_days": None, "p90_days": None, "mean_days": None, "oldest_days": None,
        "past_target_units": None, "past_target_share": None, "undated_units": undated, "dwell_reason": None,
        "units_per_week": None, "turns_per_year": None,
        "throughput_derived": True, "throughput_basis": THROUGHPUT_BASIS, "throughput_reason": None,
        # how well
        "verdict": None, "verdict_reason": None,
    }
    dated = sum(hist.values())
    if on_hand == 0:
        row.update(dwell_reason="no unit in this compartment", throughput_reason="no unit in this compartment",
                   verdict="empty", verdict_reason="nothing here")
        return row
    if dated == 0:
        reason = "no dwell recorded: status_since is empty for every unit here"
        row.update(dwell_reason=reason, throughput_reason=reason, verdict="unknown", verdict_reason=reason)
        return row

    median = _quantile_from_histogram(hist, 0.5)
    p90 = _quantile_from_histogram(hist, 0.9)
    mean = sum(d * n for d, n in hist.items()) / dated
    past = sum(n for d, n in hist.items() if d > target)
    share = past / dated
    row.update(median_days=median, p90_days=p90, mean_days=round(mean, 1), oldest_days=max(hist),
               past_target_units=past, past_target_share=round(share, 4))
    if mean > 0:
        # Little's law: flow = stock / residence time. Per week, and as turns of the stock per year.
        row.update(units_per_week=round(dated / mean * 7, 1), turns_per_year=round(365.0 / mean, 2))
    else:
        row["throughput_reason"] = "every unit arrived today: mean dwell is zero, no rate can be derived"

    if row["over_capacity"]:
        verdict, why = "over_capacity", f"{on_hand} units in a {capacity}-unit station"
    elif median > STALLED_MEDIAN_FACTOR * target:
        verdict, why = "stalled", f"median {median:.0f} d against a target of {target} d: half the stock is well past it"
    elif share > SLOW_MOVING_SHARE:
        verdict, why = "slow_moving", f"{share:.0%} of the stock is past the target of {target} d"
    else:
        verdict, why = "healthy", f"median {median:.0f} d, {share:.0%} past the target of {target} d"
    row.update(verdict=verdict, verdict_reason=why)
    return row


def compartments(db: Session, *, today: Optional[date] = None) -> dict:
    """The warehouse, per compartment and rolled up. See the module docstring."""
    today = today or date.today()
    if not _is_daas(db):
        return {"scenario": "datacenter", "as_of": today, "capacity": None, "on_hand": None, "free": None,
                "utilisation": None, "over_capacity": 0, "chain": [], "compartments": [],
                "reason": "compartments exist in the device-as-a-service scenario; this database holds the datacenter operation"}

    hist, on_hand, undated = _stock(db, today)
    caps = {code: cap for code, cap in db.execute(
        select(Location.code, Location.capacity).where(Location.code.in_([c.code for c in COMPARTMENTS]))).all()}

    rows = []
    for step, c in enumerate(COMPARTMENTS, start=1):
        h: dict[int, int] = {}
        for st in c.statuses:
            for days, n in hist.get(st, {}).items():
                h[days] = h.get(days, 0) + n
        rows.append(_measure(c, step, sum(on_hand[st] for st in c.statuses), sum(undated[st] for st in c.statuses), h,
                             caps.get(c.code), c.code in caps))

    with_cap = [r["capacity"] for r in rows if r["capacity"] is not None]
    total_cap = sum(with_cap) if with_cap else None
    total_on = sum(r["on_hand"] for r in rows)
    return {
        "scenario": "daas", "as_of": today,
        "capacity": total_cap, "on_hand": total_on,
        "free": (max(0, total_cap - total_on) if total_cap is not None else None),
        "utilisation": (round(total_on / total_cap, 4) if total_cap else None),
        "over_capacity": sum(1 for r in rows if r["over_capacity"]),
        "chain": [c.code for c in COMPARTMENTS],
        "compartments": rows,
        "reason": (None if total_cap is not None else "no station location carries a capacity"),
    }


def offenders(db: Session, code: str, *, today: Optional[date] = None, limit: int = OFFENDER_LIMIT) -> dict:
    """The worst of one compartment: the late stock by device, and the oldest units.

    Read on demand, not with the overview: the per-device breakdown of a 36,000-unit
    compartment is a grouped join the overview does not need, and the oldest units are
    an ordered read on the (status, status_since) index that costs nothing but is only
    wanted once someone asks which serials to move first.
    """
    today = today or date.today()
    c = COMPARTMENT_BY_CODE.get(code)
    if c is None:
        raise NotFoundError(f"No warehouse compartment with code {code!r}")
    statuses = tuple(c.statuses)
    cutoff = today - timedelta(days=c.target_dwell_days)
    by_product = db.execute(
        select(Product.name, Product.category, func.count(Asset.id))
        .join(Product, Product.id == Asset.product_id)
        .where(Asset.status.in_(statuses), Asset.status_since.is_not(None), Asset.status_since < cutoff)
        .group_by(Product.name, Product.category)
        .order_by(func.count(Asset.id).desc()).limit(10)
    ).all()
    return {
        "code": c.code, "name": c.name, "target_dwell_days": c.target_dwell_days, "as_of": today,
        "past_target_by_product": [{"name": name, "family": fam, "units": int(n)} for name, fam, n in by_product],
        "oldest": _oldest(db, statuses, today, limit),
    }


def _oldest(db: Session, statuses: tuple, today: date, limit: int) -> list[dict]:
    """The oldest dated units of a status set: an ordered read on the (status, status_since)
    index that stops after ``limit`` rows, so it costs the same for 36,000 units as for four.

    One read per status, merged here. A compartment that spans two statuses (new stock
    holds IN_STORAGE and RECEIVED) asked in one ``IN`` cannot be walked in index order:
    SQLite sorted 35,400 rows to find the ten oldest, 75 ms, where two index walks cost
    nothing.
    """
    limit = max(1, limit)
    rows = []
    for st in statuses:
        rows += db.execute(
            select(Asset.id, Asset.serial_number, Product.name, Product.category, Asset.grade, Asset.cycle_no,
                   Asset.status, Asset.status_since)
            .join(Product, Product.id == Asset.product_id)
            .where(Asset.status == st, Asset.status_since.is_not(None))
            .order_by(Asset.status_since).limit(limit)
        ).all()
    rows.sort(key=lambda r: (_as_date(r[7]), r[1]))
    return [{"asset_id": aid, "serial_number": sn, "product": name, "family": fam, "grade": grade,
             "cycle_no": int(cyc), "status": st.value if hasattr(st, "value") else str(st),
             "since": _as_date(since), "days": (today - _as_date(since)).days}
            for aid, sn, name, fam, grade, cyc, st, since in rows[:limit]]


def _share(n: int, total: int) -> Optional[float]:
    return round(n / total, 4) if total else None


def _cycle_key(cycle) -> str:
    n = int(cycle or 0)
    return "0" if n == 0 else "1" if n == 1 else "2+"


def _age(hist: dict[int, int], undated: int, target: int) -> tuple[Optional[dict], Optional[str]]:
    """The dwell of one compartment in bands against its target, from the {days: count} histogram."""
    dated = sum(hist.values())
    if dated == 0:
        return None, ("no unit in this compartment" if undated == 0
                      else "no dwell recorded: status_since is empty for every unit here")
    far_from = FAR_PAST_FACTOR * target
    counts = {
        "within_target": sum(n for d, n in hist.items() if d <= target),
        "past_target": sum(n for d, n in hist.items() if target < d <= far_from),
        "far_past_target": sum(n for d, n in hist.items() if d > far_from),
    }
    edges = {"within_target": (0, target), "past_target": (target + 1, far_from), "far_past_target": (far_from + 1, None)}
    bands = [{"key": key, "label": label, "from_days": edges[key][0], "to_days": edges[key][1],
              "units": counts[key], "share": _share(counts[key], dated)} for key, label in AGE_BANDS]
    past = counts["past_target"] + counts["far_past_target"]
    return {
        "target_dwell_days": target, "far_past_from_days": far_from + 1, "basis": AGE_BASIS,
        "dated_units": dated, "undated_units": undated, "bands": bands,
        "median_days": _quantile_from_histogram(hist, 0.5), "p90_days": _quantile_from_histogram(hist, 0.9),
        "mean_days": round(sum(d * n for d, n in hist.items()) / dated, 1), "oldest_days": max(hist),
        "past_target_units": past, "past_target_share": _share(past, dated),
        "far_past_units": counts["far_past_target"], "far_past_share": _share(counts["far_past_target"], dated),
    }, None


def _inbound(db: Session, station_id: Optional[str], code: str, today: date, on_hand: int, capacity: Optional[int]) -> dict:
    """What is on its way into one station: the open order lines destined there.

    Open lines are few by nature (orders, not serials), so the lines come back as
    rows and are rolled up by model here; the received quantity per line is a
    correlated sum on the receipt_item index, one per open line, not a scan of every
    receipt ever written.
    """
    view = {
        "station": (code if station_id else None), "basis": INBOUND_BASIS,
        "units": None, "lines": [], "by_model": [], "late_units": None, "late_lines": None,
        "next_eta": None, "last_eta": None, "committed": None, "committed_share": None, "inbound_share": None,
        "reason": None,
    }
    if station_id is None:
        view["reason"] = f"no station location with code {code}: no order can be destined here"
        return view
    received = (select(func.coalesce(func.sum(ReceiptItem.quantity_received), 0))
                .where(ReceiptItem.order_item_id == OrderItem.id).scalar_subquery())
    rows = db.execute(
        select(PurchaseOrder.order_number, PurchaseOrder.status, Product.id, Product.name, Product.category,
               OrderItem.id, OrderItem.quantity, received, OrderItem.estimated_delivery_date)
        .join(PurchaseOrder, PurchaseOrder.id == OrderItem.order_id)
        .join(Product, Product.id == OrderItem.product_id)
        .where(PurchaseOrder.status.in_(OPEN_ORDER_STATUSES), PurchaseOrder.destination_id == station_id)
    ).all()
    lines = []
    for po, st, pid, name, fam, oid, qty, got, eta in rows:
        outstanding = int(qty) - int(got or 0)
        if outstanding <= 0:
            continue
        eta = _as_date(eta) if eta is not None else None
        late = (eta < today) if eta is not None else None
        lines.append({
            "order_number": po, "order_status": st.value if hasattr(st, "value") else str(st), "order_item_id": oid,
            "product_id": pid, "product": name, "family": fam,
            "ordered": int(qty), "received": int(got or 0), "outstanding": outstanding,
            "eta": eta, "days_to_eta": ((eta - today).days if eta is not None else None),
            "late": late, "days_late": ((today - eta).days if late else None),
            "eta_reason": (None if eta is not None else "no estimated delivery date on the line"),
        })
    lines.sort(key=lambda r: (r["eta"] or date.max, r["order_number"]))
    units = sum(r["outstanding"] for r in lines)
    by_model: dict[str, dict] = {}
    for r in lines:
        m = by_model.setdefault(r["product_id"], {"product_id": r["product_id"], "name": r["product"], "family": r["family"],
                                                  "units": 0, "share": None, "lines": 0, "late_units": 0, "next_eta": None})
        m["units"] += r["outstanding"]
        m["lines"] += 1
        if r["late"]:
            m["late_units"] += r["outstanding"]
        if r["eta"] is not None and (m["next_eta"] is None or r["eta"] < m["next_eta"]):
            m["next_eta"] = r["eta"]
    models = sorted(by_model.values(), key=lambda m: (-m["units"], m["name"]))
    for m in models:
        m["share"] = _share(m["units"], units)
    etas = [r["eta"] for r in lines if r["eta"] is not None]
    view.update(
        units=units, lines=lines, by_model=models,
        late_units=sum(r["outstanding"] for r in lines if r["late"]), late_lines=sum(1 for r in lines if r["late"]),
        next_eta=(min(etas) if etas else None), last_eta=(max(etas) if etas else None),
        committed=on_hand + units,
        committed_share=(round((on_hand + units) / capacity, 4) if capacity else None),
        inbound_share=(round(units / capacity, 4) if capacity else None),
        reason=(None if lines else f"no open order line is destined for station {code}"),
    )
    return view


def contents(db: Session, code: str, *, today: Optional[date] = None, oldest_limit: int = CONTENTS_OLDEST_LIMIT) -> dict:
    """What is inside one compartment. See the module docstring.

    Four grouped reads and one ordered one, none of which loads a unit into Python:

      the station's capacity, one row;
      the dwell histogram by status_since, index-only, a few hundred rows however large
        the compartment: the age bands, median, p90, mean, oldest;
      one aggregate over (model, cycle, grade): count, oldest dwell, units past and far
        past the target, battery health where read. About a hundred rows for the largest
        compartment; every roll-up (by model, by class, by grade, by cycle) is summed
        from it here;
      the open order lines destined for the station, rolled up by model;
      the oldest serials, an ordered read on the (status, status_since) index with a limit.

    Every share is of the compartment's on hand unless the block says otherwise (age
    bands are shares of the dated units, inbound shares are of the outstanding units).
    """
    today = today or date.today()
    c = COMPARTMENT_BY_CODE.get(code)
    if c is None:
        raise NotFoundError(f"No warehouse compartment with code {code!r}")
    statuses = tuple(c.statuses)
    target = c.target_dwell_days
    cutoff_past = today - timedelta(days=target)                       # status_since before this: more than T days here
    cutoff_far = today - timedelta(days=FAR_PAST_FACTOR * target)      # before this: more than 2T days here

    station = db.execute(select(Location.id, Location.capacity).where(Location.code == c.code)).first()
    station_id, capacity = (station[0], station[1]) if station else (None, None)

    # how old: the histogram, index-only
    hist: dict[int, int] = {}
    undated = 0
    for since, n in db.execute(select(Asset.status_since, func.count(Asset.id))
                               .where(Asset.status.in_(statuses)).group_by(Asset.status_since)).all():
        if since is None:
            undated += int(n)
            continue
        days = max(0, (today - _as_date(since)).days)
        hist[days] = hist.get(days, 0) + int(n)
    on_hand = sum(hist.values()) + undated

    # which devices, what condition: one aggregate over (model, cycle, grade). The names come
    # from the catalogue in a read of its own: joining product into the aggregate cost up to
    # 74 ms on the full fleet for a table of thirteen rows.
    catalogue = {pid: (name, fam) for pid, name, fam in db.execute(select(Product.id, Product.name, Product.category)).all()}
    past_expr = func.sum(case((Asset.status_since < cutoff_past, 1), else_=0))
    far_expr = func.sum(case((Asset.status_since < cutoff_far, 1), else_=0))
    groups = db.execute(
        select(Asset.product_id, Asset.cycle_no, Asset.grade,
               func.count(Asset.id), func.count(Asset.status_since), func.min(Asset.status_since), past_expr, far_expr,
               func.sum(Asset.battery_health), func.count(Asset.battery_health))
        .where(Asset.status.in_(statuses))
        .group_by(Asset.product_id, Asset.cycle_no, Asset.grade)
    ).all()

    by_model: dict[str, dict] = {}
    by_class: dict[str, dict] = {}
    grades: Counter = Counter()
    cycles: Counter = Counter()
    battery_sum, battery_n = 0.0, 0
    for pid, cyc, grade, n, dated_n, oldest_since, past, far, bat_sum, bat_n in groups:
        name, fam = catalogue.get(pid, (pid, None))
        n, dated_n, past, far, bat_n = int(n), int(dated_n or 0), int(past or 0), int(far or 0), int(bat_n or 0)
        m = by_model.setdefault(pid, {"product_id": pid, "name": name, "family": fam, "units": 0, "share": None,
                                      "past_target_units": 0, "far_past_units": 0, "oldest_days": None, "undated_units": 0})
        m["units"] += n
        m["past_target_units"] += past
        m["far_past_units"] += far
        m["undated_units"] += n - dated_n
        if oldest_since is not None:
            days = (today - _as_date(oldest_since)).days
            m["oldest_days"] = days if m["oldest_days"] is None else max(m["oldest_days"], days)
        key = fam or "unclassified"
        k = by_class.setdefault(key, {"key": key, "label": (fam or "No device class"), "units": 0, "share": None, "models": set()})
        k["units"] += n
        k["models"].add(pid)
        grades[grade] += n
        cycles[_cycle_key(cyc)] += n
        if bat_n:
            battery_sum += float(bat_sum)
            battery_n += bat_n
    models = sorted(by_model.values(), key=lambda m: (-m["units"], m["name"]))
    for m in models:
        m["share"] = _share(m["units"], on_hand)
    classes = sorted(by_class.values(), key=lambda k: (-k["units"], k["label"]))
    for k in classes:
        k["share"] = _share(k["units"], on_hand)
        k["models"] = len(k["models"])

    age, age_reason = _age(hist, undated, target)

    condition, condition_reason = None, None
    if on_hand == 0:
        condition_reason = "no unit in this compartment"
    else:
        graded = sum(n for g, n in grades.items() if g is not None)
        ungraded = grades.get(None, 0)
        grade_rows = ([{"grade": g, "label": f"Grade {g}", "units": grades[g], "share": _share(grades[g], on_hand)}
                       for g in sorted(g for g in grades if g is not None)] if graded else None)
        condition = {
            "grades": grade_rows, "graded_units": graded, "ungraded_units": ungraded, "ungraded_share": _share(ungraded, on_hand),
            "grade_basis": GRADE_BASIS,
            "grade_reason": (None if graded else "no unit in this compartment carries a grade"),
            "cycles": [{"cycle": key, "label": CYCLE_LABEL[key], "units": cycles[key], "share": _share(cycles[key], on_hand)}
                       for key in ("0", "1", "2+") if cycles.get(key)],
            "battery_health_mean": (round(battery_sum / battery_n, 3) if battery_n else None),
            "battery_health_units": battery_n,
            "battery_reason": (None if battery_n else "no battery health read for any unit here"),
        }

    return {
        "code": c.code, "name": c.name, "holds": c.holds, "stage": c.stage, "step": COMPARTMENTS.index(c) + 1,
        "statuses": [s.value for s in statuses], "as_of": today, "unit": "devices",
        "target_dwell_days": target, "target_placeholder": True, "target_owner": c.target_owner,
        # how full, the same figures as the overview row, so a second screen needs no other call
        "on_hand": on_hand, "undated_units": undated, "capacity": capacity,
        "free": (max(0, capacity - on_hand) if capacity is not None else None),
        "overflow": (max(0, on_hand - capacity) if capacity is not None else 0),
        "utilisation": (round(on_hand / capacity, 4) if capacity else None),
        "over_capacity": bool(capacity is not None and on_hand > capacity),
        "capacity_reason": (None if capacity is not None
                            else (f"station {c.code} has no capacity set" if station else f"no station location with code {c.code}")),
        # which devices
        "by_class": classes, "by_model": models,
        # how old
        "age": age, "age_reason": age_reason,
        # what condition
        "condition": condition, "condition_reason": condition_reason,
        # the real devices behind the abstraction
        "oldest": _oldest(db, statuses, today, oldest_limit),
        # what is on its way in
        "inbound": _inbound(db, station_id, c.code, today, on_hand, capacity),
    }

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
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.catalog import Product
from app.models.flow import WAREHOUSE_STATUSES, Asset, AssetStatus, Location
from app.services import fleet
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
    oldest = db.execute(
        select(Asset.id, Asset.serial_number, Product.name, Product.category, Asset.grade, Asset.cycle_no,
               Asset.status, Asset.status_since)
        .join(Product, Product.id == Asset.product_id)
        .where(Asset.status.in_(statuses), Asset.status_since.is_not(None))
        .order_by(Asset.status_since).limit(max(1, limit))
    ).all()
    return {
        "code": c.code, "name": c.name, "target_dwell_days": c.target_dwell_days, "as_of": today,
        "past_target_by_product": [{"name": name, "family": fam, "units": int(n)} for name, fam, n in by_product],
        "oldest": [{"asset_id": aid, "serial_number": sn, "product": name, "family": fam, "grade": grade,
                    "cycle_no": int(cyc), "status": st.value if hasattr(st, "value") else str(st),
                    "since": _as_date(since), "days": (today - _as_date(since)).days}
                   for aid, sn, name, fam, grade, cyc, st, since in oldest],
    }

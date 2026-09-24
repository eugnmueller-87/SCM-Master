"""The movement log of the warehouse: which compartment a device left, which it entered,
when, and how long it had been there.

The business owner's second instruction of 24.09.2026: "from which storage compartment or
part of the warehouse is the piece being moved, so we understand the movement and the
turnaround." Three reads in this system said they could not answer it: ``warehouse.py``
derives its throughput from stock and dwell, ``capacity_plan.py`` inverts a dwell that is
the age of the stock still present, and ``tco_device.py`` cannot cost the warehouse days
of a finished life. All three say a measured figure needs the movement log.

The log existed in substance. ``asset_service.transition`` has written an ``AssetEvent``
with ``from_status -> to_status`` since the lifecycle log was added, and a compartment is
defined by its statuses, so every transition already said which compartment a device
left and entered. Two things were missing, and both are on the event row now (migration
f7a9b1c3d5e4): the day of the move on the fleet's own calendar (``effective_date``) and
the stay it ended (``from_since``, ``dwell_days``), written by the asset service at the
moment of the move. A third gap is not in the schema but in the data: the seed streams
the fleet in bulk and writes no events, so the 431,200 seeded serials carry no history,
and every figure here is measured from the moves made since, by the simulation tab, the
console's transition button or an integration. The reads say so rather than pad it.

**Measured, not derived.** A flow here is a count of moves in a window over the days of
the window. A dwell here is the stay of a device that actually left, from the two stamps
on the row that moved it. Both are what the other modules derive by Little's law from the
stock still present, and where a compartment shows both, they stand side by side.

**Everything aggregates in the database.** The window read is one grouped query over
(from status, to status, dwell days) on an index that carries all three, plus a distinct
count of the devices that moved; a device's path is the handful of rows behind one serial.

**No fake zeros.** A window with no dated move says so; a stay whose start the seed did
not record is counted as a move of unknown length, never as zero days.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.catalog import Product
from app.models.flow import WAREHOUSE_STATUSES, Asset, AssetEvent, AssetEventType, AssetStatus, Location
from app.services import warehouse
from app.services.exceptions import NotFoundError

DEFAULT_DAYS = 30
MAX_DAYS = 365

# Where a device is when it is not in a compartment, as the operator names it.
OUTSIDE_LABEL = {
    AssetStatus.RENTED: "At customers", AssetStatus.SOLD: "Sold", AssetStatus.RECYCLED: "Recycled",
    AssetStatus.DEPLOYED: "Deployed", AssetStatus.MAINTENANCE: "Maintenance",
    AssetStatus.DECOMMISSIONED: "Decommissioned", AssetStatus.DISPOSED: "Disposed",
}
BORN_LABEL = "On order"     # a receipt: the device had no compartment before, it was on an order line

FLOW_BASIS = "measured: moves out of the compartment in the window, over the days the log covers inside it, times seven"
DWELL_BASIS = ("measured: the stay of every device that left the compartment in the window, from the day it entered "
               "(the dwell start it carried) to the day it left; a device whose entry the seed did not record is a "
               "move of unknown length and is left out of the days")
DERIVED_BASIS = warehouse.THROUGHPUT_BASIS
HISTORY_NOTE = ("the seeded fleet carries no movement history: the seed writes devices in bulk without events. Every "
                "move made since, on the Simulation tab, through the console's transition button or by an integration, "
                "is logged with its day and the stay it ended, and that is what is measured here")


def _as_date(v) -> Optional[date]:
    if v is None:
        return None
    return v if isinstance(v, date) else date.fromisoformat(str(v)[:10])


def _status(v) -> Optional[AssetStatus]:
    if v is None:
        return None
    return v if isinstance(v, AssetStatus) else AssetStatus(str(v))


def label(status: Optional[AssetStatus]) -> str:
    """A status as a place: the compartment it belongs to, or where the device is instead."""
    if status is None:
        return BORN_LABEL
    comp = warehouse.STATION_OF_STATUS.get(status)
    if comp is not None:
        return comp.name
    return OUTSIDE_LABEL.get(status, status.value)


def code(status: Optional[AssetStatus]) -> Optional[str]:
    """The compartment code of a status, or None outside the warehouse."""
    comp = warehouse.STATION_OF_STATUS.get(status) if status is not None else None
    return comp.code if comp else None


def _quantiles(hist: dict[int, int]) -> dict:
    """Median, p90, mean and the longest stay from {days: how many}; None when nothing is dated."""
    dated = sum(hist.values())
    if not dated:
        return {"median_days": None, "p90_days": None, "mean_days": None, "max_days": None}
    return {
        "median_days": warehouse._quantile_from_histogram(hist, 0.5),
        "p90_days": warehouse._quantile_from_histogram(hist, 0.9),
        "mean_days": round(sum(d * n for d, n in hist.items()) / dated, 1),
        "max_days": max(hist),
    }


def window(db: Session, *, days: int = DEFAULT_DAYS, today: Optional[date] = None) -> dict:
    """The moves of the last ``days`` days, by pair of compartments and rolled up per compartment.

    One grouped read on the window index: (from status, to status, days in the from
    compartment) with a count, a few hundred rows however many moves the log holds. A
    pure location move (no status change) is not a compartment movement and is left out;
    a receipt is the move from an order line into new stock.
    """
    today = today or date.today()
    days = max(1, min(int(days), MAX_DAYS))
    since = today - timedelta(days=days)
    in_window = (AssetEvent.effective_date > since, AssetEvent.effective_date <= today)

    rows = db.execute(
        select(AssetEvent.from_status, AssetEvent.to_status, AssetEvent.dwell_days, func.count())
        .where(*in_window, AssetEvent.to_status.is_not(None))
        .group_by(AssetEvent.from_status, AssetEvent.to_status, AssetEvent.dwell_days)
    ).all()
    devices = int(db.scalar(select(func.count(func.distinct(AssetEvent.asset_id)))
                            .where(*in_window, AssetEvent.to_status.is_not(None))) or 0)
    undated = int(db.scalar(select(func.count()).select_from(AssetEvent)
                            .where(AssetEvent.effective_date.is_(None), AssetEvent.to_status.is_not(None))) or 0)
    first, last = db.execute(select(func.min(AssetEvent.effective_date), func.max(AssetEvent.effective_date))
                             .where(AssetEvent.to_status.is_not(None))).one()
    first, last = _as_date(first), _as_date(last)
    # A flow per week is moves over days, and the days are the ones the log covers: from the first
    # logged move inside the window to today. A seeded fleet has no history, so a 30-day window that
    # holds two days of moves would otherwise read as a fifteenth of the real flow.
    first_in = _as_date(db.scalar(select(func.min(AssetEvent.effective_date)).where(*in_window, AssetEvent.to_status.is_not(None))))
    covered = ((today - first_in).days + 1) if first_in else 0

    def per_week(n: int) -> float:
        return round(n / covered * 7, 1) if covered else 0.0

    pairs: dict[tuple, dict] = {}
    for frm, to, dwell, n in rows:
        frm, to, n = _status(frm), _status(to), int(n)
        p = pairs.setdefault((frm, to), {"hist": {}, "units": 0, "unknown_units": 0})
        p["units"] += n
        if dwell is None:
            p["unknown_units"] += n
        else:
            p["hist"][int(dwell)] = p["hist"].get(int(dwell), 0) + n

    order = {c.code: i for i, c in enumerate(warehouse.COMPARTMENTS, start=1)}

    def _rank(st: Optional[AssetStatus]) -> int:
        c = code(st)
        return order.get(c, 0) if c else (0 if st is None else 99)

    pair_rows = []
    for (frm, to), p in sorted(pairs.items(), key=lambda kv: (_rank(kv[0][0]), _rank(kv[0][1]), -kv[1]["units"])):
        dated = sum(p["hist"].values())
        pair_rows.append({
            "from_status": (frm.value if frm else None), "to_status": to.value,
            "from_code": code(frm), "to_code": code(to), "from_name": label(frm), "to_name": label(to),
            "units": p["units"], "dated_units": dated, "unknown_units": p["unknown_units"],
            "per_week": per_week(p["units"]),
            **_quantiles(p["hist"]),
            "dwell_reason": (None if dated else ("a receipt ends no stay: the device was on an order line" if frm is None
                                                 else "no stay measured: the devices that moved carried no dwell start")),
        })

    # per compartment: what came in, what went out, the measured flow and the measured finished stay,
    # next to what the Warehouse tab derives from the stock still present
    derived = {c["code"]: c for c in warehouse.compartments(db, today=today)["compartments"]}
    comp_rows = []
    for step, c in enumerate(warehouse.COMPARTMENTS, start=1):
        sts = set(c.statuses)
        into = sum(p["units"] for (f, t), p in pairs.items() if t in sts and f not in sts)
        out = sum(p["units"] for (f, t), p in pairs.items() if f in sts and t not in sts)
        hist: dict[int, int] = defaultdict(int)
        unknown = 0
        for (f, t), p in pairs.items():
            if f in sts and t not in sts:
                unknown += p["unknown_units"]
                for d, n in p["hist"].items():
                    hist[d] += n
        q = _quantiles(dict(hist))
        d = derived.get(c.code, {})
        comp_rows.append({
            "code": c.code, "name": c.name, "step": step, "stage": c.stage,
            "units_in": into, "units_out": out, "in_per_week": per_week(into), "out_per_week": per_week(out),
            "flow_basis": FLOW_BASIS,
            "dated_out": sum(hist.values()), "unknown_out": unknown, **q, "dwell_basis": DWELL_BASIS,
            "dwell_reason": (None if hist else ("nothing left this compartment in the window" if not out
                                                else "the devices that left carried no dwell start")),
            "target_dwell_days": c.target_dwell_days,
            "on_hand": d.get("on_hand"),
            "derived_units_per_week": d.get("units_per_week"), "derived_mean_days": d.get("mean_days"),
            "derived_basis": DERIVED_BASIS,
        })

    moves = sum(p["units"] for p in pairs.values())
    return {
        "as_of": today, "days": days, "since": since, "covered_days": covered,
        "coverage_basis": ("a flow per week is the moves over the days the log covers inside the window, from the first logged move to "
                           "today, times seven; a window the log does not fill is not padded with empty days"),
        "moves": moves, "devices": devices, "pairs_count": len(pair_rows),
        "undated_events": undated,
        "undated_reason": (None if not undated else f"{undated:,} logged moves carry no day: written before the movement log had one"),
        "first_move": first, "last_move": last,
        "history_note": HISTORY_NOTE,
        "reason": (None if moves else (f"no move logged in the last {days} days" if last else "no move logged yet")),
        "pairs": pair_rows,
        "compartments": comp_rows,
    }


def path(db: Session, serial: str, *, today: Optional[date] = None) -> dict:
    """One device's way through the chain: every logged move with its day and the stay it ended,
    then the compartment it is in now and how long it has been there so far."""
    today = today or date.today()
    row = db.execute(
        select(Asset, Product.name, Product.category, Location.code)
        .join(Product, Product.id == Asset.product_id)
        .outerjoin(Location, Location.id == Asset.current_location_id)
        .where(Asset.serial_number == serial)
    ).first()
    if row is None:
        raise NotFoundError(f"No device with serial {serial!r}")
    asset, product, family, station = row
    # Append-only, so the write order is the order of the moves; the day is on the row for the reading.
    events = db.execute(select(AssetEvent).where(AssetEvent.asset_id == asset.id)
                        .order_by(AssetEvent.date_created, AssetEvent.id)).scalars().all()
    steps = []
    warehouse_days = 0
    customer_days = 0
    unknown = 0
    for e in events:
        if e.event_type == AssetEventType.MOVED:
            steps.append({"kind": "moved", "from_status": None, "to_status": None, "from_name": None, "to_name": None,
                          "from_code": None, "to_code": None, "effective_date": _as_date(e.effective_date),
                          "from_since": None, "dwell_days": None, "actor": e.actor, "note": e.note, "logged_at": e.date_created})
            continue
        frm, to = _status(e.from_status), _status(e.to_status)
        if e.dwell_days is None and frm is not None:
            unknown += 1
        elif e.dwell_days is not None and frm in WAREHOUSE_STATUSES:
            warehouse_days += int(e.dwell_days)
        elif e.dwell_days is not None and frm == AssetStatus.RENTED:
            customer_days += int(e.dwell_days)
        steps.append({
            "kind": ("received" if e.event_type == AssetEventType.RECEIVED else "moved_status"),
            "from_status": (frm.value if frm else None), "to_status": (to.value if to else None),
            "from_name": (label(frm) if (frm or e.event_type == AssetEventType.RECEIVED) else None),
            "to_name": (label(to) if to else None),
            "from_code": code(frm), "to_code": code(to),
            "effective_date": _as_date(e.effective_date), "from_since": _as_date(e.from_since), "dwell_days": e.dwell_days,
            "actor": e.actor, "note": e.note, "logged_at": e.date_created,
        })
    since = _as_date(asset.status_since)
    return {
        "serial_number": asset.serial_number, "asset_id": asset.id, "product": product, "family": family,
        "cycle_no": int(asset.cycle_no or 0), "grade": asset.grade,
        "status": asset.status.value, "station_name": label(asset.status), "station_code": code(asset.status),
        "location_code": station, "since": since, "days_so_far": ((today - since).days if since else None),
        "steps": steps, "moves_logged": len(steps),
        "warehouse_days_measured": warehouse_days, "customer_days_measured": customer_days, "unknown_stays": unknown,
        "history_reason": (None if steps else "no move logged for this device: " + HISTORY_NOTE),
        "as_of": today,
    }

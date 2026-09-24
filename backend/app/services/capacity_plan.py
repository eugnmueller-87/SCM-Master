"""The capacity plan in the DaaS scenario: how fast each compartment must turn for the
fleet the owner wants, and when it runs out of room.

The business owner's question, 24.09.2026: what maximum capacity do we assume per
warehouse compartment, how fast must each turn to reach 500,000 devices at customers by
the end of 2026 and 1,000,000 by the end of 2027, and when do we have to lease more room.

``warehouse.py`` derives a flow from stock and dwell (Little's law: flow = stock / dwell).
This module runs the same law the other way. A target fleet implies a flow, a flow at
today's dwell implies a stock, and a stock above the capacity is the date a lease has
to be signed. The chain, every step of which the response carries:

  1. A fleet of F devices at customers with a mean rental term of T months returns F / T
     devices a month. T is measured from the running contracts.
  2. Growing to the target needs new devices placed: the net growth per month plus the
     returns that leave for good (sold, recycled), which have to be replaced.
  3. Every return walks the chain. The split is the rule in ``fleet.NEXT_STEP_SHARE``
     (after a first rental most go to a second rental, grade C to repair first, after a
     second rental to sale), applied over the measured mix of first and second rentals.
  4. So each compartment has a required throughput at each milestone.
  5. At today's mean dwell, required stock = throughput x dwell, against the capacity.
  6. Where it does not fit there are two levers, both given, neither picked: the dwell
     the compartment would have to reach to fit today's capacity (capacity / throughput),
     and the extra places needed at today's dwell.
  7. When it breaks: the fleet is interpolated month by month from today to each
     milestone, and the first month a compartment's required stock crosses its capacity
     is reported. That month is the lead time for a lease or a shift plan.

The swap buffer is a reserve, not a queue: it holds replacement devices against
customer defects, and its size follows the fleet it insures, not a flow. It is scaled by
today's buffer per rented device, and the dwell lever does not apply to it.

**Derived, and which way it leans.** The dwell inverted here is the mean age of the
stock still present, the same dwell ``warehouse.py`` uses, because there is no movement
log. The age of a unit still here is shorter than its finished stay, so the required
stock and the breach dates come out too small: the plan looks better than it is. Every
compartment therefore also reports what is on hand today next to what the model says
today's fleet needs, so the bias is visible per compartment instead of hidden. The mean
is used rather than the median because Little's law is a statement about means, and
the Warehouse tab's derived flow round-trips only with the mean.

**Targets are owned.** The two milestones are the business owner's instruction and are
written once with that owner; they are edited through the API the way a KPI target is.
Capacity per compartment is the design parameter the seed wrote until a person sets it,
and the row says which.

**Everything aggregates in the database**: the compartments' read (one grouped query
over the warehouse), the rented fleet by cycle and the running terms by cycle from
covering indexes, three counts for the measured cross-checks. The response is a few
hundred numbers however large the fleet.

**No fake zeros.** A figure the data cannot support is ``None`` with a ``reason``.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.flow import Asset, AssetStatus, Location, LocationType
from app.models.kpi import FleetMilestone
from app.models.rental import ContractStatus, RentalContract
from app.services import fleet, warehouse
from app.services.exceptions import NotFoundError, ValidationError

DAYS_PER_MONTH = 30.4375
MEASURED_MONTHS = 3                     # first rentals are averaged over this many full months
CAPACITY_OWNER = "Head of Operations"   # the role that decides a station's capacity; until then it is the seed's design parameter

# The business owner's instruction of 24.09.2026, written once into ``fleet_milestone``
# when the table is empty. Owned, not a placeholder. The owner is recorded as the role:
# customers and partners are role-only in this repository.
OWNER = "Business owner"
OWNER_MILESTONES = ((date(2026, 12, 31), 500_000), (date(2027, 12, 31), 1_000_000))
OWNER_NOTE = "instruction of 24.09.2026: 500,000 devices at customers by the end of 2026, 1,000,000 by the end of 2027"

DWELL_BASIS = ("derived: today's mean age of the stock still present, the dwell warehouse.py uses; a unit still here has "
               "not finished its stay, so the required stock and the breach dates lean small and the plan looks better than it is")
FLOW_BASIS = ("required throughput = the return flow at the milestone fleet (fleet / mean term) times the share of returns that "
              "walk through this compartment under the next-step rule over the measured mix of first and second rentals")
NEW_BASIS = ("required throughput = net fleet growth per month on the path plus the returns that leave for good (sold, recycled) "
             "and have to be replaced")
SWAP_BASIS = "reserve, not a queue: scaled by today's swap buffer per rented device; there is no flow, so no dwell lever"
EXIT_BASIS = "the next-step rule (fleet.NEXT_STEP_SHARE) over the measured mix of first and second rentals: sale plus recycling"


def _r0(x) -> Optional[int]:
    return None if x is None else int(round(x))


def _r1(x) -> Optional[float]:
    return None if x is None else round(float(x), 1)


def _r2(x) -> Optional[float]:
    return None if x is None else round(float(x), 2)


def _r4(x) -> Optional[float]:
    return None if x is None else round(float(x), 4)


def _as_date(v) -> Optional[date]:
    """SQLite hands a date back as text on some paths; Postgres gives a date. A datetime
    (an audit column) is cut to its day: the response promises dates, not timestamps."""
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    return date.fromisoformat(str(v)[:10])


def _is_daas(db: Session) -> bool:
    """A database with a rented device is a DaaS fleet: an existence probe, not a count."""
    return db.scalar(select(Asset.id).where(Asset.status == AssetStatus.RENTED).limit(1)) is not None


# ---------------------------------------------------------------------------
# the measured inputs


def _rented_by_cycle(db: Session) -> tuple[int, int]:
    """(devices on their first rental, devices on a second or later one), from the (status, cycle) index."""
    rows = db.execute(select(Asset.cycle_no, func.count()).where(Asset.status == AssetStatus.RENTED).group_by(Asset.cycle_no)).all()
    first = sum(int(n) for c, n in rows if int(c or 0) < 2)
    second = sum(int(n) for c, n in rows if int(c or 0) >= 2)
    return first, second


def _term(db: Session) -> tuple[Optional[float], int, dict[str, float]]:
    """Mean term in months of the running contracts, how many, and the mean per cycle.

    From the (status, cycle, term) index alone: a handful of rows however many contracts
    run. The running contracts are the ones that come back over the plan, and their term
    is a contractual fact, which is why the term is read here and not estimated from
    contracts that ended early.
    """
    rows = db.execute(select(RentalContract.cycle_no, RentalContract.term_months, func.count())
                      .where(RentalContract.status == ContractStatus.RUNNING)
                      .group_by(RentalContract.cycle_no, RentalContract.term_months)).all()
    total = sum(int(n) for _, _, n in rows)
    if not total:
        return None, 0, {}
    by_cycle: dict[str, list[float]] = {}
    for cyc, term, n in rows:
        key = "2" if int(cyc or 0) >= 2 else "1"
        acc = by_cycle.setdefault(key, [0.0, 0.0])
        acc[0] += float(term) * int(n)
        acc[1] += int(n)
    mean = sum(v[0] for v in by_cycle.values()) / total
    return mean, total, {k: round(v[0] / v[1], 2) for k, v in by_cycle.items()}


def _counts_12m(db: Session, today: date) -> tuple[int, int]:
    """Contracts ended, and devices that left for good (sold, recycled), in the last twelve
    months: the measured cross-check for the rule's exit share. Two index counts."""
    since = today - timedelta(days=365)
    ended = int(db.scalar(select(func.count()).select_from(RentalContract)
                          .where(RentalContract.status == ContractStatus.ENDED, RentalContract.actual_end >= since)) or 0)
    gone = int(db.scalar(select(func.count()).select_from(Asset)
                         .where(Asset.status.in_((AssetStatus.SOLD, AssetStatus.RECYCLED)), Asset.sold_date >= since)) or 0)
    return ended, gone


def _first_rentals(db: Session, today: date) -> tuple[Optional[float], int]:
    """New devices placed per month today, measured: first rentals started in the last full
    months, from the (cycle, start) index."""
    first_this = date(today.year, today.month, 1)
    idx = today.year * 12 + (today.month - 1) - MEASURED_MONTHS
    since = date(idx // 12, idx % 12 + 1, 1)
    n = int(db.scalar(select(func.count()).select_from(RentalContract)
                      .where(RentalContract.cycle_no == 1, RentalContract.start_date >= since,
                             RentalContract.start_date < first_this)) or 0)
    return (n / MEASURED_MONTHS if n else None), n


def _shares(c2: float) -> dict[str, float]:
    """The share of the return flow that walks through each compartment, and the share
    that leaves for good.

    ``fleet.NEXT_STEP_SHARE`` says where a return goes by cycle; the measured share of
    second rentals says how many returns are of which cycle. Every return passes intake,
    the MDM hold and wipe and grading (a return may skip the hold; only the movement log
    could say how many do). A repaired device is refurbished afterwards and waits in the
    second-life stock, so those two carry the repairs as well.
    """
    def mix(pick) -> float:
        return (1.0 - c2) * pick(fleet.NEXT_STEP_SHARE[1]) + c2 * pick(fleet.NEXT_STEP_SHARE[2])

    second = mix(lambda s: s["second_rental"] + s["repair"])
    return {
        "ST-RETURNS": 1.0, "ST-MDM": 1.0, "ST-WIPE": 1.0,
        "ST-REPAIR": mix(lambda s: s["repair"]),
        "ST-REFURB": second, "ST-SECOND": second,
        "ST-SELL": mix(lambda s: s["sale"]),
        "exit": mix(lambda s: s["sale"] + s["recycling"]),
    }


# ---------------------------------------------------------------------------
# the model


@dataclass(frozen=True)
class _Model:
    fleet_now: int
    term: Optional[float]                # months
    shares: Optional[dict[str, float]]
    swap_ratio: Optional[float]          # swap buffer per rented device


def _throughput(code: str, F: float, growth: float, M: _Model) -> Optional[float]:
    """Devices a month through one compartment at a fleet of F growing by ``growth`` a month."""
    if M.term is None or M.shares is None or code == "ST-SWAP":
        return None
    returns = F / M.term
    if code == "ST-NEW":
        return max(0.0, growth + returns * M.shares["exit"])
    return returns * M.shares[code]


def _required(code: str, F: float, growth: float, dwell_days: Optional[float], M: _Model) -> Optional[float]:
    """Stock one compartment holds at fleet F at today's dwell (Little's law), or the reserve's size."""
    if code == "ST-SWAP":
        return None if M.swap_ratio is None else M.swap_ratio * F
    t = _throughput(code, F, growth, M)
    if t is None or dwell_days is None:
        return None
    return t * dwell_days / DAYS_PER_MONTH


def _why(code: str, c: dict, M: _Model) -> Optional[str]:
    """Why a compartment has no required stock, if it has none."""
    if M.term is None:
        return "no running rental contract: the mean term, and with it the return flow, cannot be measured"
    if code == "ST-SWAP":
        return None if M.swap_ratio is not None else "no swap buffer held today: nothing to scale with the fleet"
    return c["dwell_reason"]


def _months(a: date, b: date) -> float:
    return (b - a).days / DAYS_PER_MONTH


def _month_end(y: int, m: int) -> date:
    return (date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)) - timedelta(days=1)


def _segments(today: date, fleet_now: int, ms: list[FleetMilestone]) -> list[tuple[date, float, date, float, float]]:
    """(from date, from fleet, to date, to fleet, growth per month), one per milestone in order."""
    segs = []
    d0, f0 = today, float(fleet_now)
    for m in ms:
        d1, f1 = _as_date(m.milestone_date), float(m.target_fleet)
        months = _months(d0, d1)
        segs.append((d0, f0, d1, f1, (f1 - f0) / months if months > 0 else 0.0))
        d0, f0 = d1, f1
    return segs


def _fleet_at(d: date, segs, fleet_now: int) -> tuple[float, float, Optional[date]]:
    """The fleet on the path on day ``d``, the growth of the segment it is in, and that segment's milestone."""
    for d0, f0, d1, f1, g in segs:
        if d <= d1:
            span = max(1, (d1 - d0).days)
            return f0 + (f1 - f0) * (d - d0).days / span, g, d1
    if segs:
        d0, f0, d1, f1, g = segs[-1]
        return f1, g, d1
    return float(fleet_now), 0.0, None


def _path(today: date, fleet_now: int, segs) -> list[tuple[date, float, float, Optional[date]]]:
    """Today, then every month end up to the last milestone, the milestone dates included."""
    dates = {today}
    if segs:
        last = segs[-1][2]
        dates |= {s[2] for s in segs}
        y, m = today.year, today.month
        while True:
            e = _month_end(y, m)
            if e >= last:
                break
            if e > today:
                dates.add(e)
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return [(d, *_fleet_at(d, segs, fleet_now)) for d in sorted(dates)]


# ---------------------------------------------------------------------------
# milestones and capacities: the owned inputs


def milestones(db: Session) -> list[FleetMilestone]:
    """Every milestone, oldest first. An empty table is seeded once with the owner's instruction."""
    stmt = select(FleetMilestone).order_by(FleetMilestone.milestone_date)
    rows = list(db.execute(stmt).scalars().all())
    if rows:
        return rows
    for d, n in OWNER_MILESTONES:
        db.add(FleetMilestone(milestone_date=d, target_fleet=n, owner=OWNER, note=OWNER_NOTE, placeholder=False, updated_by="seed"))
    db.flush()
    return list(db.execute(stmt).scalars().all())


def set_milestone(db: Session, milestone_date: date, *, target_fleet: int, owner: Optional[str], note: Optional[str],
                  actor: Optional[str], today: Optional[date] = None) -> FleetMilestone:
    """Set the target fleet on a date; a new date adds a milestone.

    The owner stays unless a new one is named, so a planner trying "and if it were
    800,000?" does not disown the owner's milestone; who changed the number is recorded
    as ``updated_by``.
    """
    today = today or date.today()
    if milestone_date <= today:
        raise ValidationError(f"a milestone has to lie ahead of today ({today.isoformat()}); {milestone_date.isoformat()} does not")
    if target_fleet < 0:
        raise ValidationError("a target fleet cannot be negative")
    row = db.execute(select(FleetMilestone).where(FleetMilestone.milestone_date == milestone_date)).scalar_one_or_none()
    if row is None:
        row = FleetMilestone(milestone_date=milestone_date)
        db.add(row)
    row.target_fleet = int(target_fleet)
    if owner is not None:
        row.owner = owner
    if note is not None:
        row.note = note
    row.placeholder = False
    row.updated_by = actor
    db.flush()
    return row


def set_capacity(db: Session, code: str, *, capacity: int, actor: Optional[str]) -> Location:
    """Set a compartment's station capacity and record who did. A compartment whose station
    does not exist yet gets one, the way the seed creates them."""
    c = warehouse.COMPARTMENT_BY_CODE.get(code)
    if c is None:
        raise NotFoundError(f"No warehouse compartment with code {code!r}")
    if capacity < 0:
        raise ValidationError("a capacity cannot be negative")
    loc = db.execute(select(Location).where(Location.code == code)).scalar_one_or_none()
    if loc is None:
        loc = Location(code=c.code, name=c.name, location_type=LocationType.WAREHOUSE)
        db.add(loc)
    loc.capacity = int(capacity)
    loc.capacity_set_by = actor
    db.flush()
    return loc


# ---------------------------------------------------------------------------
# the plan


def plan(db: Session, *, today: Optional[date] = None) -> dict:
    """The capacity plan: model, milestones with one row per compartment, the path, and per
    compartment the month it breaks. See the module docstring."""
    today = today or date.today()
    if not _is_daas(db):
        return {"scenario": "datacenter", "as_of": today, "fleet_now": None, "model": None, "milestones": [], "path": [],
                "compartments": [], "first_breach": None,
                "reason": "the capacity plan exists in the device-as-a-service scenario; this database holds the datacenter operation"}

    W = warehouse.compartments(db, today=today)
    first, second = _rented_by_cycle(db)
    fleet_now = first + second
    term, n_terms, term_by_cycle = _term(db)
    ended_12m, gone_12m = _counts_12m(db, today)
    placed, placed_n = _first_rentals(db, today)
    stations = {code: (set_by, _as_date(updated)) for code, set_by, updated in db.execute(
        select(Location.code, Location.capacity_set_by, Location.last_updated)
        .where(Location.code.in_([c.code for c in warehouse.COMPARTMENTS]))).all()}

    c2 = (second / fleet_now) if fleet_now else None
    shares = _shares(c2) if c2 is not None else None
    swap_on_hand = next((c["on_hand"] for c in W["compartments"] if c["code"] == "ST-SWAP"), 0)
    swap_ratio = (swap_on_hand / fleet_now) if (fleet_now and swap_on_hand) else None
    M = _Model(fleet_now, term, shares, swap_ratio)
    reason = None
    if not fleet_now:
        reason = "no rented device: no fleet to plan from"
    elif term is None:
        reason = "no running rental contract: the mean term, and with it the return flow, cannot be measured"

    ms = [m for m in milestones(db) if _as_date(m.milestone_date) > today]
    segs = _segments(today, fleet_now, ms)
    path = _path(today, fleet_now, segs)
    growth_now = segs[0][4] if segs else 0.0
    returns_now = (fleet_now / term) if term else None

    model = {
        "term_months": _r2(term), "term_contracts": n_terms, "term_by_cycle": term_by_cycle,
        "term_reason": (None if term is not None else "no running rental contract"),
        "cycle2_share": _r4(c2),
        "returns_per_month_now": _r0(returns_now),
        "exit_share": (_r4(shares["exit"]) if shares else None), "exit_share_basis": EXIT_BASIS,
        "returns_12m": ended_12m, "gone_12m": gone_12m,
        "exit_share_measured_12m": (_r4(gone_12m / ended_12m) if ended_12m else None),
        "exit_share_measured_reason": (None if ended_12m else "no contract ended in the last twelve months"),
        "first_rentals_per_month_measured": _r0(placed), "first_rentals_months": MEASURED_MONTHS,
        "first_rentals_reason": (None if placed is not None else f"no first rental started in the last {MEASURED_MONTHS} full months"),
        "next_step_share": {str(k): dict(v) for k, v in fleet.NEXT_STEP_SHARE.items()},
        "swap_ratio": _r4(swap_ratio), "swap_basis": SWAP_BASIS,
        "dwell_basis": DWELL_BASIS,
        "reason": reason,
    }

    # per compartment: the facts, today's model against today's stock, and when it breaks
    comp_rows = []
    for c in W["compartments"]:
        code, cap, dwell = c["code"], c["capacity"], c["mean_days"]
        set_by, set_on = stations.get(code, (None, None))
        required_now = _required(code, fleet_now, growth_now, dwell, M)
        why = _why(code, c, M)
        breach, state, breach_reason = None, "unknown", why
        if cap is None:
            state, breach_reason = "no_capacity", c["capacity_reason"]
        elif why is None:
            for d, F, g, _toward in path:
                req = _required(code, F, g, dwell, M)
                if req is not None and req > cap:
                    breach = (d, F, req)
                    break
            if breach is None:
                state = "fits"
                breach_reason = (f"fits through {path[-1][0].isoformat()} at today's dwell" if segs
                                 else "no milestone ahead of today: nothing to plan toward")
            else:
                state = "now" if breach[0] == today else "later"
                breach_reason = ("at today's fleet the model already needs more than the capacity" if state == "now"
                                 else f"the required stock crosses the capacity at about {_r0(breach[1]):,} devices at customers")
        over_today = bool(c["over_capacity"])
        if over_today:
            state = "over_today"
        comp_rows.append({
            "code": code, "name": c["name"], "step": c["step"], "stage": c["stage"], "holds": c["holds"],
            "on_hand": c["on_hand"], "capacity": cap,
            "capacity_placeholder": (cap is not None and set_by is None), "capacity_owner": CAPACITY_OWNER,
            "capacity_set_by": set_by, "capacity_set_on": (set_on if set_by else None), "capacity_reason": c["capacity_reason"],
            "dwell_days": _r1(dwell), "dwell_median_days": _r1(c["median_days"]), "dwell_reason": c["dwell_reason"],
            "flow_basis": (SWAP_BASIS if code == "ST-SWAP" else NEW_BASIS if code == "ST-NEW" else FLOW_BASIS),
            "share_of_returns": (_r4(shares[code]) if shares and code in shares else None),
            "required_now": _r0(required_now), "required_now_reason": why,
            "over_capacity_today": over_today,
            "breach_state": state,
            "breach_month": (breach[0].strftime("%Y-%m") if breach else None),
            "breach_date": (breach[0] if breach else None),
            "breach_fleet": (_r0(breach[1]) if breach else None),
            "breach_now": bool(breach and breach[0] == today),
            "breach_reason": breach_reason,
        })

    # the first to break: over capacity today counts as broken today; ties go to the worst ratio at the last milestone
    def _effective(r: dict) -> Optional[date]:
        if r["over_capacity_today"]:
            return today
        return r["breach_date"]

    def _ratio(r: dict) -> float:
        if not r["capacity"] or not segs:
            return 0.0
        f1, g = segs[-1][3], segs[-1][4]
        req = _required(r["code"], f1, g, r["dwell_days"], M)
        return (req / r["capacity"]) if req is not None else 0.0

    breaking = [r for r in comp_rows if _effective(r) is not None]
    first_breach = None
    if breaking:
        r = min(breaking, key=lambda x: (_effective(x), -_ratio(x)))
        first_breach = {"code": r["code"], "name": r["name"], "month": _effective(r).strftime("%Y-%m"), "date": _effective(r),
                        "over_capacity_today": r["over_capacity_today"], "breach_now": r["breach_now"], "state": r["breach_state"]}

    # one block per milestone, one row per compartment
    ms_rows = []
    for m, (d0, f0, d1, f1, g) in zip(ms, segs):
        F = f1
        returns = (F / term) if term else None
        rows = []
        for c in W["compartments"]:
            code, cap, dwell = c["code"], c["capacity"], c["mean_days"]
            t = _throughput(code, F, g, M)
            req = _required(code, F, g, dwell, M)
            why = _why(code, c, M)
            if cap is None:
                why = c["capacity_reason"] if why is None else why
            required_dwell = (cap / t * DAYS_PER_MONTH) if (cap is not None and t) else None
            gap = (cap - req) if (cap is not None and req is not None) else None
            rows.append({
                "code": code, "name": c["name"], "step": c["step"],
                "throughput_per_month": _r0(t),
                "throughput_reason": (None if t is not None else ("a reserve has no flow" if code == "ST-SWAP" else why)),
                "dwell_days": _r1(dwell),
                "required_stock": _r0(req), "capacity": cap, "gap": _r0(gap),
                "fits": (None if gap is None else gap >= 0),
                "required_dwell_days": _r1(required_dwell),
                "required_dwell_reason": (None if required_dwell is not None
                                          else ("a reserve has no flow to speed up" if code == "ST-SWAP"
                                                else (c["capacity_reason"] if cap is None else why))),
                "extra_places": (_r0(max(0.0, -gap)) if gap is not None else None),
                "reason": (why if req is None else (c["capacity_reason"] if cap is None else None)),
            })
        extras = [r["extra_places"] for r in rows if r["extra_places"] is not None]
        ms_rows.append({
            "date": d1, "target_fleet": m.target_fleet, "owner": m.owner, "note": m.note, "placeholder": m.placeholder,
            "updated_by": m.updated_by,
            "months_from_today": _r2(_months(today, d1)), "growth_per_month": _r0(g),
            "returns_per_month": _r0(returns),
            "exits_per_month": (_r0(returns * shares["exit"]) if (returns is not None and shares) else None),
            "placements_per_month": _r0(_throughput("ST-NEW", F, g, M)),
            "second_rentals_per_month": (_r0(returns * shares["ST-SECOND"]) if (returns is not None and shares) else None),
            "extra_places_total": (sum(extras) if extras else None),
            "compartments_short": sum(1 for r in rows if r["fits"] is False),
            "rows": rows,
        })

    return {
        "scenario": "daas", "as_of": today, "fleet_now": fleet_now,
        "model": model,
        "milestones": ms_rows,
        "path": [{"date": d, "month": d.strftime("%Y-%m"), "fleet": _r0(F), "growth_per_month": _r0(g), "toward": toward}
                 for d, F, g, toward in path],
        "compartments": comp_rows,
        "first_breach": first_breach,
        "reason": (reason if reason else (None if ms else "no milestone ahead of today: set one through the API")),
    }

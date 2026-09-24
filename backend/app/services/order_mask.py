"""The ordering mask: pick a device, a manufacturer or a class, see what is needed and why,
type a quantity, and see the consequence before anything is ordered.

The business owner's instruction of 24.09.2026: "if we start to purchase stuff, it needs to
open a mask which tells me how much I need and because of which factors. I want to
simulate, for example, 100 Fairphones. What will it look like? Then the system needs to
show me: okay, we will overcommit, or we are not at the buffer, or it's not enough right
now. I want people to have time for other things than calculating."

Nothing here places an order. The mask composes reads that already exist and answers a
what-if; the purchasing gate (the requisition cycle and the over-order guard) stays where
it is. Every figure says what it is: measured, derived (with the rule), or a placeholder
with the role that owns it.

**Where the arithmetic comes from.** Demand over the horizon is ``planning.demand_forecast``
(usage measured from rental starts, recency weighted, plus end-of-life replacements). The
buffer is ``planning.inventory_plan``'s service-level safety stock (a measured sigma over
lead-time buckets at the ABC class's service level). What is inbound is the open order
lines of ``planning.inbound_pipeline``, what is already staged is the open requisitions,
and whether an order fits is ``planning.check_order_capacity``, the same guard the order
path enforces, reused and not restated. The compartments are ``warehouse.compartments``
and the breach dates ``capacity_plan.plan``.

**What we already own that could serve this demand.** The owner's point, and the difference
between ordering and not ordering. Which stock can go out to a customer next is a property
of the state machine, not of this module: a compartment serves a rental when
``lifecycle`` allows its status to step to RENTED. Today that is new stock (a first
rental), second-life stock (a second rental), the swap buffer (a reserve for customer
defects) and sellable stock (cleared for resale, but the lifecycle still allows a rental).
The first two are counted against the need, the way every other read in this system
counts what can go out next (``DEPLOYABLE_STATUSES``); the other two are shown with what
taking them costs or forecloses, and are not counted. The return chain is what becomes
available on a horizon: the compartments from which second-life stock is reachable, with
the share the fleet's next-step rule expects to get there and the target dwells ahead.

**The what-if.** A quantity typed here is checked against the guard (does it fit the
warehouse), against the intake compartment (does new stock cross its capacity, now and on
the delivery date at today's measured outflow), against the capacity plan's breach, and
priced at the contract price with the landed adder; and the answer says what the quantity
covers against the gap. When there is no room, the levers that would make room are
measured from the compartments: sellable stock to move out, units past their target dwell
in the return chain, late inbound lines, or the places to lease.

**One recommendation, its factors named.** The recommended quantity is gross demand plus
the buffer, less the stock that can go out next, less what is inbound, less what is
already staged, rounded up to the source's minimum order quantity, then capped by the
guard. Each factor is a row a person can add up. The purchasing agent's position model
(``planning.inventory_position``) nets the same ingredients with the buffer subtracted
from the need instead of added; its figure is carried as a cross-check with that basis,
so the two numbers a person may meet in this system are explained next to each other
rather than left to disagree.

**Cost.** About two and a half seconds on the 431,200-serial fleet, most of it the two
fleet-wide planning reads (the forecast and the inventory plan) and the guard, which
computes the whole capacity-and-flow picture to answer whether a quantity fits. The
scope's own reads are grouped queries on the (status, product) index.

**No fake zeros.** A figure the data cannot support is ``None`` with a ``reason``.
"""
from __future__ import annotations

import math
import time
from collections import Counter
from datetime import date, timedelta
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import seed_daas as rules
from app.core.config import settings
from app.models.catalog import Organization, Product, ProductSupplier
from app.models.flow import WAREHOUSE_STATUSES, Asset, AssetStatus
from app.models.rental import ContractStatus, RentalContract
from app.services import capacity_plan, fleet, lifecycle, planning, warehouse
from app.services.exceptions import NotFoundError, ValidationError

DAYS_PER_MONTH = capacity_plan.DAYS_PER_MONTH
INTAKE = "ST-NEW"                  # where a delivery lands: the station every open order is destined for
SECOND_LIFE = "ST-SECOND"
DEFECT_WINDOW_DAYS = 90            # defects and swaps are measured over this window
RESALE_WINDOW_DAYS = 365           # forgone proceeds are measured over this window
RENT2_OWNER = "Head of Sales"      # the owner of the second-rent share, as the seed names it

# ---------------------------------------------------------------------------
# the tiers: read off the state machine, never listed by hand

# A compartment can serve a rental when the lifecycle lets its primary status step to RENTED.
_RENTABLE_CODES = tuple(c.code for c in warehouse.COMPARTMENTS
                        if any(lifecycle.can_transition(s, AssetStatus.RENTED) for s in c.statuses))
# Counted against the need: what every other read calls "can go out next" (planning._ON_HAND).
_COUNTED_CODES = tuple(c.code for c in warehouse.COMPARTMENTS if c.statuses[0] in planning._ON_HAND)

# What taking each rentable tier costs or forecloses. A rentable compartment without a line
# here is still a tier; it just carries no cost text.
_TIER_TEXT = {
    "ST-NEW": ("first rental", "New devices: what they were bought for. Taking them costs nothing; the oldest go first."),
    "ST-SECOND": ("second rental", "Refurbished grade A and B devices for a customer that takes a used device. The rent is a share of a first rental's."),
    "ST-SWAP": ("reserve", "Held against customer defects. Every unit taken is one defect the buffer no longer covers until it is refilled."),
    "ST-SELL": ("exit", "Cleared for resale; the lifecycle still allows a rental. Every unit rented instead of sold forgoes its resale proceeds."),
}

# The return chain: not rentable, but second-life stock is reachable from it inside the warehouse.
_WH = tuple(WAREHOUSE_STATUSES)


def _reaches(src: AssetStatus, dst: AssetStatus) -> bool:
    seen, stack = set(), [src]
    while stack:
        s = stack.pop()
        for n in lifecycle.allowed_transitions(s):
            if n == dst:
                return True
            if n in WAREHOUSE_STATUSES and n not in seen:
                seen.add(n)
                stack.append(n)
    return False


_CHAIN_CODES = tuple(c.code for c in warehouse.COMPARTMENTS
                     if c.code not in _RENTABLE_CODES and _reaches(c.statuses[0], AssetStatus.READY_SECOND))


def _horizons(src: AssetStatus) -> tuple[Optional[int], Optional[int]]:
    """The target dwell days ahead of a unit in ``src`` before it is second-life stock: the shortest
    and the longest simple path through the warehouse the state machine allows, the target dwell
    of every compartment on the way summed (the compartment it is in included, second-life stock
    excluded). Placeholders, because the targets are."""
    target = {s: c.target_dwell_days for c in warehouse.COMPARTMENTS for s in c.statuses}
    best: list[Optional[int]] = [None, None]

    def walk(s: AssetStatus, acc: int, seen: frozenset) -> None:
        for n in lifecycle.allowed_transitions(s):
            if n == AssetStatus.READY_SECOND:
                best[0] = acc if best[0] is None else min(best[0], acc)
                best[1] = acc if best[1] is None else max(best[1], acc)
            elif n in WAREHOUSE_STATUSES and n not in seen and n in target:
                walk(n, acc + target[n], seen | {n})

    walk(src, target.get(src, 0), frozenset({src}))
    return best[0], best[1]


# ---------------------------------------------------------------------------
# the scope


def _is_daas(db: Session) -> bool:
    return db.scalar(select(Asset.id).where(Asset.status == AssetStatus.RENTED).limit(1)) is not None


def _catalogue(db: Session) -> list[dict]:
    """Every buyable product with its class and manufacturer (the manufacturer of its preferred source)."""
    rows = db.execute(
        select(Product.id, Product.product_code, Product.name, Product.category,
               Organization.code, Organization.name, ProductSupplier.preference_rank)
        .outerjoin(ProductSupplier, (ProductSupplier.product_id == Product.id) & ProductSupplier.active.is_(True))
        .outerjoin(Organization, Organization.id == ProductSupplier.manufacturer_id)
        .where(Product.active.is_(True))
        .order_by(Product.name, ProductSupplier.preference_rank)
    ).all()
    keep = planning._procurement_excluded(db, {r[0] for r in rows})
    out: dict[str, dict] = {}
    for pid, code, name, family, mcode, mname, _rank in rows:
        if pid not in keep:
            continue
        p = out.setdefault(pid, {"product_id": pid, "code": code, "name": name, "family": family,
                                 "manufacturer": None, "manufacturer_code": None})
        if p["manufacturer"] is None and mname:
            p["manufacturer"], p["manufacturer_code"] = mname, mcode
    return list(out.values())


def scopes(db: Session) -> dict:
    """What the mask can be opened for: the models, the manufacturers, the classes."""
    cat = _catalogue(db)
    return {
        "scenario": ("daas" if _is_daas(db) else "datacenter"),
        "products": cat,
        "manufacturers": sorted({p["manufacturer"] for p in cat if p["manufacturer"]}),
        "families": sorted({p["family"] for p in cat if p["family"]}),
    }


def _resolve(db: Session, *, product_code: Optional[str], manufacturer: Optional[str], family: Optional[str]) -> tuple[list[dict], str]:
    cat = _catalogue(db)
    sel = cat
    if product_code:
        sel = [p for p in sel if p["code"] and p["code"].lower() == product_code.strip().lower()]
        if not sel:
            raise NotFoundError(f"No buyable product with code {product_code!r}")
    if manufacturer:
        m = manufacturer.strip().lower()
        sel = [p for p in sel if (p["manufacturer"] or "").lower() == m or (p["manufacturer_code"] or "").lower() == m]
    if family:
        f = family.strip().lower()
        sel = [p for p in sel if (p["family"] or "").lower() == f]
    if not sel:
        raise NotFoundError("No buyable product matches that scope")
    if product_code:
        label = sel[0]["name"]
    else:
        parts = [p for p in (manufacturer, family) if p]
        label = " ".join(parts) if parts else "the whole catalogue"
        if family and not manufacturer:
            label = f"all {family.lower()}s" if not family.lower().endswith("s") else f"all {family.lower()}"
        elif manufacturer and family:
            label = f"{manufacturer} {family.lower()}s"
    return sel, label


# ---------------------------------------------------------------------------
# the scope's stock, one compartment at a time


def _scope_stock(db: Session, pids: list[str], today: date) -> dict[str, dict]:
    """Per compartment: how many of the scope's devices, how old, which cycle, which grade.
    Two grouped reads on the (status, product) index; the rows are dates and grades, not devices."""
    dwell = db.execute(
        select(Asset.status, Asset.status_since, func.count())
        .where(Asset.product_id.in_(pids), Asset.status.in_(_WH))
        .group_by(Asset.status, Asset.status_since)
    ).all()
    cond = db.execute(
        select(Asset.status, Asset.cycle_no, Asset.grade, func.count())
        .where(Asset.product_id.in_(pids), Asset.status.in_(_WH))
        .group_by(Asset.status, Asset.cycle_no, Asset.grade)
    ).all()
    out = {c.code: {"units": 0, "undated": 0, "hist": {}, "cycles": Counter(), "grades": Counter()} for c in warehouse.COMPARTMENTS}
    for st, since, n in dwell:
        comp = warehouse.STATION_OF_STATUS.get(st if isinstance(st, AssetStatus) else AssetStatus(str(st)))
        if comp is None:
            continue
        s = out[comp.code]
        s["units"] += int(n)
        if since is None:
            s["undated"] += int(n)
        else:
            d = max(0, (today - warehouse._as_date(since)).days)
            s["hist"][d] = s["hist"].get(d, 0) + int(n)
    for st, cyc, grade, n in cond:
        comp = warehouse.STATION_OF_STATUS.get(st if isinstance(st, AssetStatus) else AssetStatus(str(st)))
        if comp is None:
            continue
        out[comp.code]["cycles"][warehouse._cycle_key(cyc)] += int(n)
        out[comp.code]["grades"][grade] += int(n)
    return out


def _scope_stock_by_product(db: Session, pids: list[str]) -> dict[str, Counter]:
    """{product_id: Counter(compartment code -> units)} from one grouped read."""
    out: dict[str, Counter] = {pid: Counter() for pid in pids}
    for pid, st, n in db.execute(select(Asset.product_id, Asset.status, func.count())
                                 .where(Asset.product_id.in_(pids), Asset.status.in_(_WH))
                                 .group_by(Asset.product_id, Asset.status)).all():
        comp = warehouse.STATION_OF_STATUS.get(st if isinstance(st, AssetStatus) else AssetStatus(str(st)))
        if comp is not None:
            out[pid][comp.code] += int(n)
    return out


def _tier_cost(db: Session, code: str, units: int, pids: list[str], stock: dict, today: date) -> dict:
    """What taking a tier costs or forecloses, measured where it can be, a placeholder where it cannot."""
    stage, text = _TIER_TEXT.get(code, ("", ""))
    cost: dict = {"text": text, "kind": None, "value": None, "unit": None, "basis": None, "reason": None, "measured": None}
    if code == INTAKE:
        cost.update(kind="none", basis="what the stock is for", measured=True)
    elif code == SECOND_LIFE:
        cost.update(kind="rent_share", value=rules.RENT2_SHARE, unit="share of a first rental's rent",
                    basis=f"placeholder, {RENT2_OWNER}: the seed's second-rent share", measured=False)
    elif code == "ST-SWAP":
        since = today - timedelta(days=DEFECT_WINDOW_DAYS)
        defects = int(db.scalar(select(func.count()).select_from(RentalContract)
                                .where(RentalContract.product_id.in_(pids), RentalContract.status == ContractStatus.ENDED,
                                       RentalContract.end_reason.in_(("defect", "swap")), RentalContract.actual_end >= since)) or 0)
        rented = int(db.scalar(select(func.count()).select_from(Asset)
                               .where(Asset.product_id.in_(pids), Asset.status == AssetStatus.RENTED)) or 0)
        per_month = defects / (DEFECT_WINDOW_DAYS / DAYS_PER_MONTH)
        cost.update(kind="defect_cover", value=(round(units / per_month, 1) if per_month else None), unit="months of defects covered",
                    basis=f"measured: {defects:,} contracts of this scope ended with a defect or a swap in the last {DEFECT_WINDOW_DAYS} days, "
                          f"{per_month:,.1f} a month; the buffer holds {units:,} against {rented:,} rented",
                    measured=True, defects_per_month=round(per_month, 2), rented=rented,
                    per_rented=(round(units / rented, 4) if rented else None),
                    reason=(None if per_month else f"no defect or swap ended a contract of this scope in the last {DEFECT_WINDOW_DAYS} days: "
                                                   "the cover cannot be put in months"))
    elif code == "ST-SELL":
        since = today - timedelta(days=RESALE_WINDOW_DAYS)
        n, total = db.execute(select(func.count(), func.coalesce(func.sum(Asset.sale_price), 0))
                              .where(Asset.product_id.in_(pids), Asset.status == AssetStatus.SOLD,
                                     Asset.sold_date >= since, Asset.sale_price.is_not(None))).one()
        n, total = int(n or 0), float(total or 0)
        cost.update(kind="forgone_proceeds", value=(round(total / n, 2) if n else None), unit="EUR net per device, forgone",
                    basis=f"measured: mean net proceeds of {n:,} priced sales of this scope in the last {RESALE_WINDOW_DAYS} days",
                    measured=True, sold=n, forgone_total=(round(total / n * units, 2) if n else None),
                    reason=(None if n else f"no priced sale of this scope in the last {RESALE_WINDOW_DAYS} days"))
    grades = stock["grades"]
    good = grades.get("A", 0) + grades.get("B", 0)
    cost["grade_ab_share"] = (round(good / units, 4) if units else None)
    return cost


def _owned(db: Session, pids: list[str], today: date, stock: dict[str, dict]) -> dict:
    """The tiers that could serve the demand, and the return chain on its horizon."""
    tiers = []
    for c in warehouse.COMPARTMENTS:
        if c.code not in _RENTABLE_CODES:
            continue
        s = stock[c.code]
        q = _quant(s["hist"])
        stage, _text = _TIER_TEXT.get(c.code, (c.stage, ""))
        tiers.append({
            "code": c.code, "name": c.name, "serves": stage, "statuses": [x.value for x in c.statuses],
            "counted": c.code in _COUNTED_CODES,
            "units": s["units"], "undated_units": s["undated"], **q,
            "past_target_units": sum(n for d, n in s["hist"].items() if d > c.target_dwell_days), "target_dwell_days": c.target_dwell_days,
            "cycles": [{"cycle": k, "label": warehouse.CYCLE_LABEL[k], "units": s["cycles"][k]} for k in ("0", "1", "2+") if s["cycles"].get(k)],
            "grades": [{"grade": g, "units": n} for g, n in sorted(s["grades"].items(), key=lambda kv: (kv[0] is None, kv[0] or ""))],
            "cost": _tier_cost(db, c.code, s["units"], pids, s, today),
        })
    chain_rows = []
    total = 0
    exp_second = 0.0
    exp_sale = 0.0
    for c in warehouse.COMPARTMENTS:
        if c.code not in _CHAIN_CODES:
            continue
        s = stock[c.code]
        lo, hi = _horizons(c.statuses[0])
        # the rule: after a first rental, second_rental + repair reach the second-life stock; after a second, everything is sold;
        # a device already in repair or refurbishment is on its way and counted whole
        c1 = s["cycles"].get("1", 0) + s["cycles"].get("0", 0)
        c2 = s["cycles"].get("2+", 0)
        if c.statuses[0] in (AssetStatus.REPAIR, AssetStatus.REFURB):
            second, sale = float(s["units"]), 0.0
        else:
            r1, r2 = fleet.NEXT_STEP_SHARE[1], fleet.NEXT_STEP_SHARE[2]
            second = c1 * (r1["second_rental"] + r1["repair"]) + c2 * (r2["second_rental"] + r2["repair"])
            sale = c1 * r1["sale"] + c2 * r2["sale"]
        total += s["units"]
        exp_second += second
        exp_sale += sale
        chain_rows.append({
            "code": c.code, "name": c.name, "units": s["units"], "undated_units": s["undated"], **_quant(s["hist"]),
            "target_dwell_days": c.target_dwell_days, "target_owner": c.target_owner,
            "cycles": [{"cycle": k, "label": warehouse.CYCLE_LABEL[k], "units": s["cycles"][k]} for k in ("0", "1", "2+") if s["cycles"].get(k)],
            "expected_second_life": int(round(second)), "expected_sale": int(round(sale)),
            "horizon_days_min": lo, "horizon_days_max": hi,
        })
    counted = sum(t["units"] for t in tiers if t["counted"])
    return {
        "rentable_basis": "a compartment serves a rental when the lifecycle allows its status to step to RENTED (services/lifecycle.py)",
        "counted_basis": "counted against the need: new stock and second-life stock, what every read here calls the stock that can go out next",
        "rentable_total": sum(t["units"] for t in tiers), "counted_total": counted,
        "not_counted_total": sum(t["units"] for t in tiers if not t["counted"]),
        "tiers": tiers,
        "return_chain": {
            "units": total, "expected_second_life": int(round(exp_second)), "expected_sale": int(round(exp_sale)),
            "rule_basis": "derived: fleet.NEXT_STEP_SHARE by rental cycle over the devices in each compartment; repair and refurbishment counted whole",
            "horizon_basis": "target dwell days of the compartments ahead on the paths the state machine allows, summed; placeholders with their owners",
            "compartments": chain_rows,
            "reason": (None if total else "nothing of this scope is in the return chain"),
        },
    }


def _quant(hist: dict[int, int]) -> dict:
    dated = sum(hist.values())
    if not dated:
        return {"dated_units": 0, "median_days": None, "oldest_days": None, "mean_days": None}
    return {"dated_units": dated, "median_days": warehouse._quantile_from_histogram(hist, 0.5), "oldest_days": max(hist),
            "mean_days": round(sum(d * n for d, n in hist.items()) / dated, 1)}


# ---------------------------------------------------------------------------
# the recommendation


def _split(q: int, weights: list[float]) -> list[int]:
    """``q`` over the products in proportion to ``weights`` (largest remainder), whole units."""
    if q <= 0 or not weights:
        return [0] * len(weights)
    if sum(weights) <= 0:
        weights = [1.0] * len(weights)
    total = float(sum(weights))
    raw = [q * w / total for w in weights]
    out = [int(math.floor(x)) for x in raw]
    for i in sorted(range(len(raw)), key=lambda i: raw[i] - out[i], reverse=True)[: q - sum(out)]:
        out[i] += 1
    return out


def _recommend(db: Session, products: list[dict], today: date, per_product_stock: dict[str, Counter]) -> tuple[list[dict], dict]:
    """Per product, the factors and the gap; the scope's factors summed."""
    fc = {r["product_id"]: r for r in planning.demand_forecast(db, today=today)}
    ip = {r["product_id"]: r for r in planning.inventory_plan(db, today=today)}
    staged = planning._staged_planned_by_product(db)
    horizon = settings.demand_horizon_days
    rows = []
    for p in products:
        pid = p["product_id"]
        f, i = fc.get(pid), ip.get(pid)
        src = planning._preferred_source(db, pid)
        st = per_product_stock.get(pid, Counter())
        new, second = st.get(INTAKE, 0), st.get(SECOND_LIFE, 0)
        usage = float(f["projected_usage"]) if f else 0.0
        eol = int(f["eol_replacement"]) if f else 0
        gross = int(math.ceil(float(f["projected_demand"]))) if f else 0
        buffer = int(i["safety_stock"]) if i else 0
        inbound = int(f["on_order"]) if f else 0
        stg = int(staged.get(pid, 0))
        need = gross + buffer
        gap = max(0, need - new - second - inbound - stg)
        moq = int((src.min_order_quantity or 1) if src else 1)
        rec = 0
        if gap > 0:
            rec = max(gap, moq)
            if moq > 1:
                rec = int(math.ceil(rec / moq) * moq)
        lead = int((src.standard_lead_time_days or 0) if src else 0)
        price = (float(src.contract_price) if src and src.contract_price is not None else None)
        rows.append({
            **p,
            "usage": round(usage, 1), "eol": eol, "gross": gross, "rate_per_day": (float(f["usage_rate_per_day"]) if f else 0.0),
            "method": (f["forecast_method"] if f else None),
            "buffer": buffer, "service_level": (i["service_level"] if i else None), "abc_class": (i["abc_class"] if i else None),
            "new": new, "second_life": second, "inbound": inbound, "staged": stg,
            "need": need, "gap": gap, "moq": moq, "recommended": rec,
            "lead_time_days": lead, "unit_price": price,
            "order_by": ((f["order_by"] if f and f["recommended_order_qty"] else None) if f else None),
            "forecast_recommended": (int(f["recommended_order_qty"]) if f else 0),
            "position_model_net": max(0, gross - (new + second + inbound) - buffer),
            "demand_reason": (None if f else "no usage, no stock and no inbound in the forecast window: the forecast carries no row for this model"),
            "buffer_reason": (None if i else "no stock and no inbound: the inventory plan carries no row for this model"),
            "source_reason": (None if src else "no active source: no price, lead time or minimum order quantity"),
        })
    S = {k: sum(r[k] for r in rows) for k in ("usage", "eol", "gross", "buffer", "new", "second_life", "inbound", "staged", "need", "gap",
                                              "recommended", "forecast_recommended", "position_model_net", "rate_per_day")}
    S["usage"], S["rate_per_day"] = round(S["usage"], 1), round(S["rate_per_day"], 3)
    S["lead_time_days"] = max((r["lead_time_days"] for r in rows), default=0)
    order_bys = [r["order_by"] for r in rows if r["order_by"]]
    S["order_by"] = min(order_bys) if order_bys else None
    S["horizon_days"] = horizon
    S["window_days"] = settings.demand_window_days
    S["useful_life_days"] = settings.asset_useful_life_days
    S["factors"] = [
        {"key": "usage", "label": f"Rental starts over the next {horizon} days", "sign": "+", "value": S["usage"],
         "basis": f"measured: rental starts of the last {settings.demand_window_days} days, recency weighted (half-life {settings.demand_halflife_days} days), "
                  f"{S['rate_per_day']:,.2f} a day times {horizon} days"},
        {"key": "eol", "label": "End-of-life replacements in the horizon", "sign": "+", "value": S["eol"],
         "basis": f"measured: devices at customers that pass the useful life of {settings.asset_useful_life_days} days within the horizon"},
        {"key": "buffer", "label": "Buffer against demand variability", "sign": "+", "value": S["buffer"],
         "basis": "measured: safety stock from the spread of demand over the lead time, at the service level of the model's ABC class (planning.inventory_plan)"},
        {"key": "new", "label": "New stock on hand", "sign": "-", "value": S["new"], "basis": "measured: devices in new stock, never rented"},
        {"key": "second_life", "label": "Second-life stock on hand", "sign": "-", "value": S["second_life"],
         "basis": "measured: refurbished devices waiting for a second customer"},
        {"key": "inbound", "label": "On order, not yet received", "sign": "-", "value": S["inbound"], "basis": "measured: outstanding units on open order lines"},
        {"key": "staged", "label": "Already staged for approval", "sign": "-", "value": S["staged"],
         "basis": "measured: included lines of open requisitions, at their current quantity"},
    ]
    S["gap_basis"] = "the sum of the factors, floored at zero"
    S["recommended_basis"] = "the gap rounded up to the preferred source's minimum order quantity, per model"
    S["position_model_basis"] = ("planning.inventory_position, the model the purchasing agent stages from: the same ingredients with the buffer "
                                 "subtracted from the need instead of added; the two differ by twice the buffer wherever there is a gap")
    return rows, S


# ---------------------------------------------------------------------------
# the what-if


def _what_if(db: Session, q: int, rows: list[dict], S: dict, today: date, W: dict, P: dict, head: dict, guard: dict) -> dict:
    """The consequence of ordering ``q`` for this scope, before anything is ordered."""
    weights = [float(r["recommended"]) for r in rows]
    if sum(weights) <= 0:
        weights = [float(r["gross"]) for r in rows]
    parts = _split(q, weights)
    split = []
    total = 0.0
    unpriced = 0
    for r, n in zip(rows, parts):
        cost = (n * r["unit_price"]) if r["unit_price"] is not None else None
        if n and cost is None:
            unpriced += n
        total += cost or 0.0
        split.append({"product_id": r["product_id"], "code": r["code"], "name": r["name"], "units": n, "unit_price": r["unit_price"],
                      "cost": (round(cost, 2) if cost is not None else None), "moq": r["moq"], "lead_time_days": r["lead_time_days"],
                      "moq_short": (n < r["moq"] if n else False), "eta": (today + timedelta(days=r["lead_time_days"])) if n else None})
    lead = max((s["lead_time_days"] for s in split if s["units"]), default=S["lead_time_days"])
    eta = today + timedelta(days=lead)

    # the intake compartment: now, with the order, and on the delivery date at today's measured outflow
    comp = next((c for c in W["compartments"] if c["code"] == INTAKE), None)
    zone = next((z for z in head["zones"] if z["code"] == INTAKE), None)
    on_hand = comp["on_hand"] if comp else 0
    cap = comp["capacity"] if comp else None
    inbound_all = zone["inbound"] if zone else 0
    placed, placed_n = capacity_plan._first_rentals(db, today)
    outflow = (placed / DAYS_PER_MONTH) if placed else None
    due = sum(int(r["outstanding"]) for r in planning.inbound_pipeline(db, as_of=today)
              if r["estimated_delivery_date"] is not None and r["estimated_delivery_date"] <= eta)
    drained = (max(0.0, on_hand - outflow * lead) if outflow is not None else None)
    at_eta = (int(round(drained)) + due + q) if drained is not None else None
    committed_with = on_hand + inbound_all + q
    intake = {
        "code": INTAKE, "name": (comp["name"] if comp else "New stock"), "capacity": cap, "capacity_reason": (comp["capacity_reason"] if comp else None),
        "on_hand": on_hand, "inbound": inbound_all, "committed_now": on_hand + inbound_all, "committed_with": committed_with,
        "over_now": (max(0, on_hand + inbound_all - cap) if cap is not None else None),
        "over_with": (max(0, committed_with - cap) if cap is not None else None),
        "utilisation_with": (round(committed_with / cap, 4) if cap else None),
        "static_basis": "everything lands at once: on hand plus every open inbound line plus this order, against the station's capacity",
        "eta": eta, "lead_time_days": lead,
        "outflow_per_day": (round(outflow, 1) if outflow is not None else None),
        "outflow_basis": (f"measured: {placed_n:,} first rentals started in the last {capacity_plan.MEASURED_MONTHS} full months, per day"
                          if outflow is not None else f"no first rental started in the last {capacity_plan.MEASURED_MONTHS} full months"),
        "stock_at_eta_without": (int(round(drained)) if drained is not None else None), "inbound_due_by_eta": due,
        "stock_at_eta_with": at_eta, "over_at_eta": (max(0, at_eta - cap) if (at_eta is not None and cap is not None) else None),
        "drained_basis": "at today's measured outflow: new stock drains for the lead time, the lines due by the delivery date land, then this order lands",
    }

    # the capacity plan: the station's breach today, and with the order
    prow = next((c for c in P.get("compartments", []) if c["code"] == INTAKE), None)
    fb = P.get("first_breach")
    with_state, with_month = None, None
    if intake["over_at_eta"] is not None:
        if intake["over_at_eta"] > 0:
            with_state, with_month = "breaks_at_delivery", eta.strftime("%Y-%m")
        else:
            with_state, with_month = "fits_at_delivery", None
    plan = {
        "new_stock_state": (prow["breach_state"] if prow else None), "new_stock_month": (prow["breach_month"] if prow else None),
        "new_stock_required_now": (prow["required_now"] if prow else None), "new_stock_reason": (prow["breach_reason"] if prow else None),
        "first_breach": fb,
        "with_order_state": with_state, "with_order_month": with_month,
        "earlier_than_plan": bool(with_state == "breaks_at_delivery" and prow and (prow["breach_month"] is None or with_month < prow["breach_month"])
                                  and prow["breach_state"] not in ("over_today", "now")),
        "basis": "the plan's breach is the month the flow model's required stock crosses the capacity; this order is a stock that lands on one day, "
                 "so it is checked on the delivery date against the same capacity",
    }

    # the whole warehouse, as the guard sees it
    cf_cap = sum(z["capacity"] for z in head["zones"]) or 0
    committed_now = sum(z["used"] for z in head["zones"]) + head["committed_inbound"]
    wh = {"capacity": cf_cap, "committed": committed_now, "committed_pct": (round(committed_now / cf_cap, 4) if cf_cap else None),
          "committed_with": committed_now + q, "committed_pct_with": (round((committed_now + q) / cf_cap, 4) if cf_cap else None)}

    # what it costs
    adder = settings.landed_cost_adder_pct
    landed = total * (1 + adder)
    if unpriced:
        verdict, why = None, f"{unpriced:,} units have no contract price: the order cannot be priced"
    elif landed >= settings.escalate_spend_threshold:
        verdict, why = "escalate", f"at or above the escalation threshold of {settings.escalate_spend_threshold:,.0f} EUR: a human with authority decides"
    elif landed > settings.auto_place_spend_cap:
        verdict, why = "human", f"above the auto-place cap of {settings.auto_place_spend_cap:,.0f} EUR: a human approves"
    else:
        verdict, why = "under_cap", f"under the auto-place cap of {settings.auto_place_spend_cap:,.0f} EUR: the requisition cycle may place it on its own if its confidence clears the bar"
    cost = {"total": round(total, 2), "landed": round(landed, 2), "adder_pct": adder, "adder_basis": "placeholder: duties, freight and insurance as a share of the price (settings.landed_cost_adder_pct)",
            "unpriced_units": unpriced, "auto_place_cap": settings.auto_place_spend_cap, "escalate_threshold": settings.escalate_spend_threshold,
            "verdict": verdict, "reason": why}

    # what it covers
    rate = S["rate_per_day"]
    rec = S["recommended"]
    covers = {
        "vs_gap": q - rec, "recommended": rec,
        "verdict": ("covers" if q >= rec and rec > 0 else "short" if rec > 0 else "no_gap"),
        "days_of_demand": (round(q / rate, 1) if rate else None),
        "cover_days_after": (round((S["new"] + S["second_life"] + S["inbound"] + q) / rate, 1) if rate else None),
        "cover_days_now": (round((S["new"] + S["second_life"] + S["inbound"]) / rate, 1) if rate else None),
        "rate_reason": (None if rate else "no rental start of this scope in the forecast window: days of demand cannot be given"),
    }

    # what would make room
    room = None
    if guard["verdict"] != "ok":
        needed = q - int(guard["allowed"] or 0)
        sell = next((c for c in W["compartments"] if c["code"] == "ST-SELL"), None)
        chain_past = sum(int(c["past_target_units"] or 0) for c in W["compartments"] if c["code"] in _CHAIN_CODES)
        late = [r for r in planning.inbound_pipeline(db, as_of=today) if r["overdue"]]
        levers = []
        if sell and sell["on_hand"]:
            levers.append({"kind": "sell", "label": "Move sellable stock out", "units": sell["on_hand"],
                           "detail": f"{sell['on_hand']:,} devices cleared for resale, median {sell['median_days'] or 0:.0f} days waiting; every sale frees a place"})
        if chain_past:
            levers.append({"kind": "clear_chain", "label": "Clear the return chain's late units", "units": chain_past,
                           "detail": f"{chain_past:,} devices past their target dwell in the return chain; each one that moves on frees a place"})
        if late:
            levers.append({"kind": "late_inbound", "label": "Push out the late inbound lines", "units": sum(int(r["outstanding"]) for r in late),
                           "detail": f"{len(late)} open lines are past their delivery date; their {sum(int(r['outstanding']) for r in late):,} units are counted as committed"})
        levers.append({"kind": "lease", "label": "Lease the places", "units": needed,
                       "detail": f"{needed:,} more places than the warehouse can commit today"})
        room = {"needed": needed, "levers": levers,
                "basis": "measured from the compartments and the open lines; each lever names the units it can free, none is picked"}

    return {"quantity": q, "split": split, "guard": guard, "fits": guard["verdict"] == "ok", "intake": intake, "plan": plan,
            "warehouse": wh, "cost": cost, "covers": covers, "room": room}


# ---------------------------------------------------------------------------
# the mask


def mask(db: Session, *, product_code: Optional[str] = None, manufacturer: Optional[str] = None, family: Optional[str] = None,
         quantity: Optional[int] = None, today: Optional[date] = None) -> dict:
    """The ordering mask for a scope. See the module docstring."""
    today = today or date.today()
    if quantity is not None and quantity < 0:
        raise ValidationError("a quantity cannot be negative")
    if not _is_daas(db):
        return {"scenario": "datacenter", "as_of": today, "scope": None, "demand": None, "owned": None, "inbound": None,
                "recommendation": None, "what_if": None, "timing_ms": {},
                "reason": "the ordering mask reads the device fleet's compartments; this database holds the datacenter operation"}
    t0 = time.perf_counter()
    products, label = _resolve(db, product_code=product_code, manufacturer=manufacturer, family=family)
    pids = [p["product_id"] for p in products]
    stock = _scope_stock(db, pids, today)
    by_product = _scope_stock_by_product(db, pids)
    t1 = time.perf_counter()
    rows, S = _recommend(db, products, today, by_product)
    t2 = time.perf_counter()
    owned = _owned(db, pids, today, stock)
    lines = [r for r in planning.inbound_pipeline(db, as_of=today) if r["product_id"] in set(pids)]
    etas = [r["estimated_delivery_date"] for r in lines if r["estimated_delivery_date"]]
    names = {p["product_id"]: p["name"] for p in products}
    inbound = {
        "units": sum(int(r["outstanding"]) for r in lines), "lines": [{
            "order_number": r["order_number"], "order_status": (r["order_status"].value if hasattr(r["order_status"], "value") else str(r["order_status"])),
            "product_id": r["product_id"], "product": names.get(r["product_id"]), "ordered": r["ordered"], "received": r["received"],
            "outstanding": r["outstanding"], "eta": r["estimated_delivery_date"], "late": r["overdue"],
            "days_to_eta": ((r["estimated_delivery_date"] - today).days if r["estimated_delivery_date"] else None),
        } for r in lines],
        "late_units": sum(int(r["outstanding"]) for r in lines if r["overdue"]), "late_lines": sum(1 for r in lines if r["overdue"]),
        "next_eta": (min(etas) if etas else None), "last_eta": (max(etas) if etas else None),
        "basis": "open order lines of this scope (pending, approved, placed, partially received): outstanding is ordered minus received; late is a delivery date before today",
        "reason": (None if lines else "no open order line for this scope"),
    }
    t3 = time.perf_counter()
    W = warehouse.compartments(db, today=today)
    P = capacity_plan.plan(db, today=today)
    head = planning.storage_headroom(db)
    asked = int(quantity) if quantity else int(S["recommended"])
    guard = planning.check_order_capacity(db, asked, today=today)
    t4 = time.perf_counter()
    rec = {
        **{k: S[k] for k in ("gross", "usage", "eol", "buffer", "new", "second_life", "inbound", "staged", "need", "gap", "recommended",
                             "forecast_recommended", "position_model_net", "rate_per_day", "lead_time_days", "order_by", "horizon_days")},
        "factors": S["factors"], "gap_basis": S["gap_basis"], "recommended_basis": S["recommended_basis"],
        "position_model_basis": S["position_model_basis"],
        "guard": guard, "guard_for": ("what_if" if quantity else "recommendation"),
        # against the warehouse's free-to-order places, whatever quantity the guard was asked about
        "orderable_now": (min(S["recommended"], int(guard["free_to_order"])) if guard["free_to_order"] is not None else S["recommended"]),
        "deferred": (max(0, S["recommended"] - int(guard["free_to_order"])) if guard["free_to_order"] is not None else 0),
        "not_counted": {t["code"]: t["units"] for t in owned["tiers"] if not t["counted"]},
        "cover_days_now": (round((S["new"] + S["second_life"] + S["inbound"]) / S["rate_per_day"], 1) if S["rate_per_day"] else None),
        "products": rows,
    }
    what_if = _what_if(db, int(quantity), rows, S, today, W, P, head, guard) if quantity else None
    t5 = time.perf_counter()
    return {
        "scenario": "daas", "as_of": today,
        "scope": {"product_code": product_code, "manufacturer": manufacturer, "family": family, "label": label, "products": products},
        "demand": {"usage": S["usage"], "eol": S["eol"], "gross": S["gross"], "rate_per_day": S["rate_per_day"], "horizon_days": S["horizon_days"],
                   "window_days": S["window_days"], "useful_life_days": S["useful_life_days"], "lead_time_days": S["lead_time_days"],
                   "order_by": S["order_by"], "method": next((r["method"] for r in rows if r["method"]), None),
                   "basis": "planning.demand_forecast: rental starts of the window, recency weighted, times the horizon, plus end-of-life replacements",
                   "reason": (None if S["gross"] else "no rental start and no device past its useful life for this scope in the window")},
        "owned": owned, "inbound": inbound, "recommendation": rec, "what_if": what_if,
        "reason": None,
        "timing_ms": {"scope": round((t1 - t0) * 1000), "demand": round((t2 - t1) * 1000), "owned": round((t3 - t2) * 1000),
                      "guard_and_plan": round((t4 - t3) * 1000), "what_if": round((t5 - t4) * 1000), "total": round((t5 - t0) * 1000)},
    }

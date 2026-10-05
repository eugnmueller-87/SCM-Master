"""The fleet in the DaaS scenario: where every device is, and what comes back when.

Reads only ``asset`` and ``rental_contract``. The scenario is detected, never
configured: a database with rented devices is a DaaS fleet, one without is the
datacenter operation, and the frontend asks this module which one it is looking at.

The return calendar is built from the planned end of every running contract. What
happens to a return is a rule (grade, cycle), applied here as an expectation over
the grade mix, and labelled as such in the response.

**Everything aggregates in the database.** At 400,000 devices and 490,000 contracts,
pulling rows into Python to count them is the difference between a page that answers
at once and one that times out. Two things carry the weight: counts come back as
scalars, and where a median is needed the query groups by the date itself — dates are
discrete, so the result is an exact histogram of a few hundred rows rather than a
hundred thousand.
"""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import date, timedelta
from typing import Optional

from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session

from app.models.catalog import Organization, Product, ProductSupplier
from app.models.flow import WAREHOUSE_STATUSES, Asset, AssetStatus
from app.models.rental import ContractStatus, RentalContract

MDM_RELEASE_SLA_DAYS = 21       # placeholder: the old customer should release within this
SELLABLE_AGING_DAYS = 90
GROWTH_MONTHS = 24              # how far back the fleet-growth curve reaches
# expected next step of a return, by cycle, over the grade mix A .35 B .40 C .20 D .05 (placeholder, Head of Recommerce):
# after cycle 1: A/B -> second rental, C -> repair first, D -> sale; 4 % recycled. After cycle 2: sale, 4 % recycled.
NEXT_STEP_SHARE = {
    1: {"second_rental": 0.72, "repair": 0.19, "sale": 0.05, "recycling": 0.04},
    2: {"second_rental": 0.0, "repair": 0.0, "sale": 0.96, "recycling": 0.04},
}
STATION_ORDER = [AssetStatus.RETURNED, AssetStatus.MDM_RELEASE, AssetStatus.WIPE_GRADING, AssetStatus.REPAIR,
                 AssetStatus.REFURB, AssetStatus.READY_SECOND, AssetStatus.SELLABLE, AssetStatus.SWAP_BUFFER,
                 AssetStatus.IN_STORAGE, AssetStatus.RECEIVED]
STATION_LABEL = {
    AssetStatus.RETURNED: "Returns intake", AssetStatus.MDM_RELEASE: "MDM release hold", AssetStatus.WIPE_GRADING: "Wipe and grading",
    AssetStatus.REPAIR: "Repair", AssetStatus.REFURB: "Refurbishment", AssetStatus.READY_SECOND: "Second-life stock",
    AssetStatus.SELLABLE: "Sellable stock",
    AssetStatus.SWAP_BUFFER: "Swap buffer", AssetStatus.IN_STORAGE: "New stock", AssetStatus.RECEIVED: "Just received",
}


def scenario(db: Session) -> str:
    n = db.scalar(select(func.count(Asset.id)).where(Asset.status == AssetStatus.RENTED)) or 0
    return "daas" if n > 0 else "datacenter"


def _status_counts(db: Session) -> dict[AssetStatus, int]:
    rows = db.execute(select(Asset.status, func.count(Asset.id)).group_by(Asset.status)).all()
    return {s: int(n) for s, n in rows}


def _as_date(v) -> date:
    """SQLite hands a date back as text on some paths; Postgres gives a date."""
    return v if isinstance(v, date) else date.fromisoformat(str(v)[:10])


def _median_from_histogram(hist: dict[int, int]) -> Optional[float]:
    """Exact median of a set of day counts given as {days: how many}."""
    total = sum(hist.values())
    if not total:
        return None
    half = total / 2.0
    acc = 0
    last = 0
    for days in sorted(hist):
        acc += hist[days]
        last = days
        if acc >= half:
            return float(days)
    return float(last)


def _dwell_histogram(db: Session, today: date) -> dict[AssetStatus, dict[int, int]]:
    """Days in the current station, per station, as {status: {days: count}}.

    One grouped query for every station at once. Because ``status_since`` is a date,
    grouping by it yields a few hundred rows however large the fleet is.
    """
    rows = db.execute(
        select(Asset.status, Asset.status_since, func.count(Asset.id))
        .where(Asset.status.in_(tuple(STATION_ORDER)), Asset.status_since.is_not(None))
        .group_by(Asset.status, Asset.status_since)
    ).all()
    out: dict[AssetStatus, dict[int, int]] = defaultdict(dict)
    for status, since, n in rows:
        days = (today - _as_date(since)).days
        out[status][days] = out[status].get(days, 0) + int(n)
    return out


def _running_ends(db: Session, today: date) -> dict[str, int]:
    """How many running contracts end in each window, and how many should have ended."""
    def n(*crit) -> int:
        return int(db.scalar(select(func.count(RentalContract.id))
                             .where(RentalContract.status == ContractStatus.RUNNING, *crit)) or 0)

    return {
        "due_30": n(RentalContract.planned_end >= today, RentalContract.planned_end <= today + timedelta(days=30)),
        "due_90": n(RentalContract.planned_end >= today, RentalContract.planned_end <= today + timedelta(days=90)),
        "due_365": n(RentalContract.planned_end >= today, RentalContract.planned_end <= today + timedelta(days=365)),
        "overdue": n(RentalContract.planned_end < today),
    }


def growth(db: Session, *, today: Optional[date] = None, months: int = GROWTH_MONTHS) -> dict:
    """How fast the fleet is growing, read from the contracts themselves.

    ``by_month`` counts first rentals started — a device entering service for the first
    time. ``rented_12m_ago`` counts the contracts that were running on that day, which is
    the honest comparison for "how much bigger are we than a year ago", because it
    includes the devices that have come back since.
    """
    today = today or date.today()
    # exactly ``months`` calendar months, the current one last
    idx = today.year * 12 + (today.month - 1) - (max(1, months) - 1)
    since = date(idx // 12, idx % 12 + 1, 1)
    # Group by the date column itself, not by an expression over it: the database can then
    # answer from the index alone (a month is a few hundred dates, whatever the fleet size),
    # and there is no dialect difference in how a month is extracted.
    rows = db.execute(
        select(RentalContract.start_date, func.count(RentalContract.id))
        .where(RentalContract.cycle_no == 1, RentalContract.start_date >= since)
        .group_by(RentalContract.start_date)
    ).all()
    by_month: dict[str, int] = {}
    for d, n in rows:
        key = _as_date(d).strftime("%Y-%m")
        by_month[key] = by_month.get(key, 0) + int(n)
    series = []
    y, m = since.year, since.month
    while (y, m) <= (today.year, today.month):
        key = f"{y:04d}-{m:02d}"
        series.append({"month": key, "first_rentals": by_month.get(key, 0)})
        m += 1
        if m == 13:
            y, m = y + 1, 1

    a_year_ago = today - timedelta(days=365)
    then = int(db.scalar(
        select(func.count(RentalContract.id)).where(
            RentalContract.start_date <= a_year_ago,
            or_(RentalContract.actual_end.is_(None), RentalContract.actual_end > a_year_ago))) or 0)
    now = int(db.scalar(select(func.count(RentalContract.id)).where(RentalContract.status == ContractStatus.RUNNING)) or 0)
    return {
        "by_month": series,
        "rented_now": now,
        "rented_12m_ago": then,
        "growth_12m_pct": round((now - then) / then * 100, 1) if then else None,
        "added_12m": now - then,
    }


def summary(db: Session, *, today: Optional[date] = None) -> dict:
    today = today or date.today()
    counts = _status_counts(db)                     # one grouped read answers the scenario too
    rented = counts.get(AssetStatus.RENTED, 0)
    cycle2 = db.scalar(select(func.count(Asset.id)).where(Asset.status == AssetStatus.RENTED, Asset.cycle_no >= 2)) or 0
    warehouse = sum(counts.get(s, 0) for s in WAREHOUSE_STATUSES)

    # stations with dwell, from one grouped query
    hist = _dwell_histogram(db, today)
    stations = []
    for st in STATION_ORDER:
        n = counts.get(st, 0)
        if n == 0 and st in (AssetStatus.RECEIVED,):
            continue
        h = hist.get(st, {})
        stations.append({
            "status": st.value, "label": STATION_LABEL[st], "count": n,
            "median_days": _median_from_histogram(h),
            "over_90_days": sum(c for d, c in h.items() if d > SELLABLE_AGING_DAYS),
            "over_sla": (sum(c for d, c in h.items() if d > MDM_RELEASE_SLA_DAYS) if st == AssetStatus.MDM_RELEASE else None),
        })

    due = _running_ends(db, today)

    # resale, last 12 months
    since = today - timedelta(days=365)
    sold = db.execute(select(func.count(Asset.id), func.coalesce(func.sum(Asset.sale_price), 0))
                      .where(Asset.status == AssetStatus.SOLD, Asset.sold_date >= since)).one()
    recycled = db.scalar(select(func.count(Asset.id)).where(Asset.status == AssetStatus.RECYCLED, Asset.sold_date >= since)) or 0
    ended_90 = db.execute(select(RentalContract.end_reason, func.count())
                          .where(RentalContract.status == ContractStatus.ENDED, RentalContract.actual_end >= today - timedelta(days=90))
                          .group_by(RentalContract.end_reason)).all()
    ended = {r or "planned": int(n) for r, n in ended_90}

    return {
        "scenario": "daas" if rented else "datacenter", "as_of": today,
        "total": sum(counts.values()),
        "rented": rented, "rented_cycle2": cycle2, "rented_cycle1": rented - cycle2,
        "warehouse": warehouse, "stations": stations,
        "sold_12m": int(sold[0]), "sold_12m_eur": float(sold[1] or 0), "recycled_12m": int(recycled),
        "returns_due_30d": due["due_30"], "returns_due_90d": due["due_90"], "returns_due_365d": due["due_365"],
        "returns_overdue": due["overdue"],
        "ended_last_90d": ended,
        "growth": growth(db, today=today),
        "by_status": {s.value: n for s, n in counts.items()},
    }


def return_calendar(db: Session, *, today: Optional[date] = None, months: int = 24) -> list[dict]:
    today = today or date.today()
    start = date(today.year, today.month, 1)
    end_y, end_m = divmod(start.month - 1 + months, 12)
    horizon = date(start.year + end_y, end_m + 1, 1)

    # One grouped query: month x cycle x device family, so the answer is a few hundred
    # rows however many contracts are running.
    cyc = case((RentalContract.cycle_no >= 2, 2), else_=1).label("cyc")
    rows = db.execute(
        select(RentalContract.planned_end, cyc, Product.category, func.count(RentalContract.id))
        .join(Asset, Asset.id == RentalContract.asset_id)
        .join(Product, Product.id == Asset.product_id)
        .where(RentalContract.status == ContractStatus.RUNNING,
               RentalContract.planned_end >= start, RentalContract.planned_end < horizon)
        .group_by(RentalContract.planned_end, cyc, Product.category)
    ).all()

    out: dict[str, dict] = {}
    for i in range(months):
        y, m = divmod(start.month - 1 + i, 12)
        key = f"{start.year + y:04d}-{m + 1:02d}"
        out[key] = {"month": key, "from_cycle1": 0, "from_cycle2": 0, "total": 0, "second_rental": 0.0, "repair": 0.0,
                    "sale": 0.0, "recycling": 0.0, "by_family": defaultdict(int)}
    for end, cycle, family, n in rows:
        o = out.get(_as_date(end).strftime("%Y-%m"))
        if o is None:
            continue
        n = int(n)
        cycle = int(cycle)
        o["from_cycle2" if cycle >= 2 else "from_cycle1"] += n
        o["total"] += n
        for k, s in NEXT_STEP_SHARE[2 if cycle >= 2 else 1].items():
            o[k] += s * n
        o["by_family"][family or "?"] += n
    result = []
    for o in out.values():
        o["by_family"] = dict(o["by_family"])
        for k in ("second_rental", "repair", "sale", "recycling"):
            o[k] = int(round(o[k]))
        result.append(o)
    return result


def upcoming_returns(db: Session, *, today: Optional[date] = None, days: int = 30, limit: int = 200) -> list[dict]:
    today = today or date.today()
    stmt = (select(RentalContract, Asset, Product, Organization)
            .join(Asset, Asset.id == RentalContract.asset_id)
            .join(Product, Product.id == Asset.product_id)
            .join(Organization, Organization.id == RentalContract.customer_id)
            .where(RentalContract.status == ContractStatus.RUNNING, RentalContract.planned_end <= today + timedelta(days=days))
            .order_by(RentalContract.planned_end).limit(limit))
    out = []
    for c, a, p, org in db.execute(stmt).all():
        out.append({"contract_id": c.id, "asset_id": a.id, "serial_number": a.serial_number, "product": p.name, "family": p.category,
                    "customer": org.name, "cycle_no": c.cycle_no, "start_date": c.start_date, "term_months": c.term_months,
                    "planned_end": c.planned_end, "overdue": c.planned_end < today,
                    "age_months": round((today - a.received_date).days / 30.4375, 1) if a.received_date else None,
                    "expected_next": ("sale" if c.cycle_no >= 2 else "second rental, grade permitting")})
    return out


# ---------------------------------------------------------------------------
# What the fleet is made of: manufacturer, class, model
# ---------------------------------------------------------------------------
# Added 04.10.2026. The console could say how many devices are where, never WHICH
# devices. "How many Apple, how many Samsung" had no answer, and a fleet owner
# negotiates per manufacturer, plans a launch per model and writes off per class.
#
# WHY IT IS BUILT THIS WAY
# No join from asset to manufacturer. A product carries two sources for the volume
# models (Apple and Samsung phones have a second reseller), so joining asset to
# product_supplier would count those devices twice, silently and only for the two
# manufacturers that matter most. Instead: one small read builds product -> maker
# (a few dozen rows), one grouped read counts assets per product and status (a few
# hundred rows), and the tree is folded in Python. Nothing fans out, nothing is
# pulled row by row, and at 400,000 devices both reads stay scalar.
#
# A product with no manufacturer on file lands under UNKNOWN_MAKER rather than
# being dropped. A fleet that does not add up is worse than one with an honest
# unknown in it.

UNKNOWN_MAKER = "not on file"

# Everything in the warehouse, in the order a device travels, so a model row reads
# as a road and not as an alphabet.
BREAKDOWN_STATUSES = [
    # the rental road
    AssetStatus.RENTED, AssetStatus.IN_STORAGE, AssetStatus.RETURNED, AssetStatus.MDM_RELEASE,
    AssetStatus.WIPE_GRADING, AssetStatus.REPAIR, AssetStatus.REFURB, AssetStatus.READY_SECOND,
    AssetStatus.SELLABLE, AssetStatus.SWAP_BUFFER, AssetStatus.RECEIVED,
    # the datacenter scenario still exists and seed_reset still detects it. Without
    # these two a datacenter database would carry devices in `total` that appear in
    # no chip at all: present in the sum, invisible in the breakdown.
    AssetStatus.DEPLOYED, AssetStatus.MAINTENANCE,
    # left the fleet
    AssetStatus.SOLD, AssetStatus.RECYCLED, AssetStatus.DISPOSED, AssetStatus.DECOMMISSIONED,
]

_RRP = re.compile(r"Launch RRP\s+([0-9]+(?:[.,][0-9]+)?)\s*EUR")
_LAUNCH = re.compile(r"(\d{4}-\d{2}-\d{2})")


def _catalogue_facts(description: Optional[str]) -> dict:
    """Launch price and launch day, read back out of the product description the
    seed wrote from the catalogue, plus the source URL that came with it. Parsed
    tolerantly: a description in another shape yields None, never a guess, and the
    raw text travels along so a reader can always see where the number came from."""
    text = description or ""
    rrp = _RRP.search(text)
    launch = _LAUNCH.search(text)
    url = text.split("Source:", 1)[1].strip() if "Source:" in text else ""
    return {
        "launch_rrp_eur": float(rrp.group(1).replace(",", ".")) if rrp else None,
        "launch_date": launch.group(1) if launch else None,
        "source": url,
        "provenance": text,
    }


def _maker_by_product(db: Session) -> dict[str, str]:
    """product id -> manufacturer name. DISTINCT, because a product may carry
    several sources and they all name the same maker; without it the count of a
    dual-sourced model would double."""
    rows = db.execute(
        select(ProductSupplier.product_id, Organization.name)
        .join(Organization, Organization.id == ProductSupplier.manufacturer_id)
        .distinct()
    ).all()
    return {str(pid): str(name) for pid, name in rows}


def breakdown(db: Session, *, today: Optional[date] = None) -> dict:
    """The fleet by manufacturer, class and model, each with its per-status counts.

    The tree is returned whole and sorted by size, so the client can drill without
    another round trip: a manufacturer opens its classes, a class opens its models.
    `active` excludes what has left the fleet (sold, recycled); `total` does not, so
    both questions are answerable from the same row without a second call.
    """
    today = today or date.today()
    makers = _maker_by_product(db)

    products: dict[str, tuple] = {}
    rows = db.execute(
        select(Product.id, Product.product_code, Product.name, Product.category, Product.description)
    ).all()
    for r in rows:
        products[str(r[0])] = r

    counts = db.execute(
        select(Asset.product_id, Asset.status, func.count(Asset.id))
        .group_by(Asset.product_id, Asset.status)
    ).all()

    gone = {AssetStatus.SOLD, AssetStatus.RECYCLED, AssetStatus.DISPOSED, AssetStatus.DECOMMISSIONED}

    def blank() -> dict:
        return {"total": 0, "active": 0, "by_status": defaultdict(int)}

    tree: dict[str, dict] = {}
    model_of: dict[tuple, dict] = {}
    for product_id, status, n in counts:
        key = str(product_id)
        prod = products.get(key)
        if prod is None:
            continue
        _pid, code, name, family, description = prod
        maker = makers.get(key, UNKNOWN_MAKER)
        family = family or "not classified"
        n = int(n)

        maker_node = tree.setdefault(maker, dict(blank(), name=maker, families={}))
        fam_node = maker_node["families"].setdefault(family, dict(blank(), name=family, models={}))
        mk = (maker, family, code)
        model_node = model_of.get(mk)
        if model_node is None:
            model_node = dict(blank(), code=code, name=name, **_catalogue_facts(description))
            model_of[mk] = model_node
            fam_node["models"][code] = model_node

        for node in (maker_node, fam_node, model_node):
            node["total"] += n
            node["by_status"][status.value if hasattr(status, "value") else str(status)] += n
            if status not in gone:
                node["active"] += n

    def finish(node: dict) -> dict:
        node["by_status"] = {s.value: node["by_status"].get(s.value, 0) for s in BREAKDOWN_STATUSES
                             if node["by_status"].get(s.value, 0)}
        return node

    makers_out = []
    for maker_node in tree.values():
        fams = []
        for fam_node in maker_node["families"].values():
            models = sorted(fam_node["models"].values(), key=lambda m: (-m["active"], -m["total"]))
            fam_node["models"] = [finish(m) for m in models]
            fams.append(finish(fam_node))
        maker_node["families"] = sorted(fams, key=lambda f: (-f["active"], -f["total"]))
        makers_out.append(finish(maker_node))
    # Nach der LEBENDEN Flotte sortiert: was verkauft ist, verwaltet niemand mehr.
    makers_out.sort(key=lambda m: (-m["active"], -m["total"]))

    total = sum(m["total"] for m in makers_out)
    active = sum(m["active"] for m in makers_out)
    for m in makers_out:
        # Precomputed so no client has to divide, and so two screens cannot
        # disagree about what the share is a share OF: the active fleet.
        m["share_active"] = round(100.0 * m["active"] / active, 1) if active else 0.0

    return {
        "as_of": today,
        "total": total,
        "active": active,
        "statuses": [s.value for s in BREAKDOWN_STATUSES],
        "manufacturers": makers_out,
    }

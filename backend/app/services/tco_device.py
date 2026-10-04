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

**Every cost a device causes between two rentals is its own line (04.10.2026).** Shipping
to the user and the return trip, the intake, test and grading of every device that comes
back with its wipe are separate components, because they are what an
operations lead negotiates one by one and what scales with every rental cycle. Two layers
became measurable from the asset's own dates: *financing*, the cost of capital on what the
device was bought for over every day it is owned (receipt to sale, recycling or today),
which replaces the stock-only capital charge; and *days off rent*, every day owned and not
out on rent, which gives the finished lives the warehouse figure they lacked. Beside the
cost, the rent every contract carries: revenue, rent per device-month and the margin per
device and per month, so a model that costs more than it earns shows it. One device is a
population of one: ``device()`` runs a single serial through the same ``_figures`` as the
fleet, so a serial's TCO and the model's average can never be computed two ways.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, fields
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

from sqlalchemy import Date, case, func, literal, select
from sqlalchemy.orm import Session

from app.models.catalog import Product
from app.models.flow import WAREHOUSE_STATUSES, Asset, AssetStatus
from app.models.procurement import OrderItem
from app.models.rental import RentalContract
from app.models.tco import ServiceEvent, ServiceKind
from app.services import kpis
from app.services.exceptions import NotFoundError

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
    # Where the number comes from when it is not ours to set: a public price list or tariff,
    # with what it covers. None means a placeholder until the owner sets it.
    source: Optional[str] = None

    @property
    def placeholder(self) -> bool:
        return self.source is None

    def for_family(self, family: Optional[str]) -> Optional[float]:
        if self.by_family is not None:
            return self.by_family.get(family or "")
        return self.value


# EUR net. Three origins, each written at the value (researched 04.10.2026): a public price
# with its source; a figure derived from public prices, the derivation written out; or a
# placeholder with the role that sets it, only where nothing is public. Every public price
# here is a list price for a small sender and overstates what a fleet of this size pays; it
# is the upper bound until the owner puts the negotiated rate in its place.
GBP_PER_EUR = 0.85033   # ECB euro reference rate, 2 Oct 2026
_DHL_SERVICES = "https://www.dhl.de/dam/jcr:03e8bbfe-7e82-450c-a5be-1132bccfb798/dhl-paket-preise-fuer-services-102025.pdf"
_CENTERPRISE = ("https://assets.applytosupply.digitalmarketplace.service.gov.uk/g-cloud-14/documents/92520/"
                "351928326234533-pricing-document-2022-05-18-1013.pdf")
_PARCEL = {"Smartphone": 4.24, "Tablet": 4.24, "Laptop": 7.69}

RATES: dict[str, Rate] = {r.id: r for r in (
    Rate("inbound", "Inbound logistics and handling", "per device", "Head of Supply",
         "freight, import handling and receiving, once per device delivered",
         by_family={"Smartphone": 4.0, "Tablet": 5.0, "Laptop": 9.0}),
    # Until 04.10.2026 one placeholder of 15 (25 for a laptop) carried enrolment and the parcel
    # together. The parcel is now its own sourced line; enrolment is that placeholder less the
    # parcel, so splitting the line did not move the total.
    Rate("enrolment", "Enrolment and staging", "per rental start", "Head of Operations",
         "MDM enrolment, configuration and packing, once per rental: the earlier combined placeholder "
         "(15, a laptop 25) less the parcel, which is now its own line",
         by_family={f: round(old - _PARCEL[f], 2) for f, old in (("Smartphone", 15.0), ("Tablet", 15.0), ("Laptop", 25.0))}),
    Rate("outbound", "Shipping to the user", "per rental start", "Head of Operations",
         "the parcel to the user's address, once per rental start",
         by_family=dict(_PARCEL),
         source="Smartphone and tablet: DHL Paket for business customers, 1 kg, from 4.24 EUR net incl. toll, at an example "
                "3,000 parcels a year plus an unpublished monthly fee (https://www.dhl.de/de/geschaeftskunden/paket/kunde-werden.html). "
                "Laptop: DHL publishes no business price per weight, so the private-customer price for 5 kg, 7.69 EUR, stands "
                "as the upper bound (https://www.dhl.de/de/privatkunden/pakete-versenden/deutschlandweit-versenden/preise-national.html). "
                "Both retrieved 04.10.2026."),
    Rate("software", "Software and management", "per device-month", "Head of IT",
         "the MDM licence per month in service; support tooling is not in this rate", value=round(25.61 / 12, 2),
         source="Microsoft Intune Plan 1 device licence, 25.61 EUR net a year on a one-year CSP term, divided by 12 "
                "(reseller list price, https://www.software-express.de/hersteller/microsoft/intune/, page dated 06.03.2026, "
                "retrieved 04.10.2026)"),
    Rate("support", "Support", "per device-month", "Head of Customer Success",
         "first-level support, per month in service", value=1.5),
    Rate("swap", "Defect and swap handling", "per event", "Head of Service Operations",
         "return of the defect device and shipping of the replacement, per contract ended by a defect or a swap", value=35.0),
    Rate("return_ship", "Return shipping", "per rental ended on schedule or early", "Head of Operations",
         "DHL collects the device from the user with the label; a return after a defect or a swap is in the swap handling",
         value=round(6.40 + 1.99, 2),
         source="DHL Retoure mit Abholung und Label, 6.40 EUR net incl. toll and CO2 surcharge, plus the pickup order with "
                f"label print booked online, 1.99 EUR, which DHL bills on top (footnote 5), {_DHL_SERVICES}. Price list "
                "Stand 10/2025, before the price rise of 01.01.2026; no weight class. Retrieved 04.10.2026."),
    Rate("intake", "Return intake, test, grade and wipe", "per device returned", "Head of Operations",
         "receiving, audit, Blancco erasure or factory reset, function test and visual grade of every device that comes back",
         by_family={"Smartphone": round(6.00 / GBP_PER_EUR, 2), "Tablet": round(6.00 / GBP_PER_EUR, 2),
                    "Laptop": round(8.00 / GBP_PER_EUR, 2)},
         source="Centerprise asset lifetime management, per unit for audit, Blancco/factory reset, de-tag, post test and "
                f"visual grade: phones and PDAs 6.00 GBP, laptop 8.00 GBP ({_CENTERPRISE}), a UK public-sector framework "
                "price (G-Cloud 14), converted at the ECB reference rate of 2 Oct 2026 (1 EUR = 0.85033 GBP). A tablet "
                "takes the phone price; the list has no tablet line. Retrieved 04.10.2026."),
    Rate("warehouse_day", "Warehousing", "per device-day", "Head of Operations",
         "space and handling, per device and day owned and not out on rent", value=0.04),
    Rate("capital", "Cost of capital", "per EUR of acquisition value and year", "CFO",
         "the KPI tab's carrying-cost rate, on what the device was bought for, over every day it is owned: from receipt to "
         "sale, recycling or today. No device-as-a-service financier publishes its rate; the public anchors are the ECB "
         "main refinancing rate, 2.65 % from 16.09.2026 (https://www.ecb.europa.eu/stats/policy_and_exchange_rates/"
         "key_ecb_interest_rates/html/index.en.html), euro-area new corporate loans over 1m EUR, 3.34 % in August 2026 "
         "(https://www.ecb.europa.eu/press/stats/mfi/html/ecb.mir2610~8e4898ad10.en.html), and the 4.625 % coupon of "
         "Grenke's 2031 bond (https://grenke.com/investor-relations/debt-capital/issued-bonds), all retrieved 04.10.2026",
         value=kpis.CARRYING_COST_RATE_PA),
    Rate("recycling", "Recycling", "per device", "Head of Recommerce",
         "WEEE handling and data destruction of a device that is not sold", value=6.0),
)}

# The layers, in the order of a device's life. The last is the credit.
LAYERS = (
    ("acquisition", "Acquisition", "what the device was bought for: the unit price of the order line it traces to"),
    ("inbound", "Inbound logistics", "freight, import handling and receiving, per device delivered"),
    ("enrolment", "Enrolment and staging", "MDM enrolment, configuration and packing, once per rental start"),
    ("outbound", "Shipping to the user", "the parcel to the user, once per rental start"),
    ("software", "Software and management", "MDM licence and support tooling, per month in service"),
    ("support", "Support and damage", "first-level support per month in service, plus every contract ended by a defect or a swap"),
    ("returns", "Returns and intake", "the return trip, then intake, test, grade and wipe of every device that comes back"),
    ("service", "Repair and refurbishment", "the partner's invoice per repair and per refurbishment, from the service events"),
    ("warehouse", "Days off rent", "every day the device is owned and not out on rent: on the shelf, in transit or on the bench"),
    ("financing", "Financing", "the cost of the money in the device: what it was bought for, over every day it is owned"),
    ("eol", "End of life", "resale proceeds as a credit, from the sold serials; recycling cost where there are none"),
)

COHORTS = {
    "finished": ("Finished lives", "Devices sold or recycled: acquisition, every rental, every repair and the resale are all known. "
                 "The whole-life cost per device and per month in service, the number a rental price has to cover."),
    "fleet": ("Whole fleet to date", "Every serial with what it has cost so far. Right for the running layers; acquisition is "
              "paid on day one and most of a young fleet's resale credit has not arrived yet."),
}

BASIS = ("Quantities are measured: prices from the order lines, months in service and rent from the rental contracts, "
         "returns from how each contract ended, repairs and refurbishments from the service events, days owned from the "
         "receipt and sale dates, proceeds from the sold serials. A rate is a public list price with its source, or a "
         "placeholder with the role that sets it. Everything is computed in the database from every serial.")


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
    returns: int = 0                    # contracts ended: every one is a device coming back
    returns_plain: int = 0               # of those, ended on schedule or early (a defect or swap is in the swap rate)
    owned_dated: int = 0                 # devices with a receipt date
    owned_days: int = 0                  # device-days owned: receipt to sale, recycling or today
    owned_value_days: Decimal = _ZERO    # EUR-days: days owned times the order-line price, for the priced ones
    owned_unpriced: int = 0              # dated devices without an order-line price
    rent_contracts: int = 0              # contracts that carry a rent
    rent_days: int = 0                   # the days those contracts ran
    rent_eur_days: Decimal = _ZERO       # rent per month times the days it ran; revenue is this over DAYS_PER_MONTH
    inbound: Decimal = _ZERO             # the per-class rates, applied per model and summed upward
    enrolment: Decimal = _ZERO
    outbound: Decimal = _ZERO
    return_ship: Decimal = _ZERO
    intake: Decimal = _ZERO

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


def _add_return(q: _Q, reason, n: int) -> None:
    """``n`` contracts ended: every one brings a device back; a defect or a swap travels on the swap rate."""
    q.returns += n
    if reason in ("defect", "swap"):
        q.swap_events += n
    else:
        q.returns_plain += n


def _add_ends(acc: dict[str, _Q], rows) -> None:
    for pid, cycle, end, reason, n in rows:
        q, n, o = acc[pid], int(n), _as_date(end).toordinal()
        q.ended += n
        q.end_ord += n * o
        if int(cycle) >= 2:
            q.ended_cycle2 += n
            q.end_ord_cycle2 += n * o
        _add_return(q, reason, n)


def _add_joined(acc: dict[str, _Q], rows) -> None:
    """Contracts already summed in SQL: (model, cycle, end reason, how many, device-days, how many
    ended, how many carry a rent, rent times days, the days of those with a rent)."""
    for pid, cycle, reason, n, days, n_ended, n_rent, rent_eur_days, rent_days in rows:
        q, n, days = acc[pid], int(n), max(0, int(round(float(days or 0))))
        q.contracts += n
        q.direct_days += days
        if int(cycle) >= 2:
            q.contracts_cycle2 += n
            q.direct_days_cycle2 += days
        _add_return(q, reason, int(n_ended))
        _add_rent(q, n_rent, rent_eur_days, rent_days)


def _add_rent(q: _Q, n, rent_eur_days, days) -> None:
    q.rent_contracts += int(n or 0)
    q.rent_eur_days += max(_ZERO, _money(rent_eur_days))
    q.rent_days += max(0, int(round(float(days or 0))))


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

    # Days owned, summed per (status, order line) in SQL: receipt to sale, recycling or today.
    # One pass over the asset table, with the day count done there because every row is
    # visited anyway; the line gives the price, so the capital is price times days. They feed
    # two layers: financing (the money in the device for as long as it is ours) and the days
    # off rent (days owned minus days on rent). A receipt date in the future would count
    # negative, so each group is floored at zero; a device without one is counted apart.
    today_lit = literal(today, Date)
    owned_end = func.coalesce(Asset.sold_date, Asset.decommissioned_date, today_lit)
    owned_cols = (func.count(Asset.received_date), func.sum(_days_between(db, owned_end, Asset.received_date)))

    def add_owned(status, pid, price, n_dated, days):
        n_dated, days = int(n_dated), max(0, int(round(float(days or 0))))
        for q in ((fleet[pid], fin[pid]) if status in FINISHED_STATUSES else (fleet[pid],)):
            q.owned_dated += n_dated
            q.owned_days += days
            if price is not None:
                q.owned_value_days += price * days
            else:
                q.owned_unpriced += n_dated

    for status, lid, n_dated, days in db.execute(
            select(Asset.status, Asset.source_order_item_id, *owned_cols)
            .where(Asset.source_order_item_id.is_not(None))
            .group_by(Asset.status, Asset.source_order_item_id)).all():
        pid, price = lines.get(lid, (None, None))
        if pid is not None:
            add_owned(status, pid, price, n_dated, days)
    for status, pid, n_dated, days in db.execute(
            select(Asset.status, Asset.product_id, *owned_cols)
            .where(Asset.source_order_item_id.is_(None))
            .group_by(Asset.status, Asset.product_id)).all():
        add_owned(status, pid, None, n_dated, days)

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
    # Rent: what each contract carries per month, times the days it ran (to today for a running
    # one). The rent is not in any index, so this is one pass over the contracts, grouped by
    # model; a contract without its model is joined to its device, as above.
    run_days = _days_between(db, func.coalesce(rc.actual_end, today_lit), rc.start_date)
    rent_cols = (func.count(rc.rent_eur_month),
                 func.coalesce(func.sum(rc.rent_eur_month * run_days), 0),
                 func.coalesce(func.sum(case((rc.rent_eur_month.is_not(None), run_days), else_=0)), 0))
    for pid, n_rent, rent_eur_days, rent_days in db.execute(
            select(rc.product_id, *rent_cols).where(rc.product_id.is_not(None)).group_by(rc.product_id)).all():
        _add_rent(fleet[pid], n_rent, rent_eur_days, rent_days)
    for pid, n_rent, rent_eur_days, rent_days in db.execute(
            select(Asset.product_id, *rent_cols).select_from(rc).join(Asset, Asset.id == rc.asset_id)
            .where(rc.product_id.is_(None)).group_by(Asset.product_id)).all():
        _add_rent(fleet[pid], n_rent, rent_eur_days, rent_days)
    # the finished cohort's contracts: one join driven from its devices, which are few, with
    # the days, the returns and the rent summed in SQL; a contract still running on a finished
    # device would end today
    _add_joined(fin, db.execute(select(Asset.product_id, rc.cycle_no, rc.end_reason, func.count(), func.sum(run_days),
                                       func.count(rc.actual_end), *rent_cols)
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
            _apply_family_rates(q, products.get(pid, (None, None, None))[2])
    return products, lines, fleet, fin


def _apply_family_rates(q: _Q, family: Optional[str]) -> None:
    """The rates that differ by device class, applied to one model's (or one device's) counts."""
    rate = lambda rid: RATES[rid].for_family(family) or 0  # noqa: E731 - a lookup, not a function
    q.inbound = _money(rate("inbound") * q.devices)
    q.enrolment = _money(rate("enrolment") * q.contracts)
    q.outbound = _money(rate("outbound") * q.contracts)
    q.return_ship = _money(rate("return_ship") * q.returns_plain)
    q.intake = _money(rate("intake") * q.returns)


# ---------------------------------------------------------------------------
# from quantities to figures


def _component(cid: str, label: str, *, quantity, unit: str, rate: Optional[Rate], rate_value, total,
               measured: bool, reason: Optional[str] = None, note: Optional[str] = None) -> dict:
    if measured:
        basis = "measured"
    elif rate is not None and rate.source:
        basis = "quantity measured, rate from a public source"
    else:
        basis = "quantity measured, rate placeholder"
    return {
        "id": cid, "label": label, "basis": basis,
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
    """The layers of one population, the totals over them, and what its contracts earned."""
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
    outbound = [_component("outbound", R["outbound"].label, quantity=q.contracts, unit="rental starts", rate=R["outbound"],
                           rate_value=(q.outbound / q.contracts if q.contracts else R["outbound"].for_family(None)),
                           total=q.outbound, measured=False)]
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
    # Every ended contract brings a device back: it is received, tested, graded and wiped. The
    # trip itself is its own rate, except after a defect or a swap, where the swap rate already
    # carries the return and the replacement.
    returns = [
        _component("return_ship", R["return_ship"].label, quantity=q.returns_plain, unit="rentals ended on schedule or early",
                   rate=R["return_ship"],
                   rate_value=(q.return_ship / q.returns_plain if q.returns_plain else R["return_ship"].for_family(None)),
                   total=q.return_ship, measured=False,
                   note=(f"{q.swap_events:,} returns after a defect or a swap travel on the swap rate" if q.swap_events else None)),
        _component("intake", R["intake"].label, quantity=q.returns, unit="devices returned", rate=R["intake"],
                   rate_value=(q.intake / q.returns if q.returns else R["intake"].for_family(None)),
                   total=q.intake, measured=False),
    ]
    # Days owned come from the receipt and sale dates of every serial; days on rent from the
    # contracts. What lies between is the time a device costs space, handling and money while
    # it earns nothing: the shelf, the carrier, the refurbisher's bench.
    own_reason = None if q.owned_dated else "no device of this group carries a receipt date"
    undated = devices - q.owned_dated
    undated_note = f"{undated:,} devices carry no receipt date and are not in this figure" if undated > 0 else None
    off_rent = None if own_reason else max(0, q.owned_days - days)
    warehouse = [_component(
        "off_rent_days", R["warehouse_day"].label, quantity=off_rent, unit="device-days owned and not out on rent",
        rate=R["warehouse_day"], rate_value=R["warehouse_day"].value,
        total=(None if own_reason else _money(R["warehouse_day"].value * off_rent)), measured=False, reason=own_reason,
        note=(None if own_reason else undated_note))]
    unpriced_note = (f"{q.owned_unpriced:,} devices have no order-line price and tie up nothing here"
                     if q.owned_unpriced else None)
    financing = [_component(
        "capital", R["capital"].label, quantity=(None if own_reason else float(q.owned_value_days)),
        unit="EUR-days of acquisition value owned", rate=R["capital"], rate_value=R["capital"].value,
        total=(None if own_reason else _money(float(q.owned_value_days) * R["capital"].value / 365.0)), measured=False,
        reason=own_reason, note=(None if own_reason else "; ".join(n for n in (undated_note, unpriced_note) if n) or None))]
    eol = [
        _component("resale", "Resale proceeds (credit)", quantity=q.sold_priced, unit="devices sold with recorded proceeds", rate=None,
                   rate_value=(q.proceeds / q.sold_priced if q.sold_priced else None), total=(-q.proceeds if q.sold else None), measured=True,
                   reason=(None if q.sold else "no device of this group has been sold yet"),
                   note=(f"{q.sold - q.sold_priced:,} sold devices have no recorded proceeds" if q.sold > q.sold_priced else None)),
        _component("recycling", R["recycling"].label, quantity=q.recycled, unit="devices recycled", rate=R["recycling"],
                   rate_value=R["recycling"].value, total=_money(R["recycling"].value * q.recycled), measured=False),
    ]
    parts = {"acquisition": acquisition, "inbound": inbound, "enrolment": enrolment, "outbound": outbound,
             "software": software, "support": support, "returns": returns, "service": service,
             "warehouse": warehouse, "financing": financing, "eol": eol}
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
    # What the contracts earned: the rent each one carries, times the months it ran. Set
    # against the whole-life cost this is the margin, per device and per month in service.
    rent_months = q.rent_days / DAYS_PER_MONTH
    if not devices:
        rent_reason = "no device in this group"
    elif not q.contracts:
        rent_reason = "no rental yet"
    elif not q.rent_contracts:
        rent_reason = "no contract of this group carries a rent"
    else:
        rent_reason = None
    revenue = None if rent_reason else round(float(q.rent_eur_days) / DAYS_PER_MONTH, 2)
    margin = (round(revenue - net, 2) if revenue is not None and net is not None else None)
    rent = {
        "revenue": revenue, "contracts_with_rent": q.rent_contracts, "rent_months": round(rent_months, 1),
        "per_month": per(revenue, rent_months), "per_device": per(revenue, devices),
        "margin": margin, "margin_per_device": per(margin, devices), "margin_per_month": per(margin, months),
        "margin_share": (round(margin / revenue, 4) if margin is not None and revenue else None),
        "reason": rent_reason,
        "note": (f"{q.contracts - q.rent_contracts:,} contracts carry no rent; the margin leaves out what they earned"
                 if 0 < q.rent_contracts < q.contracts else None),
    }
    totals = {
        "devices": devices, "rented": q.rented, "on_hand": q.on_hand, "sold": q.sold, "recycled": q.recycled, "priced": q.priced,
        "contracts": q.contracts, "contracts_cycle2": q.contracts_cycle2,
        "device_months": round(months, 1), "device_months_cycle2": round(q.days(today, cycle2=True) / DAYS_PER_MONTH, 1),
        "months_per_device": (round(months / devices, 1) if devices and q.contracts else None),
        "second_life_share_of_months": (round(q.days(today, cycle2=True) / days, 4) if days > 0 else None),
        "repairs": q.repairs, "refurbs": q.refurbs, "in_repair": q.in_repair, "in_refurb": q.in_refurb, "swap_events": q.swap_events,
        "returns": q.returns, "device_days_owned": q.owned_days, "device_days_off_rent": off_rent,
        "gross": gross, "credit": round(credit, 2), "net": net, "rent": rent,
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
        "rates": [{"id": r.id, "label": r.label, "unit": r.unit, "owner": r.owner, "placeholder": r.placeholder, "note": r.note,
                   "value": r.value, "by_family": r.by_family, "source": r.source} for r in RATES.values()],
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


# ---------------------------------------------------------------------------
# one device: a population of one, through the same figures


def device(db: Session, key: str, *, today: Optional[date] = None) -> dict:
    """The whole life of one serial: every layer of its TCO, what its contracts earned, and the
    dated events behind both. ``key`` is the serial number or the asset id.

    The quantities are the same ones the fleet reads sum, taken from this device's own rows,
    and they go through the same ``_figures``: a serial's number and its model's average are
    one calculation at two sizes.
    """
    today = today or date.today()
    a = db.scalar(select(Asset).where(Asset.serial_number == key)) or db.get(Asset, key)
    if a is None:
        raise NotFoundError(f"Device {key!r} not found")
    prod = db.get(Product, a.product_id)
    family = prod.category if prod is not None else None
    line = db.get(OrderItem, a.source_order_item_id) if a.source_order_item_id else None
    price = Decimal(str(line.unit_price)) if line is not None and line.unit_price is not None else None
    rc, se = RentalContract, ServiceEvent
    contracts = db.scalars(select(rc).where(rc.asset_id == a.id).order_by(rc.cycle_no, rc.start_date)).all()
    events = db.scalars(select(se).where(se.asset_id == a.id).order_by(se.event_date)).all()

    q, status = _Q(devices=1), a.status
    finished = status in FINISHED_STATUSES
    if status == AssetStatus.RENTED:
        q.rented = 1
    elif status in WAREHOUSE_STATUSES:
        q.on_hand = 1
    elif status == AssetStatus.SOLD:
        q.sold = 1
    elif status == AssetStatus.RECYCLED:
        q.recycled = 1
    q.in_repair = int(status == AssetStatus.REPAIR)
    q.in_refurb = int(status == AssetStatus.REFURB)
    if price is not None:
        q.priced, q.acquisition = 1, price
    if status == AssetStatus.SOLD and a.sale_price is not None:
        q.sold_priced, q.proceeds = 1, _money(a.sale_price)
        q.acquisition_of_sold = price if price is not None else _ZERO

    life: list[dict] = []
    if a.received_date is not None:
        life.append({"date": _as_date(a.received_date), "kind": "received", "amount": (float(price) if price is not None else None)})
    for c in contracts:
        start = _as_date(c.start_date)
        end = _as_date(c.actual_end) if c.actual_end is not None else None
        d = max(0, ((end or today) - start).days)
        q.contracts += 1
        q.direct_days += d
        if c.cycle_no >= 2:
            q.contracts_cycle2 += 1
            q.direct_days_cycle2 += d
        if end is not None:
            _add_return(q, c.end_reason, 1)
        rent = Decimal(str(c.rent_eur_month)) if c.rent_eur_month is not None else None
        if rent is not None:
            _add_rent(q, 1, rent * d, d)
        life.append({"date": start, "kind": "rental", "cycle": c.cycle_no, "end": end, "reason": c.end_reason,
                     "days": d, "rent_eur_month": (float(rent) if rent is not None else None),
                     "amount": (round(float(rent) * d / DAYS_PER_MONTH, 2) if rent is not None else None)})
    for e in events:
        if e.kind == ServiceKind.REPAIR or e.kind == "REPAIR":
            q.repairs += 1
            q.repair_cost += _money(e.cost)
        else:
            q.refurbs += 1
            q.refurb_cost += _money(e.cost)
        kind = getattr(e.kind, "value", e.kind)
        life.append({"date": _as_date(e.event_date), "kind": str(kind).lower(), "cycle": e.cycle_no, "amount": float(_money(e.cost))})
    if a.received_date is not None:
        owned_end = a.sold_date or a.decommissioned_date or today
        q.owned_dated = 1
        q.owned_days = max(0, (_as_date(owned_end) - _as_date(a.received_date)).days)
        if price is not None:
            q.owned_value_days = price * q.owned_days
        else:
            q.owned_unpriced = 1
    if status == AssetStatus.SOLD and a.sold_date is not None:
        life.append({"date": _as_date(a.sold_date), "kind": "sold", "channel": a.sale_channel,
                     "amount": (float(_money(a.sale_price)) if a.sale_price is not None else None)})
    elif status == AssetStatus.RECYCLED and (a.decommissioned_date or a.sold_date) is not None:
        life.append({"date": _as_date(a.decommissioned_date or a.sold_date), "kind": "recycled", "amount": None})
    life.sort(key=lambda ev: ev["date"])
    _apply_family_rates(q, family)

    layers, totals = _figures(q, today, finished=finished)
    row = {"kind": "device", "key": a.serial_number, "label": (prod.name if prod is not None else a.product_id),
           "family": family, "product_code": (prod.product_code if prod is not None else None), "reason": None,
           "as_of": today, "id": a.id, "serial_number": a.serial_number, "product_id": a.product_id,
           "status": getattr(status, "value", status), "grade": a.grade, "cycle_no": a.cycle_no, "finished": finished,
           "unit_price": (float(price) if price is not None else None), "received_date": a.received_date,
           "sold_date": a.sold_date, "sale_price": (float(a.sale_price) if a.sale_price is not None else None),
           "sale_channel": a.sale_channel, "life": life}
    row.update(totals, layers=layers)
    return row


SERIAL_GROUPS = (("finished", FINISHED_STATUSES), ("rented", (AssetStatus.RENTED,)), ("on_hand", tuple(WAREHOUSE_STATUSES)))


def serials(db: Session, product_id: str, *, per_group: int = 4) -> dict:
    """A few serials of one model to open one by one: finished lives first, the ones with the
    most rentals behind them, then rented, then on hand. A handful of index reads per model."""
    prod = db.get(Product, product_id)
    if prod is None:
        raise NotFoundError(f"Product {product_id!r} not found")
    out = []
    for group, statuses in SERIAL_GROUPS:
        rows = db.execute(select(Asset.serial_number, Asset.status, Asset.cycle_no)
                          .where(Asset.product_id == product_id, Asset.status.in_(statuses))
                          .order_by(Asset.cycle_no.desc(), Asset.serial_number).limit(per_group)).all()
        out += [{"serial_number": s, "status": getattr(st, "value", st), "cycle_no": c, "group": group} for s, st, c in rows]
    return {"product_id": product_id, "label": prod.name, "family": prod.category, "serials": out}

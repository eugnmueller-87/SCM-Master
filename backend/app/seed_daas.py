"""DaaS demo dataset — a device-as-a-service fleet in the middle of its scale-up.

The company is "the DaaS provider": it buys smartphones, tablets and laptops, rents
them to business customers for 12 to 48 months, takes them back, wipes and grades
them, repairs or refurbishes them, rents them a second time, and finally sells or
recycles them. Every serial carries its history in ``asset`` + ``rental_contract``
so Overview, Fleet, Returns, Warehouse, KPIs and Spend all read one truth.

**Scale.** 300,000 devices at customers, 100,000 in the warehouse — the business
owner's instruction of 21.09.2026, at full size by default. With the resale and
recycling history that is about 431,000 serials and 493,000 rental contracts.
``DAAS_SCALE`` shrinks everything proportionally for a quick run (``0.1`` -> 43,000
serials in about seven seconds); the ratios stay exact, only the absolute money moves.

**Mid-scale-up, not steady state.** A fleet in equilibrium has its contracts spread
evenly over their term. This one does not: ``GROWTH_SKEW`` pulls rental starts toward
the recent months, so the fleet is visibly younger than its own terms and the return
wave is still ahead of it. Three more things follow from that and are seeded on
purpose: an inbound pipeline of ``N_INBOUND`` devices still on order, two intake
stations already over their capacity, and 24 customers onboarded in the last nine
months that did not exist a year ago. That is what the screens should show — a
business growing faster than its warehouse.

**Honesty.** Catalogue prices are public launch RRPs (source URL on each product).
Every other number is a synthetic design parameter, marked below with the role that
would own it in a real company. No real customer, supplier or provider name beyond
the manufacturers: customers and partners are role-only.

Run (from backend/):
    .venv\\Scripts\\alembic upgrade head
    .venv\\Scripts\\python -m app.seed_demo                          # this dataset boots by default
    set DAAS_SCALE=0.1 && .venv\\Scripts\\python -m app.seed_daas    # a tenth, for a quick run

Idempotent: bails out if the catalog is already populated. A database holding a
*different* dataset is replaced on boot — see ``app/seed_reset.py``.

Memory: the fleet never exists in Python as a whole. Rows stream to the database in
chunks of ``CHUNK`` through SQLAlchemy Core; the master data goes through the real
services, so it is built the way the application itself would build it.
"""
from __future__ import annotations

import math
import os
import random
import uuid
from bisect import bisect_left, bisect_right
from collections import Counter
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import bindparam, delete, insert, select, update

from app.core.db import SessionLocal
from app.core.safety import assert_seeding_allowed
from app.models.auth import Role
from app.models.catalog import Product
from app.models.flow import Asset, AssetStatus, LocationType
from app.models.procurement import OrderItem, OrderStatus, PurchaseOrder
from app.models.rental import ContractStatus, RentalContract
from app.services import warehouse
from app.services.auth import ensure_user
from app.services.catalog import organization_service, product_service, product_supplier_service
from app.services.flow import location_service

DAYS_PER_MONTH = 30.4375
VAT = 0.19
CHUNK = 5_000                           # rows per bulk insert; the fleet never sits in memory as a whole
LINE_NS = uuid.UUID("6f1b6a3e-1d5a-4f7b-9a1e-2c0d5f8b41aa")   # fixed namespace: an order line's id is a function of (product, month)

# ---------------------------------------------------------------------------
# design parameters (synthetic; the role that would own the number in brackets)

SCALE = float(os.getenv("DAAS_SCALE", "1.0"))
N_RENTED = int(300_000 * SCALE)          # business owner's instruction 21.09.2026
N_WAREHOUSE = int(100_000 * SCALE)       # business owner's instruction 21.09.2026
N_SOLD_LAST_YEAR = int(30_000 * SCALE)   # resale history, last 12 months [placeholder, Head of Recommerce]
N_RECYCLED_LAST_YEAR = int(1_200 * SCALE)
N_INBOUND = int(28_000 * SCALE)          # devices on order, not yet received [placeholder, Head of Supply]

# A steady fleet spreads rental starts evenly over the term (skew 1.0). This one is
# scaling: the exponent pulls starts toward the present, so recent quarters carry more
# new devices than older ones and the return wave is still building. 1.25 lands at
# roughly +60 % first rentals year over year - fast, and still a rate a real supply
# chain can be asked to keep up with. [Head of Sales]
GROWTH_SKEW = 1.25
N_CUSTOMERS = 84                         # role-only accounts
N_CUSTOMERS_NEW = 24                     # of those, onboarded within the last nine months

FAMILY_MIX = {"Smartphone": 0.60, "Tablet": 0.15, "Laptop": 0.25}   # placeholder [Head of Procurement]
TERM_MIX = {                                                          # placeholder [Head of Customer Success]
    "Smartphone": {12: 0.10, 24: 0.52, 36: 0.33, 48: 0.05},
    "Tablet": {12: 0.05, 24: 0.40, 36: 0.45, 48: 0.10},
    "Laptop": {12: 0.05, 24: 0.25, 36: 0.50, 48: 0.20},
}
TERM_MIX_CYCLE2 = {24: 0.60, 36: 0.40}
SHARE_CYCLE2 = 0.25                       # share of the rented fleet on its second rental [placeholder, Head of Recommerce]
EARLY_TERMINATION = 0.06                  # [Head of Customer Success]
OVERDUE_RETURN_SHARE = 0.005              # devices still out after the contract ended [placeholder, Head of Customer Success]
OVERDUE_MAX_DAYS = 75                     # how late the latest of them is
GRADE_MIX = {"A": 0.35, "B": 0.40, "C": 0.20, "D": 0.05}
RENT2_SHARE = 0.70                        # second rent as share of the first [placeholder, Head of Sales]
RENT_SHARE_PER_MONTH = {"Smartphone": 0.042, "Tablet": 0.040, "Laptop": 0.038}
TERM_RATE_FACTOR = {12: 1.5, 24: 1.0, 36: 0.85, 48: 0.75}
DISCOUNT = {"Apple": (0.05, 0.12), "Samsung": (0.12, 0.25), "Google": (0.10, 0.22), "Fairphone": (0.03, 0.08), "Lenovo": (0.15, 0.30), "HP": (0.15, 0.28)}
# warehouse composition: shares from the fleet simulation of 21.09.2026 (flow x dwell, plus the owner's top-up)
#
# Second-life stock (READY_SECOND, added 23.09.2026) is taken out of new stock, not added on top:
# the warehouse holds exactly N_WAREHOUSE devices by the owner's instruction, and refurbished
# devices waiting for their second customer were counted as new stock before, which is the
# mixing the owner named. Derivation, the same flow x dwell as the other stations: the seeded
# fleet returns about 8,900 devices a month over the next twelve months, 4,900 of them headed
# for a second rental after the grade rule; at the wait of new stock (7 to 30 days, mean 18.5)
# that flow holds about 3,000 devices, the rest is refurbished stock waiting for a customer
# that takes a used device, the second-life analogue of the sale backlog. [placeholder, Head of Recommerce]
WAREHOUSE_MIX = {
    AssetStatus.RETURNED: 0.045, AssetStatus.MDM_RELEASE: 0.040, AssetStatus.WIPE_GRADING: 0.008, AssetStatus.REPAIR: 0.014,
    AssetStatus.REFURB: 0.011, AssetStatus.READY_SECOND: 0.100, AssetStatus.SELLABLE: 0.368, AssetStatus.SWAP_BUFFER: 0.060,
    AssetStatus.IN_STORAGE: 0.354,
}
# Station capacity was sized for last year's fleet, not for the wave arriving now. A factor
# below 1.0 means the station is already over its capacity — the scale-up on the floor.
STATION_CAPACITY_FACTOR = {
    AssetStatus.RETURNED: 0.85, AssetStatus.MDM_RELEASE: 0.80, AssetStatus.WIPE_GRADING: 1.60, AssetStatus.REPAIR: 1.60,
    AssetStatus.REFURB: 1.60, AssetStatus.READY_SECOND: 1.20, AssetStatus.SELLABLE: 1.25, AssetStatus.SWAP_BUFFER: 1.50,
    AssetStatus.IN_STORAGE: 1.45,
}
DWELL_MAX_DAYS = {AssetStatus.RETURNED: 15, AssetStatus.MDM_RELEASE: 21, AssetStatus.WIPE_GRADING: 3, AssetStatus.REPAIR: 20,
                  AssetStatus.REFURB: 12, AssetStatus.READY_SECOND: 45, AssetStatus.SELLABLE: 60, AssetStatus.SWAP_BUFFER: 120,
                  AssetStatus.IN_STORAGE: 30}
SECOND_LIFE_WAITING_SHARE = 0.5           # second-life stock refurbished ahead of its demand, waits longer [placeholder, Head of Recommerce]
SLOW_MOVER_SHARE = 0.04
CHANNEL_MIX = {"marketplace": 0.55, "b2b_wholesale": 0.30, "employee_buyout": 0.10, "as_is": 0.05}
CHANNEL_FEE = {"marketplace": 0.12, "b2b_wholesale": 0.20, "employee_buyout": 0.00, "as_is": 0.25}
GRADE_FACTOR = {"A": 1.09, "B": 1.00, "C": 0.86, "D": 0.73}       # marketplace grade offsets, simplified from the public curves

# The catalogue: public launch RRPs, gross EUR, Germany. Source URL on every row.
# (code, name, family, oem, launch, rrp_gross, source)
CATALOGUE = [
    ("APL-IP16-128", "iPhone 16 · 128 GB", "Smartphone", "Apple", date(2024, 9, 20), 949, "https://www.apple.com/de/newsroom/2024/09/apple-introduces-iphone-16-and-iphone-16-plus/"),
    ("APL-IP15-128", "iPhone 15 · 128 GB", "Smartphone", "Apple", date(2023, 9, 22), 949, "https://www.apple.com/de/newsroom/2023/09/apple-debuts-iphone-15-and-iphone-15-plus/"),
    ("APL-IP16E-128", "iPhone 16e · 128 GB", "Smartphone", "Apple", date(2025, 2, 28), 699, "https://www.apple.com/de/newsroom/2025/02/apple-debuts-iphone-16e-a-powerful-new-member-of-the-iphone-16-family/"),
    ("SAM-S24-128", "Galaxy S24 · 128 GB", "Smartphone", "Samsung", date(2024, 1, 31), 899, "https://news.samsung.com/de/samsung-enthullt-die-galaxy-s24-serie"),
    ("SAM-S25-128", "Galaxy S25 · 128 GB", "Smartphone", "Samsung", date(2025, 2, 7), 899, "https://news.samsung.com/de/samsung-galaxy-s25-serie-ai-smartphones"),
    ("SAM-A55-128", "Galaxy A55 · 128 GB", "Smartphone", "Samsung", date(2024, 3, 11), 479, "https://news.samsung.com/de/samsung-galaxy-a55-5g-und-galaxy-a35-5g"),
    ("GOO-PX9-128", "Pixel 9 · 128 GB", "Smartphone", "Google", date(2024, 8, 22), 899, "https://www.googlewatchblog.de/2024/08/made-by-google-neue-pixel-9/"),
    ("FPH-FP5-256", "Fairphone 5 · 8 GB / 256 GB", "Smartphone", "Fairphone", date(2023, 9, 14), 699, "https://www.teltarif.de/fairphone-5-neuvorstellung-preis-update/news/91859.html"),
    ("APL-IPAD10-64", "iPad (10th gen) · 64 GB Wi-Fi", "Tablet", "Apple", date(2022, 10, 26), 579, "https://www.apple.com/de/newsroom/2022/10/apple-unveils-completely-redesigned-ipad-in-four-vibrant-colors/"),
    ("SAM-TABS9FE-128", "Galaxy Tab S9 FE · 128 GB Wi-Fi", "Tablet", "Samsung", date(2023, 10, 11), 529, "https://www.notebookcheck.com/Samsung-startet-Verkauf-von-Galaxy-Tab-S9-FE.760046.0.html"),
    ("APL-MBA13-M3", "MacBook Air 13 (M3) · 8 GB / 256 GB", "Laptop", "Apple", date(2024, 3, 8), 1299, "https://www.apple.com/de/newsroom/2024/03/apple-unveils-the-new-13-and-15-inch-macbook-air-with-the-powerful-m3-chip/"),
    ("LEN-T14G5", "ThinkPad T14 Gen 5 · Core Ultra 7 / 16 GB / 512 GB", "Laptop", "Lenovo", date(2024, 5, 1), 1719, "https://www.notebookcheck.com/Test-Lenovo-ThinkPad-T14-Gen-5-Intel.851254.0.html"),
    ("HP-EB840G10", "EliteBook 840 G10 · Core i7 / 16 GB / 512 GB", "Laptop", "HP", date(2023, 5, 1), 1300, "https://www.notebookcheck.com/HP-Elitebook-840-G10.770372.0.html"),
]


def _residual_share(family: str, age_months: float, grade: str) -> float:
    """Share of the net launch price a used device fetches — simplified from the fitted
    public curves of the Restwert Engine (marketplace, 2026-09): log-linear in age."""
    if family == "Laptop":
        base = math.exp(0.40 - 0.0230 * max(6.0, min(age_months, 84.0)))
    else:
        base = math.exp(-0.46 - 0.0096 * max(6.0, min(age_months, 84.0)))
    return max(0.03, min(0.95, base * GRADE_FACTOR.get(grade, 1.0)))


def _pick(rng: random.Random, mix: dict):
    r = rng.random()
    acc = 0.0
    for k, p in mix.items():
        acc += p
        if r <= acc:
            return k
    return k


def _ym(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def _month_steps(first: date, last: date):
    """Every month from ``first`` to ``last`` inclusive, as YYYY-MM."""
    y, m = first.year, first.month
    while (y, m) <= (last.year, last.month):
        yield f"{y:04d}-{m:02d}"
        m += 1
        if m == 13:
            y, m = y + 1, 1


def _line_id(code: str, ym: str) -> str:
    """An order line is identified by what it is: this product, bought in this month.

    Deriving the id instead of generating one is what lets the fleet stream to the
    database: a serial can name the line it came from long before that line's
    quantity is known.
    """
    return str(uuid.uuid5(LINE_NS, f"{code}|{ym}"))


class _Sink:
    """Buffered bulk insert for the fleet.

    Assets and contracts are flushed together, assets first, because a contract points
    at an asset. Counters live here so the summary at the end does not need the rows to
    still be in memory.
    """

    def __init__(self, db, chunk: int = CHUNK):
        self.db, self.chunk = db, chunk
        self.assets: list[dict] = []
        self.contracts: list[dict] = []
        self.n_assets = 0
        self.n_contracts = 0
        self.by_status: Counter = Counter()
        self.cycle2_rented = 0
        self.first_rental_by_quarter: Counter = Counter()

    def asset(self, row: dict) -> None:
        self.assets.append(row)
        self.n_assets += 1
        self.by_status[row["status"]] += 1
        if len(self.assets) >= self.chunk:
            self.flush()

    def contract(self, row: dict) -> None:
        self.contracts.append(row)
        self.n_contracts += 1
        if row["cycle_no"] == 1:
            s = row["start_date"]
            self.first_rental_by_quarter[f"{s.year}-Q{(s.month - 1) // 3 + 1}"] += 1

    def flush(self) -> None:
        if self.assets:
            self.db.execute(insert(Asset), self.assets)
            self.assets.clear()
        if self.contracts:
            self.db.execute(insert(RentalContract), self.contracts)
            self.contracts.clear()
        self.db.commit()


def seed_daas() -> None:
    assert_seeding_allowed("DaaS demo dataset")
    rng = random.Random(42)  # nosec B311 - a fixed seed, not a secret: the dataset must be reproducible
    today = date.today()
    db = SessionLocal()
    try:
        if db.scalar(select(Product).limit(1)):
            print("Catalog already populated — skipping DaaS seed.")
            return

        # --- users -----------------------------------------------------------
        ensure_user(db, email="admin@example.com", full_name="Anders Mohr", password="admin", role=Role.ADMIN)  # nosec B106 demo-only
        ensure_user(db, email="buyer@example.com", full_name="Pia Schulz", password="buyer", role=Role.PROCUREMENT)  # nosec B106
        ensure_user(db, email="warehouse@example.com", full_name="Tomas Reuter", password="whse", role=Role.WAREHOUSE)  # nosec B106
        ensure_user(db, email="guest@example.com", full_name="Demo Guest", password="guest", role=Role.VIEWER)  # nosec B106

        # --- organisations: manufacturers, role-only partners, role-only customers
        oems = {}
        for code, name in (("APPLE", "Apple"), ("SAMSUNG", "Samsung"), ("GOOGLE", "Google"), ("FAIRPHONE", "Fairphone"), ("LENOVO", "Lenovo"), ("HP", "HP")):
            oems[name] = organization_service.create(db, dict(code=code, name=name, is_supplier=True, is_manufacturer=True))
        reseller_a = organization_service.create(db, dict(code="RESELLER-A", name="IT reseller A (role-only)", is_supplier=True, is_manufacturer=False))
        reseller_b = organization_service.create(db, dict(code="RESELLER-B", name="IT reseller B (role-only)", is_supplier=True, is_manufacturer=False))
        organization_service.create(db, dict(code="REPAIR-P", name="Refurbishment and repair partner (role-only)", is_supplier=True, is_manufacturer=False))
        organization_service.create(db, dict(code="LOGI-P", name="Logistics partner (role-only)", is_supplier=True, is_manufacturer=False))

        def onboard(org, risk="LOW"):
            organization_service.record_risk(db, org, risk_level=risk, risk_notes=f"Desk review — {risk} exposure", assessed_at=today - timedelta(days=60))
            for kind in ("dpa", "nda"):
                organization_service.record_document(db, org, kind=kind, signed=True, reference=f"{kind.upper()}-{org.code}-2025", signed_at=today - timedelta(days=55))
            if org.onboarding_complete:
                organization_service.approve(db, org)

        for o in list(oems.values()) + [reseller_a, reseller_b]:
            onboard(o)

        # Customers carry an onboarding date, and a contract can only belong to a customer
        # that already existed when it started. The last 24 are the new logos of this
        # scale-up; the first five are the large accounts that carry most of the fleet.
        ids, since_dates, weights = [], [], []
        established = N_CUSTOMERS - N_CUSTOMERS_NEW
        for i in range(N_CUSTOMERS):
            if i < established:
                since = today - timedelta(days=rng.randint(400, 2200))
                weight = 8.0 if i < 5 else (3.0 if i < 20 else 1.0)
            else:
                since = today - timedelta(days=rng.randint(20, 270))    # new this year
                weight = 2.0
            org = organization_service.create(db, dict(code=f"CUST-{i + 1:03d}", name=f"Customer {i + 1:03d} (role-only)", is_supplier=False, is_manufacturer=False))
            ids.append(org.id)
            since_dates.append(since)
            weights.append(weight)
        order = sorted(range(N_CUSTOMERS), key=lambda i: since_dates[i])
        cust_ids = [ids[i] for i in order]
        cust_since = [since_dates[i] for i in order]
        cum_weight, acc = [], 0.0
        for i in order:
            acc += weights[i]
            cum_weight.append(acc)

        def customer_for(start: date) -> str:
            """A customer that had already signed on by ``start``, weighted by size."""
            k = bisect_right(cust_since, start) or 1
            r = rng.random() * cum_weight[k - 1]
            return cust_ids[bisect_left(cum_weight, r, 0, k)]

        # --- products and sources --------------------------------------------
        products = {}
        sources = {}
        route = {"Apple": None, "Samsung": None, "Fairphone": None, "Google": reseller_a, "Lenovo": reseller_b, "HP": reseller_b}
        for i, (code, name, family, oem, launch, rrp, url) in enumerate(CATALOGUE):
            p = product_service.create(db, dict(product_code=code, name=name, category=family,
                                                description=f"Launch RRP {rrp} EUR gross, Germany, {launch.isoformat()}. Source: {url}"))
            lo, hi = DISCOUNT[oem]
            disc = (lo + hi) / 2.0
            price = Decimal(str(round(rrp / (1 + VAT) * (1 - disc), 2)))
            supplier = route[oem] or oems[oem]
            # contract states: most active, two renewal-due, one expiring, so the Contracts tab and the KPI have something to say
            term_end = today + timedelta(days=400 + 30 * i)
            stored_status = "ACTIVE"
            if i in (3, 9):
                term_end, stored_status = today + timedelta(days=35), None      # derived: RENEWAL_DUE
            if i == 12:
                term_end, stored_status = today + timedelta(days=10), None      # derived: EXPIRING
            ps = product_supplier_service.create(db, dict(
                product_id=p.id, supplier_id=supplier.id, manufacturer_id=oems[oem].id,
                contract_price=price, standard_lead_time_days=(14 if route[oem] else 21), min_order_quantity=50,
                preference_rank=1, supplier_product_code=f"{supplier.code}-{code}", contract_status=stored_status,
                term_start=today - timedelta(days=300), term_end=term_end,
                annual_budget=Decimal(str(round(float(price) * 2500 * SCALE * 10, 2)))))
            products[code] = (p, family, oem, launch, rrp, float(price), supplier)
            sources[code] = ps
            # a second source for the volume models (Apple and Samsung phones via reseller A): re-sourcing is possible
            if oem in ("Apple", "Samsung") and family == "Smartphone":
                product_supplier_service.create(db, dict(
                    product_id=p.id, supplier_id=reseller_a.id, manufacturer_id=oems[oem].id,
                    contract_price=Decimal(str(round(float(price) * 1.02, 2))), standard_lead_time_days=10, min_order_quantity=25,
                    preference_rank=2, supplier_product_code=f"{reseller_a.code}-{code}", contract_status="ACTIVE",
                    term_start=today - timedelta(days=200), term_end=today + timedelta(days=500)))

        # --- the warehouse: nine compartments of one flow ---------------------
        # One station location per compartment, code and name from the warehouse service,
        # so the seed and the read cannot drift apart. No parent location. Capacity is the
        # sum of the stations, which is what the capacity screen and the order guard read;
        # two stations are short on purpose.
        stations = {}
        for comp in warehouse.COMPARTMENTS:
            primary = comp.statuses[0]
            cap = max(10, int(N_WAREHOUSE * WAREHOUSE_MIX[primary] * STATION_CAPACITY_FACTOR[primary]))
            loc = location_service.create(db, dict(code=comp.code, name=comp.name, location_type=LocationType.WAREHOUSE, capacity=cap))
            for st in comp.statuses:
                stations[st] = loc
        db.flush()
        intake = stations[AssetStatus.IN_STORAGE]

        # --- the order grid ---------------------------------------------------
        # One purchase order per supplier and month, one line per product and month,
        # written BEFORE the fleet so every serial can name the line it came from.
        # Quantities are counted while the fleet streams and written back at the end;
        # lines that stayed empty are deleted.
        seen_po: dict[str, dict] = {}
        item_rows = []
        for code, (p, family, oem, launch, rrp, price, supplier) in products.items():
            for ym in _month_steps(launch + timedelta(days=14), today):
                ordered = date(int(ym[:4]), int(ym[5:]), 1)
                po_id = str(uuid.uuid5(LINE_NS, f"PO|{supplier.code}|{ym}"))
                if po_id not in seen_po:
                    seen_po[po_id] = dict(id=po_id, order_number=f"PO-{ym.replace('-', '')}-{supplier.code}", status=OrderStatus.RECEIVED,
                                          supplier_id=supplier.id, destination_id=intake.id, currency_code="EUR", date_ordered=ordered)
                item_rows.append(dict(id=_line_id(code, ym), order_id=po_id, product_id=p.id, product_supplier_id=sources[code].id,
                                      quantity=0, unit_price=Decimal(str(price)), estimated_delivery_date=ordered + timedelta(days=21),
                                      actual_delivery_date=ordered + timedelta(days=rng.randint(14, 28))))
        po_rows = list(seen_po.values())
        db.execute(insert(PurchaseOrder), po_rows)
        db.execute(insert(OrderItem), item_rows)
        db.commit()

        # --- the fleet --------------------------------------------------------
        sink = _Sink(db)
        qty: Counter = Counter()
        serial_no = [0]
        codes = list(products.keys())
        by_family = {f: [c for c in codes if products[c][1] == f] for f in FAMILY_MIX}

        def choose_product(family: str, purchase: date) -> str:
            pool = [c for c in by_family[family] if products[c][3] <= purchase - timedelta(days=14)]
            if not pool:
                pool = sorted(by_family[family], key=lambda c: products[c][3])[:1]
            weights = [math.exp(-max(0, (purchase - products[c][3]).days) / 365.0 * 1.2) for c in pool]
            return rng.choices(pool, weights=weights, k=1)[0]

        def rent_for(code: str, term: int, cycle: int) -> float:
            _, family, _, _, _, price, _ = products[code]
            r = price * RENT_SHARE_PER_MONTH[family] * TERM_RATE_FACTOR[term]
            return round(r * (RENT2_SHARE if cycle == 2 else 1.0), 2)

        def add_asset(code: str, purchase: date, **kw) -> str:
            serial_no[0] += 1
            ym = _ym(purchase)
            qty[(code, ym)] += 1
            aid = str(uuid.uuid4())
            row = dict(id=aid, serial_number=f"DAAS-{serial_no[0]:07d}", status=AssetStatus.IN_STORAGE, cycle_no=0,
                       current_location_id=None, product_id=products[code][0].id, source_order_item_id=_line_id(code, ym),
                       received_date=None, deployed_date=None, warranty_end_date=None, decommissioned_date=None, notes=None,
                       grade=None, battery_health=None, customer_id=None, status_since=None, sold_date=None, sale_price=None, sale_channel=None)
            row.update(kw)
            sink.asset(row)
            return aid

        def add_contract(aid: str, code: str, cycle: int, start: date, term: int, end, reason, customer: str) -> None:
            sink.contract(dict(asset_id=aid, customer_id=customer, cycle_no=cycle, start_date=start, term_months=term,
                               planned_end=start + timedelta(days=round(term * DAYS_PER_MONTH)), actual_end=end, end_reason=reason,
                               rent_eur_month=rent_for(code, term, cycle),
                               status=ContractStatus.ENDED if end else ContractStatus.RUNNING))

        # rented fleet — the scale-up shows here: elapsed time is skewed toward the start
        for _ in range(N_RENTED):
            family = _pick(rng, FAMILY_MIX)
            cycle = 2 if rng.random() < SHARE_CYCLE2 else 1
            t1 = _pick(rng, TERM_MIX[family])
            t2 = _pick(rng, TERM_MIX_CYCLE2)
            term_now = t2 if cycle == 2 else t1
            elapsed = timedelta(days=int(rng.random() ** GROWTH_SKEW * term_now * DAYS_PER_MONTH))
            start_now = today - elapsed
            if cycle == 2:
                end1 = start_now - timedelta(days=rng.randint(20, 60))
                start1 = end1 - timedelta(days=round(t1 * DAYS_PER_MONTH))
            else:
                end1, start1 = None, start_now
            purchase = start1 - timedelta(days=rng.randint(7, 30))
            code = choose_product(family, purchase)
            launch = products[code][3]
            purchase = max(purchase, launch + timedelta(days=14))
            start1 = max(start1, purchase + timedelta(days=7))
            if cycle == 2 and start1 + timedelta(days=50) > start_now:
                # The model launched too late for a full first rental before this one, so this
                # IS the first rental. Its start has to be re-drawn against the new term:
                # keeping the old one produced contracts whose planned end was already two
                # years in the past, all on the same late-launching model - an artefact of the
                # generator that read like a fleet-wide failure to collect devices.
                cycle, end1, term_now = 1, None, t1
                elapsed = timedelta(days=int(rng.random() ** GROWTH_SKEW * term_now * DAYS_PER_MONTH))
                start_now = max(start1, today - elapsed)
            if rng.random() < OVERDUE_RETURN_SHARE:
                # A share of devices is still out after the contract ended: the customer has
                # not shipped them back. Days, not years - that is what chasing looks like.
                start_now = today - timedelta(days=round(term_now * DAYS_PER_MONTH) + rng.randint(1, OVERDUE_MAX_DAYS))
                start_now = max(start_now, purchase + timedelta(days=7))
            cust = customer_for(start_now)
            aid = add_asset(code, purchase, status=AssetStatus.RENTED, cycle_no=cycle, customer_id=cust,
                            received_date=purchase + timedelta(days=rng.randint(2, 6)), deployed_date=start_now, status_since=start_now,
                            grade=(_pick(rng, GRADE_MIX) if cycle == 2 else None),
                            battery_health=round(max(0.6, 1.0 - 0.006 * ((today - purchase).days / DAYS_PER_MONTH) + rng.gauss(0, 0.03)), 3),
                            warranty_end_date=purchase + timedelta(days=730))
            if cycle == 2:
                sink.cycle2_rented += 1
                end1 = max(end1, start1 + timedelta(days=30))
                add_contract(aid, code, 1, start1, t1, end1, "planned", customer_for(start1))
            add_contract(aid, code, cycle, start_now, term_now, None, None, cust)

        # warehouse
        counts = {st: int(round(N_WAREHOUSE * share)) for st, share in WAREHOUSE_MIX.items()}
        counts[AssetStatus.IN_STORAGE] += N_WAREHOUSE - sum(counts.values())
        for st, n in counts.items():
            for _ in range(n):
                family = _pick(rng, FAMILY_MIX)
                since_days = rng.randint(0, DWELL_MAX_DAYS[st])
                if st == AssetStatus.MDM_RELEASE and rng.random() < 0.25:
                    since_days = rng.randint(22, 60)         # the old customer is slow: over the release SLA
                if st == AssetStatus.SELLABLE and rng.random() < 0.6:
                    since_days = rng.randint(30, 400)        # the owner's top-up: sale backlog, older than the flow
                elif st == AssetStatus.SELLABLE and rng.random() < SLOW_MOVER_SHARE:
                    since_days = rng.randint(90, 400)
                if st == AssetStatus.IN_STORAGE and rng.random() < 0.6:
                    since_days = rng.randint(0, 120)         # stock bought ahead of the next customer ramp
                if st == AssetStatus.READY_SECOND and rng.random() < SECOND_LIFE_WAITING_SHARE:
                    since_days = rng.randint(30, 150)        # refurbished ahead of second-life demand: waits for a customer that takes a used device
                status_since = today - timedelta(days=since_days)
                if st == AssetStatus.IN_STORAGE:
                    purchase = status_since - timedelta(days=rng.randint(0, 10))
                    code = choose_product(family, purchase)
                    purchase = max(purchase, products[code][3] + timedelta(days=14))
                    add_asset(code, purchase, status=st, cycle_no=0, current_location_id=stations[st].id,
                              received_date=purchase + timedelta(days=rng.randint(2, 6)), status_since=status_since, grade="A",
                              battery_health=round(rng.uniform(0.97, 1.0), 3), warranty_end_date=purchase + timedelta(days=730))
                    continue
                # a returned device: after cycle 1 (or 2 for the sale-bound ones)
                cycle_done = 2 if (st in (AssetStatus.SELLABLE, AssetStatus.WIPE_GRADING, AssetStatus.RETURNED, AssetStatus.MDM_RELEASE) and rng.random() < 0.35) else 1
                t1 = _pick(rng, TERM_MIX[family])
                t2 = _pick(rng, TERM_MIX_CYCLE2)
                early = rng.random() < EARLY_TERMINATION
                defect = st == AssetStatus.REPAIR and rng.random() < 0.6
                back = {AssetStatus.RETURNED: 0, AssetStatus.MDM_RELEASE: 10, AssetStatus.WIPE_GRADING: 22, AssetStatus.REPAIR: 24,
                        AssetStatus.REFURB: 30, AssetStatus.READY_SECOND: 42, AssetStatus.SELLABLE: 30, AssetStatus.SWAP_BUFFER: 40}[st]
                return_date = status_since - timedelta(days=back)
                used1 = rng.uniform(3, t1) if early else t1
                used2 = rng.uniform(3, t2) if early else t2
                if defect:
                    used1 = rng.uniform(1, t1)
                if cycle_done == 2:
                    end2 = return_date
                    start2 = end2 - timedelta(days=round(used2 * DAYS_PER_MONTH))
                    end1 = start2 - timedelta(days=rng.randint(20, 60))
                    start1 = end1 - timedelta(days=round(used1 * DAYS_PER_MONTH))
                else:
                    end1 = return_date
                    start1 = end1 - timedelta(days=round(used1 * DAYS_PER_MONTH))
                purchase = start1 - timedelta(days=rng.randint(7, 30))
                code = choose_product(family, purchase)
                purchase = max(purchase, products[code][3] + timedelta(days=14))
                start1 = max(start1, purchase + timedelta(days=7))
                end1 = max(end1, start1 + timedelta(days=30))
                if cycle_done == 2:
                    start2 = max(start2, end1 + timedelta(days=20))
                    end2 = max(end2, start2 + timedelta(days=30))
                    return_date = end2
                grade = _pick(rng, GRADE_MIX)
                if st == AssetStatus.REPAIR:
                    grade = "C"
                if st == AssetStatus.SWAP_BUFFER:
                    grade = "A" if rng.random() < 0.6 else "B"
                if st == AssetStatus.READY_SECOND:
                    # refurbished for a second life: grade A or B only, in their share of the grade mix
                    grade = "A" if rng.random() < GRADE_MIX["A"] / (GRADE_MIX["A"] + GRADE_MIX["B"]) else "B"
                if st in (AssetStatus.RETURNED, AssetStatus.MDM_RELEASE):
                    grade = None                         # not graded yet
                aid = add_asset(code, purchase, status=st, cycle_no=cycle_done, current_location_id=stations[st].id,
                                received_date=purchase + timedelta(days=rng.randint(2, 6)), deployed_date=(start2 if cycle_done == 2 else start1),
                                status_since=status_since, grade=grade,
                                battery_health=round(max(0.6, 1.0 - 0.006 * ((today - purchase).days / DAYS_PER_MONTH) + rng.gauss(0, 0.03)), 3),
                                warranty_end_date=purchase + timedelta(days=730))
                reason1 = "defect" if defect else ("early" if (early and cycle_done == 1) else "planned")
                add_contract(aid, code, 1, start1, t1, end1, reason1, customer_for(start1))
                if cycle_done == 2:
                    add_contract(aid, code, 2, start2, t2, end2, ("early" if early else "planned"), customer_for(start2))

        # sold and recycled in the last twelve months (resale history for the recommerce KPIs)
        for kind, n in (("sold", N_SOLD_LAST_YEAR), ("recycled", N_RECYCLED_LAST_YEAR)):
            for _ in range(n):
                family = _pick(rng, FAMILY_MIX)
                sold_date = today - timedelta(days=rng.randint(1, 365))
                cycle_done = 2 if rng.random() < 0.55 else 1
                t1 = _pick(rng, TERM_MIX[family])
                t2 = _pick(rng, TERM_MIX_CYCLE2)
                return_date = sold_date - timedelta(days=rng.randint(15, 75))
                if cycle_done == 2:
                    end2 = return_date
                    start2 = end2 - timedelta(days=round(t2 * DAYS_PER_MONTH))
                    end1 = start2 - timedelta(days=rng.randint(20, 60))
                    start1 = end1 - timedelta(days=round(t1 * DAYS_PER_MONTH))
                else:
                    end1 = return_date
                    start1 = end1 - timedelta(days=round(t1 * DAYS_PER_MONTH))
                purchase = start1 - timedelta(days=rng.randint(7, 30))
                code = choose_product(family, purchase)
                purchase = max(purchase, products[code][3] + timedelta(days=14))
                start1 = max(start1, purchase + timedelta(days=7))
                end1 = max(end1, start1 + timedelta(days=30))
                if cycle_done == 2:
                    start2 = max(start2, end1 + timedelta(days=20))
                    end2 = max(end2, start2 + timedelta(days=30))
                    sold_date = max(sold_date, end2 + timedelta(days=15))
                grade = _pick(rng, GRADE_MIX) if kind == "sold" else "D"
                age_m = (sold_date - purchase).days / DAYS_PER_MONTH
                rrp = products[code][4]
                channel = _pick(rng, CHANNEL_MIX)
                price = round(rrp / (1 + VAT) * _residual_share(family, age_m, grade) * (1 - CHANNEL_FEE[channel]), 2) if kind == "sold" else None
                aid = add_asset(code, purchase, status=(AssetStatus.SOLD if kind == "sold" else AssetStatus.RECYCLED), cycle_no=cycle_done,
                                received_date=purchase + timedelta(days=rng.randint(2, 6)), deployed_date=(start2 if cycle_done == 2 else start1),
                                status_since=sold_date, grade=grade, sold_date=sold_date, sale_price=price, sale_channel=(channel if kind == "sold" else None),
                                battery_health=round(max(0.5, 1.0 - 0.006 * age_m + rng.gauss(0, 0.03)), 3), warranty_end_date=purchase + timedelta(days=730),
                                decommissioned_date=(sold_date if kind == "recycled" else None))
                add_contract(aid, code, 1, start1, t1, end1, "planned", customer_for(start1))
                if cycle_done == 2:
                    add_contract(aid, code, 2, start2, t2, end2, "planned", customer_for(start2))

        sink.flush()

        # --- close the order grid: real quantities, empty lines removed --------
        # Core table, not the mapped class: this is one executemany over known ids, not an ORM unit of work.
        _ITEM = OrderItem.__table__
        updates = [{"b_id": _line_id(code, ym), "q": n} for (code, ym), n in qty.items()]
        for i in range(0, len(updates), 500):
            db.execute(update(_ITEM).where(_ITEM.c.id == bindparam("b_id")).values(quantity=bindparam("q")), updates[i:i + 500])
        db.execute(delete(OrderItem).where(OrderItem.quantity == 0))
        db.commit()
        used_po = {pid for (pid,) in db.execute(select(OrderItem.order_id).distinct()).all()}
        empty = [r["id"] for r in po_rows if r["id"] not in used_po]
        for i in range(0, len(empty), 500):
            db.execute(delete(PurchaseOrder).where(PurchaseOrder.id.in_(empty[i:i + 500])))
        db.commit()

        # --- the next tranche: devices on order, not yet received --------------
        # A fleet that is scaling always has stock in the air. These lines are what the
        # Overview counts as inbound, what the capacity guard subtracts from free space,
        # and where the two late deliveries come from.
        inbound_pos, inbound_items = [], []
        plan = [("APL-IP16-128", 0.22), ("SAM-S25-128", 0.18), ("APL-IP16E-128", 0.14), ("SAM-A55-128", 0.12),
                ("GOO-PX9-128", 0.08), ("APL-MBA13-M3", 0.10), ("LEN-T14G5", 0.08), ("HP-EB840G10", 0.05), ("SAM-TABS9FE-128", 0.03)]
        eta_offsets = (-11, -4, 6, 13, 20, 27, 34, 48, 62)      # two lines are already late
        for j, (code, share) in enumerate(plan):
            p, family, oem, launch, rrp, price, supplier = products[code]
            units = int(N_INBOUND * share)
            if units <= 0:
                continue
            eta_offset = eta_offsets[j]
            po_id = str(uuid.uuid4())
            inbound_pos.append(dict(id=po_id, order_number=f"PO-{today.strftime('%Y%m')}-OPEN-{j + 1:02d}", status=OrderStatus.PLACED,
                                    supplier_id=supplier.id, destination_id=intake.id, currency_code="EUR",
                                    date_ordered=today - timedelta(days=21 + max(0, eta_offset) // 2)))
            inbound_items.append(dict(id=str(uuid.uuid4()), order_id=po_id, product_id=p.id, product_supplier_id=sources[code].id,
                                      quantity=units, unit_price=Decimal(str(price)),
                                      estimated_delivery_date=today + timedelta(days=eta_offset), actual_delivery_date=None))
        if inbound_pos:
            db.execute(insert(PurchaseOrder), inbound_pos)
            db.execute(insert(OrderItem), inbound_items)
            db.commit()

        # --- the control tower and the buyer's queue ---------------------------
        _seed_tracking(db, today)
        _stage_requisitions(db)

        # --- what was built ----------------------------------------------------
        growth = " | ".join(f"{k} {v:,}" for k, v in sorted(sink.first_rental_by_quarter.items())[-5:])
        print(f"DaaS fleet seeded: {sink.n_assets:,} serials - rented {sink.by_status[AssetStatus.RENTED]:,} "
              f"(second rental {sink.cycle2_rented:,}), warehouse {sum(sink.by_status[s] for s in WAREHOUSE_MIX):,}, "
              f"sold {sink.by_status[AssetStatus.SOLD]:,}, recycled {sink.by_status[AssetStatus.RECYCLED]:,}; "
              f"{sink.n_contracts:,} rental contracts; {len(po_rows) - len(empty):,} received orders, {len(qty):,} lines; "
              f"{len(inbound_pos)} open orders, {sum(r['quantity'] for r in inbound_items):,} devices inbound; scale {SCALE}")
        print(f"  first rentals started, last five quarters: {growth}")
    finally:
        db.close()


# Logistics origins for the control tower. Synthetic: a plausible European inbound lane
# per manufacturer, not a claim about where any company actually ships from.
LANES = {
    "APPLE": ("APPL", "IE", "air", "Dublin, IE"),
    "SAMSUNG": ("SMSG", "NL", "ocean", "Rotterdam, NL"),
    "GOOGLE": ("GOOG", "NL", "air", "Amsterdam, NL"),
    "FAIRPHONE": ("FRPH", "NL", "road", "Amsterdam, NL"),
    "LENOVO": ("LNVO", "DE", "road", "Stuttgart, DE"),
    "HP": ("HPQ", "DE", "road", "Ratingen, DE"),
    "RESELLER-A": ("RSLA", "DE", "road", "Munich, DE"),
    "RESELLER-B": ("RSLB", "DE", "road", "Hamburg, DE"),
}
CARRIER = {"air": "Lufthansa Cargo", "ocean": "Maersk", "road": "DB Schenker"}
N_DELIVERED_SHIPMENTS = 12          # how much recent history the control tower keeps


def _seed_tracking(db, today: date) -> None:
    """Shipments for the devices still in the air, plus the last few that landed.

    The control tower shows the same PO numbers, suppliers and values as Procurement and
    Inbound, because it is built from those orders rather than from a fixture. Only the
    open orders and the most recent deliveries become shipments: a scaling fleet has 180
    historic orders, and a tower that lists all of them shows nothing.
    """
    from datetime import datetime

    from app.models.catalog import Organization
    from app.models.tracking import Shipment, ShipmentEvent, TrkPurchaseOrder, TrkSupplier

    if db.scalar(select(Shipment).limit(1)):
        return
    orgs = {o.id: o for o in db.scalars(select(Organization)).all()}
    for code, (sid, country, _mode, _origin) in LANES.items():
        org = next((o for o in orgs.values() if o.code == code), None)
        if org is not None:
            db.add(TrkSupplier(supplier_id=sid, name=org.name, country=country, tier=1))
    db.flush()

    open_pos = db.scalars(select(PurchaseOrder).where(PurchaseOrder.status == OrderStatus.PLACED)).all()
    done_pos = db.scalars(select(PurchaseOrder).where(PurchaseOrder.status == OrderStatus.RECEIVED)
                          .order_by(PurchaseOrder.date_ordered.desc()).limit(N_DELIVERED_SHIPMENTS)).all()
    hub, dest = "Frankfurt hub, DE", "Central warehouse, DE"
    n = 0
    for po in list(open_pos) + list(done_pos):
        org = orgs.get(po.supplier_id)
        lane = LANES.get(org.code if org else "")
        if lane is None:
            continue
        sid, _country, mode, origin = lane
        items = db.scalars(select(OrderItem).where(OrderItem.order_id == po.id)).all()
        value = sum(float(i.quantity) * float(i.unit_price or 0) for i in items)
        eta = next((i.estimated_delivery_date for i in items if i.estimated_delivery_date), None)
        eta_current = eta
        if po.status == OrderStatus.RECEIVED:
            cur, idx, exc, reason = "delivered", 5, False, None
            steps = [("placed", origin), ("packed", origin), ("departed_origin", origin),
                     ("in_transit", hub), ("out_for_delivery", dest), ("delivered", dest)]
        elif eta and eta < today:
            # late: the two overdue lines of the next tranche are where the tower earns its keep
            cur, idx, exc, reason = "customs", 3, True, "Held - import documentation query"
            steps = [("placed", origin), ("packed", origin), ("departed_origin", origin), ("customs", hub)]
            eta_current = eta + timedelta(days=6)
        elif eta and (eta - today).days <= 14:
            cur, idx, exc, reason = "arrived_hub", 3, False, None
            steps = [("placed", origin), ("packed", origin), ("departed_origin", origin), ("arrived_hub", hub)]
        else:
            cur, idx, exc, reason = "in_transit", 2, False, None
            steps = [("placed", origin), ("packed", origin), ("in_transit", hub)]
        db.add(TrkPurchaseOrder(po_id=po.order_number, supplier_id=sid, order_date=po.date_ordered,
                                expected_delivery=eta, total_value=value,
                                status="closed" if po.status == OrderStatus.RECEIVED else "open"))
        ship_id = f"SHP-{n + 1:04d}"
        db.add(Shipment(shipment_id=ship_id, po_id=po.order_number, mode=mode, carrier=CARRIER[mode],
                        current_status=cur, progress_idx=idx, current_location=steps[-1][1],
                        last_event_at=datetime(today.year, today.month, today.day),
                        eta_original=eta, eta_current=eta_current,
                        exception_flag=exc, exception_reason=reason))
        for seq, (status, loc) in enumerate(steps, start=1):
            db.add(ShipmentEvent(shipment_id=ship_id, seq=seq, status=status, location_name=loc,
                                 event_ts=datetime(today.year, today.month, max(1, seq)),
                                 notes=f"{status.replace('_', ' ').title()} - {loc}"))
        n += 1
    db.commit()
    print(f"  tracking: {n} shipments from the real orders")


def _stage_requisitions(db) -> None:
    """Run the purchasing agent once so the buyer's queue is not empty on day one.

    The same ``run_requisition_cycle`` the agent runs on a schedule, deterministically
    (no LLM call, no token cost at boot), so the queue is what a real run produces.
    """
    try:
        from app.agent import purchasing
        res = purchasing.run_requisition_cycle(db, period_days=7, actor="seed", use_llm=False)
        db.commit()
        print(f"  requisitions: staged {res['staged']} (auto-placed {res['auto_placed']}) from the demand of a growing fleet")
    except Exception as exc:  # noqa: BLE001 - the queue is a convenience, never a reason to fail the seed
        db.rollback()
        print(f"  requisitions: skipped ({type(exc).__name__}: {exc})")


if __name__ == "__main__":
    from app.core.safety import should_seed_demo

    if should_seed_demo():
        seed_daas()

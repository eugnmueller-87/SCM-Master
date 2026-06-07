"""Demo dataset — a lived-in operation so every screen is populated.

Builds a believable Frankfurt-DC hardware operation through the REAL services
(same rules as the API), so the data is internally consistent:
  - 6 suppliers/manufacturers, 6 products across categories, multi-sourced;
  - sourcing contracts at varied lifecycle states (active / renewal-due /
    expiring / expired / draft) with annual budgets;
  - locations including a near-full rack and an OVER-capacity staging cage;
  - purchase orders spanning every status (pending → approved → placed →
    partially/received, plus cancelled), with some overdue inbound lines;
  - assets received and driven through the full lifecycle: in storage, deployed,
    in maintenance, decommissioned, disposed — so Overview / Assets / Spend /
    Inventory / Capacity all show real distributions;
  - the logistics control-tower data (via seed_tracking).

Run on a FRESH database (from backend/):
    .venv\\Scripts\\alembic upgrade head
    .venv\\Scripts\\python -m app.seed_demo

Idempotent: bails out if the catalog is already populated.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select

from app.core.db import SessionLocal
from app.core.safety import assert_seeding_allowed
from app.models.auth import Role
from app.models.catalog import Product
from app.models.flow import AssetStatus, LocationType
from app.models.procurement import OrderStatus
from app.services.asset import asset_service
from app.services.auth import ensure_user
from app.services.catalog import (
    organization_service,
    product_service,
    product_supplier_service,
)
from app.services.flow import location_service
from app.services.procurement import purchase_order_service

TODAY = date(2026, 6, 1)


def _po_number(n: int) -> str:
    return f"PO-2026-{n:04d}"


def seed_demo() -> None:
    assert_seeding_allowed("demo dataset")  # forge-lock: never seed in prod
    db = SessionLocal()
    try:
        if db.scalar(select(Product).limit(1)):
            print("Catalog already populated — skipping demo seed.")
            return

        # --- Users across roles (so role-gating can be demoed) ------------
        # ensure_user (not create_user): the boot step may already have created
        # admin/guest, and re-running must not raise on the existing rows.
        ensure_user(db, email="admin@example.com", full_name="Anders Mohr",
                    password="admin", role=Role.ADMIN)  # nosec B106 — demo-only seed creds; never run in prod (forge-lock)
        ensure_user(db, email="buyer@example.com", full_name="Pia Schulz",
                    password="buyer", role=Role.PROCUREMENT)  # nosec B106 — demo-only seed creds; never run in prod (forge-lock)
        ensure_user(db, email="warehouse@example.com", full_name="Tomas Reuter",
                    password="whse", role=Role.WAREHOUSE)  # nosec B106 — demo-only seed creds; never run in prod (forge-lock)
        ensure_user(db, email="dc@example.com", full_name="Lena Brandt",
                    password="dc", role=Role.DATACENTER)  # nosec B106 — demo-only seed creds; never run in prod (forge-lock)
        # A read-only guest for the public demo's "Explore as guest" button.
        ensure_user(db, email="guest@example.com", full_name="Demo Guest",
                    password="guest", role=Role.VIEWER)  # nosec B106 — demo-only seed creds; never run in prod (forge-lock)

        # --- Organizations ------------------------------------------------
        dell = organization_service.create(db, dict(code="DELL", name="Dell Technologies", is_supplier=True, is_manufacturer=True))
        smci = organization_service.create(db, dict(code="SMCI", name="Supermicro", is_supplier=True, is_manufacturer=True))
        tdsynnex = organization_service.create(db, dict(code="TDS", name="TD Synnex", is_supplier=True, is_manufacturer=False))
        arrow = organization_service.create(db, dict(code="ARW", name="Arrow Electronics", is_supplier=True, is_manufacturer=False))
        samsung = organization_service.create(db, dict(code="SSNLF", name="Samsung Semiconductor", is_supplier=True, is_manufacturer=True))
        intel = organization_service.create(db, dict(code="INTC", name="Intel", is_supplier=False, is_manufacturer=True))

        # --- Products -----------------------------------------------------
        srv = product_service.create(db, dict(product_code="DELL-R760", name="PowerEdge R760 · 2U server", category="Servers", description="2U dual-socket server"))
        cpu = product_service.create(db, dict(product_code="AMD-9554", name="EPYC 9554 · 64-core CPU", category="Processors", description="64-core server CPU"))
        dimm = product_service.create(db, dict(product_code="SEC-M321R", name="64GB DDR5-4800 RDIMM", category="Memory", description="Registered DDR5"))
        jbod = product_service.create(db, dict(product_code="SMC-SC847", name="SC847 · 4U JBOD chassis", category="Storage", description="44-bay JBOD"))
        nic = product_service.create(db, dict(product_code="NVDA-CX7", name="ConnectX-7 200G NIC", category="Networking", description="200GbE adapter"))
        psu = product_service.create(db, dict(product_code="PSU-2400T", name="2400W Titanium PSU", category="Power", description="Hot-swap PSU"))

        # --- Sourcing contracts (varied lifecycle states + budgets) -------
        def src(product, supplier, manufacturer, price, lead, moq, rank, **kw):
            return product_supplier_service.create(db, dict(
                product_id=product.id, supplier_id=supplier.id,
                manufacturer_id=(manufacturer.id if manufacturer else None),
                contract_price=Decimal(price), standard_lead_time_days=lead,
                min_order_quantity=moq, preference_rank=rank, **kw))

        # server: Dell (preferred, active) + TD Synnex (renewal due soon)
        ps_srv_dell = src(srv, dell, dell, "8420.00", 21, 1, 1,
                          supplier_product_code="DELL-R760-EU", contract_status="ACTIVE",
                          term_start=date(2025, 1, 1), term_end=date(2026, 12, 31),
                          annual_budget="420000.00")
        src(srv, tdsynnex, dell, "8690.00", 14, 1, 2, supplier_product_code="TDS-R760",
            term_start=date(2024, 7, 1), term_end=TODAY + timedelta(days=40),  # -> RENEWAL_DUE
            annual_budget="150000.00")
        # cpu: Arrow (active) + TD Synnex (expiring within 2 weeks)
        ps_cpu_arrow = src(cpu, arrow, intel, "7120.00", 35, 4, 1, supplier_product_code="ARW-9554",
                           contract_status="ACTIVE", term_start=date(2025, 3, 1), term_end=date(2027, 2, 28),
                           annual_budget="300000.00")
        src(cpu, tdsynnex, intel, "7350.00", 28, 2, 2, supplier_product_code="TDS-9554",
            term_start=date(2024, 1, 1), term_end=TODAY + timedelta(days=14),  # -> EXPIRING
            annual_budget="90000.00")
        # dimm: Samsung (active, single source — concentration) + expired backup
        ps_dimm_ss = src(dimm, samsung, samsung, "430.00", 18, 16, 1, supplier_product_code="SEC-M321R",
                         contract_status="ACTIVE", term_start=date(2025, 6, 1), term_end=date(2027, 5, 31),
                         annual_budget="80000.00")
        src(dimm, tdsynnex, samsung, "455.00", 12, 8, 2, supplier_product_code="TDS-DDR5",
            active=False, term_start=date(2023, 1, 1), term_end=date(2025, 12, 31))  # -> EXPIRED
        # jbod: Supermicro (draft contract, no terms yet)
        ps_jbod = src(jbod, smci, smci, "3180.00", 25, 1, 1, supplier_product_code="SMC-SC847")
        # nic + psu: single active sources
        ps_nic = src(nic, arrow, intel, "1180.00", 14, 2, 1, contract_status="ACTIVE",
                     term_start=date(2025, 1, 1), term_end=date(2026, 12, 31), annual_budget="60000.00")
        ps_psu = src(psu, dell, dell, "540.00", 16, 4, 1, contract_status="ACTIVE",
                     term_start=date(2025, 1, 1), term_end=date(2026, 12, 31), annual_budget="50000.00")

        # --- Locations (incl. a near-full rack + an over-capacity cage) ---
        wh = location_service.create(db, dict(code="TRANSIT-WH", name="Transit warehouse", location_type=LocationType.WAREHOUSE, capacity=200))
        cage = location_service.create(db, dict(code="CAGE-T1", name="Inbound staging cage", location_type=LocationType.WAREHOUSE, capacity=6))
        dc = location_service.create(db, dict(code="DC-FRA1", name="Frankfurt DC", location_type=LocationType.DATACENTER, capacity=168))
        # Rack capacity counts mounted units (servers + components racked into them).
        rack_a = location_service.create(db, dict(code="RACK-A12", name="Rack A12", location_type=LocationType.RACK, parent_id=dc.id, capacity=120))
        rack_b = location_service.create(db, dict(code="RACK-B07", name="Rack B07", location_type=LocationType.RACK, parent_id=dc.id, capacity=100))

        # --- Helper: place a PO and optionally receive + drive lifecycle --
        po_counter = [30]

        def make_po(supplier, lines, *, status=OrderStatus.PENDING, eta_days=None, ordered_days_ago=20):
            po_counter[0] += 1
            items = []
            for product, source, qty in lines:
                eta = (TODAY + timedelta(days=eta_days)) if eta_days is not None else None
                items.append(dict(product_id=product.id, product_supplier_id=source.id,
                                  quantity=qty, unit_price=source.contract_price,
                                  estimated_delivery_date=eta))
            po = purchase_order_service.create(db, dict(
                order_number=_po_number(po_counter[0]), supplier_id=supplier.id,
                destination_id=wh.id, date_ordered=TODAY - timedelta(days=ordered_days_ago),
                items=items))
            # advance status through the legal chain up to the requested one
            chain = [OrderStatus.APPROVED, OrderStatus.PLACED]
            for st in chain:
                if status in (OrderStatus.APPROVED, OrderStatus.PLACED,
                              OrderStatus.PARTIALLY_RECEIVED, OrderStatus.RECEIVED) and \
                   _rank(st) <= _rank(status):
                    purchase_order_service.set_status(db, po.id, st)
            if status == OrderStatus.CANCELLED:
                purchase_order_service.set_status(db, po.id, OrderStatus.CANCELLED)
            return po

        def receive(po, line_qtys, *, days_ago=10, location=None):
            lines = [{"order_item_id": oi.id, "quantity": q}
                     for oi, q in zip(po.items, line_qtys) if q > 0]
            return asset_service.receive(db, po.id, location_id=(location or wh).id,
                                         lines=lines, receipt_date=TODAY - timedelta(days=days_ago),
                                         actor="warehouse")

        def drive(product, status, n, location, *, note=None):
            """Push n on-hand assets of a product to `status` via legal transitions."""
            assets = [a for a in asset_service.list(db, status=AssetStatus.RECEIVED, limit=1000)
                      if a.product_id == product.id][:n]
            path = {
                AssetStatus.IN_STORAGE: [AssetStatus.IN_STORAGE],
                AssetStatus.DEPLOYED: [AssetStatus.IN_STORAGE, AssetStatus.DEPLOYED],
                AssetStatus.MAINTENANCE: [AssetStatus.IN_STORAGE, AssetStatus.DEPLOYED, AssetStatus.MAINTENANCE],
                AssetStatus.DECOMMISSIONED: [AssetStatus.IN_STORAGE, AssetStatus.DEPLOYED, AssetStatus.DECOMMISSIONED],
                AssetStatus.DISPOSED: [AssetStatus.IN_STORAGE, AssetStatus.DEPLOYED, AssetStatus.DECOMMISSIONED, AssetStatus.DISPOSED],
            }[status]
            for a in assets:
                for step in path:
                    loc = location if step == AssetStatus.DEPLOYED else None
                    asset_service.transition(db, a.id, step, location_id=(loc.id if loc else None),
                                             actor="dc", note=note)

        # --- PO 1: fully received servers, mostly deployed ----------------
        po1 = make_po(dell, [(srv, ps_srv_dell, 12)], status=OrderStatus.PLACED, eta_days=-5, ordered_days_ago=40)
        receive(po1, [12], days_ago=30)
        drive(srv, AssetStatus.DEPLOYED, 8, rack_a)        # 8 deployed
        drive(srv, AssetStatus.MAINTENANCE, 1, rack_a)     # 1 in maintenance
        drive(srv, AssetStatus.IN_STORAGE, 2, wh)          # 2 staged
        # (1 left RECEIVED on the floor)

        # --- PO 2: CPUs partially received, some deployed -----------------
        po2 = make_po(arrow, [(cpu, ps_cpu_arrow, 20)], status=OrderStatus.PLACED, eta_days=12, ordered_days_ago=25)
        receive(po2, [12], days_ago=18)                    # 12 of 20 in -> PARTIALLY_RECEIVED
        drive(cpu, AssetStatus.DEPLOYED, 9, rack_b)
        drive(cpu, AssetStatus.IN_STORAGE, 2, wh)

        # --- PO 3: DIMMs fully received, heavily deployed -----------------
        po3 = make_po(samsung, [(dimm, ps_dimm_ss, 96)], status=OrderStatus.PLACED, eta_days=-8, ordered_days_ago=35)
        receive(po3, [96], days_ago=22)
        drive(dimm, AssetStatus.DEPLOYED, 70, rack_b)      # concentration + fills rack B
        drive(dimm, AssetStatus.IN_STORAGE, 20, wh)

        # --- PO 4: JBOD, one decommissioned + one disposed (refresh) ------
        po4 = make_po(smci, [(jbod, ps_jbod, 6)], status=OrderStatus.RECEIVED, eta_days=-60, ordered_days_ago=120)
        receive(po4, [6], days_ago=110)
        drive(jbod, AssetStatus.DEPLOYED, 3, rack_a)
        drive(jbod, AssetStatus.DECOMMISSIONED, 1, rack_a)
        drive(jbod, AssetStatus.DISPOSED, 1, rack_a)

        # --- PO 5: OVERDUE placed order (no receipts) — Inbound flags it ---
        make_po(arrow, [(cpu, ps_cpu_arrow, 6)], status=OrderStatus.PLACED, eta_days=-10, ordered_days_ago=45)

        # --- PO 6: approved (awaiting placement) --------------------------
        make_po(dell, [(nic, ps_nic, 8)], status=OrderStatus.APPROVED, eta_days=25, ordered_days_ago=5)

        # --- PO 7: pending PSUs (fresh draft) -----------------------------
        make_po(dell, [(psu, ps_psu, 40)], status=OrderStatus.PENDING, eta_days=18, ordered_days_ago=2)

        # --- PO 8: cancelled --------------------------------------------
        make_po(tdsynnex, [(srv, ps_srv_dell, 2)], status=OrderStatus.CANCELLED, ordered_days_ago=15)

        # --- Push the staging cage over capacity (cap 6) ------------------
        cage_assets = [a for a in asset_service.list(db, status=AssetStatus.IN_STORAGE, limit=1000)][:7]
        for a in cage_assets:
            asset_service.move(db, a.id, cage.id, actor="warehouse")

        # --- Should-cost: commodities + classes + a BOM for the R760 ------
        from app.seed_costing import seed_costing
        seed_costing(db)

        db.commit()

        # --- Control-tower shipments derived from the REAL POs ------------
        # So Tracking reconciles with Procurement/Inbound (same PO numbers,
        # suppliers and values) rather than a disjoint sample set.
        _seed_tracking_from_pos(db)

        # --- Summary ------------------------------------------------------
        from app.models.flow import Asset
        counts = {s.value: db.scalar(select(__import__("sqlalchemy").func.count(Asset.id)).where(Asset.status == s)) for s in AssetStatus}
        print("Demo seed complete:")
        print("  users        : admin/buyer/warehouse/dc (pw = role)")
        print("  organizations: 6   products: 6   contracts: 9")
        print("  locations    : 5 (warehouse, staging cage [over-cap], DC, 2 racks)")
        print("  purchase orders: 8 across PENDING/APPROVED/PLACED/PARTIALLY_RECEIVED/RECEIVED/CANCELLED")
        print(f"  assets by status: {counts}")
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _rank(status: OrderStatus) -> int:
    order = [OrderStatus.PENDING, OrderStatus.APPROVED, OrderStatus.PLACED,
             OrderStatus.PARTIALLY_RECEIVED, OrderStatus.RECEIVED]
    return order.index(status) if status in order else -1


def _seed_tracking_from_pos(db) -> None:
    """Build control-tower shipments from the demo's real procurement POs.

    Each open/placed PO (those with goods actually moving) becomes a tracking PO
    + a shipment with a milestone trail keyed to its situation, so the Tracking
    screen shows the SAME PO numbers / suppliers / values as Procurement and
    Inbound. A couple get a richer logistics story (an ocean customs hold, a
    delivered road shipment) for the walkthrough.
    """
    from datetime import datetime

    from app.models.catalog import Organization
    from app.models.procurement import OrderItem, PurchaseOrder
    from app.models.tracking import Shipment, ShipmentEvent, TrkPurchaseOrder, TrkSupplier

    if db.scalar(select(Shipment).limit(1)):
        return

    # Suppliers referenced by the demo POs (country + a transport mode/origin).
    SUP = {
        "Dell Technologies": ("DELL", "IE", "air", "Dublin, IE"),
        "Supermicro": ("SMCI", "NL", "ocean", "Rotterdam, NL"),
        "TD Synnex": ("TDS", "DE", "road", "Munich, DE"),
        "Arrow Electronics": ("ARW", "US", "air", "Denver, US"),
        "Samsung Semiconductor": ("SSNLF", "KR", "ocean", "Busan, KR"),
    }
    for name, (sid, country, _mode, _origin) in SUP.items():
        db.add(TrkSupplier(supplier_id=sid, name=name, country=country, tier=1))
    db.flush()

    # status -> (shipment current_status, progress_idx, exception, milestone path)
    def trail(po, sup_name, value):
        sid, country, mode, origin = SUP[sup_name]
        st = po.status
        hub = "Frankfurt hub, DE"
        dest = "Frankfurt DC, DE"
        if st == OrderStatus.RECEIVED:
            cur, idx, exc, reason = "delivered", 5, False, None
            steps = [("placed", origin), ("packed", origin), ("departed_origin", origin),
                     ("in_transit", hub), ("out_for_delivery", dest), ("delivered", dest)]
        elif st == OrderStatus.PARTIALLY_RECEIVED:
            cur, idx, exc, reason = "out_for_delivery", 4, False, None
            steps = [("placed", origin), ("packed", origin), ("departed_origin", origin),
                     ("arrived_hub", hub), ("out_for_delivery", dest)]
        else:  # PLACED
            # the overdue one (eta in the past) gets a customs hold exception
            overdue = po.items and po.items[0].estimated_delivery_date and po.items[0].estimated_delivery_date < TODAY
            if overdue and mode == "air":
                cur, idx, exc, reason = "customs", 3, True, "Held — import documentation query"
                steps = [("placed", origin), ("packed", origin), ("departed_origin", origin), ("customs", hub)]
            else:
                cur, idx, exc, reason = "in_transit", 2, False, None
                steps = [("placed", origin), ("packed", origin), ("in_transit", hub)]
        return sid, mode, cur, idx, exc, reason, steps

    moving = {OrderStatus.PLACED, OrderStatus.PARTIALLY_RECEIVED, OrderStatus.RECEIVED}
    pos = db.scalars(select(PurchaseOrder)).all()
    n = 0
    for po in pos:
        if po.status not in moving:
            continue
        sup = db.get(Organization, po.supplier_id)
        if not sup or sup.name not in SUP:
            continue
        # PO value = sum(line qty x unit price)
        value = 0.0
        eta_o = eta_c = None
        for oi in db.scalars(select(OrderItem).where(OrderItem.order_id == po.id)).all():
            value += float(oi.quantity) * float(oi.unit_price or 0)
            if oi.estimated_delivery_date:
                eta_o = oi.estimated_delivery_date
                eta_c = oi.estimated_delivery_date
        sid, mode, cur, idx, exc, reason, steps = trail(po, sup.name, value)
        # a delayed current ETA on exceptions
        if exc and eta_c:
            eta_c = eta_c + timedelta(days=4)
        db.add(TrkPurchaseOrder(po_id=po.order_number, supplier_id=sid,
                                order_date=po.date_ordered, expected_delivery=eta_o,
                                total_value=value, status="open"))
        ship_id = f"SHP-{po.order_number[-4:]}"
        last = steps[-1]
        db.add(Shipment(shipment_id=ship_id, po_id=po.order_number, mode=mode,
                        carrier={"air": "Lufthansa Cargo", "ocean": "Maersk", "road": "DB Schenker"}[mode],
                        current_status=cur, progress_idx=idx, current_location=last[1],
                        last_event_at=datetime(TODAY.year, TODAY.month, TODAY.day),
                        eta_original=eta_o, eta_current=eta_c,
                        exception_flag=exc, exception_reason=reason))
        for seq, (status, loc) in enumerate(steps, start=1):
            db.add(ShipmentEvent(shipment_id=ship_id, seq=seq, status=status,
                                 location_name=loc,
                                 event_ts=datetime(TODAY.year, TODAY.month, max(1, seq)),
                                 notes=f"{status.replace('_', ' ').title()} — {loc}"))
        n += 1
    db.commit()
    print(f"  tracking      : {n} shipments derived from real POs")


if __name__ == "__main__":
    # Self-wiring demo: seed unless this is production (forge-locked) or the
    # operator opted out with SEED_DEMO=0. Idempotent on an already-seeded DB.
    from app.core.safety import should_seed_demo

    if should_seed_demo():
        seed_demo()
    else:
        print("Skipping demo seed (production, or SEED_DEMO=0).")

"""Seed a realistic hardware scenario for local dev and demos.

Run (from backend/, after ``alembic upgrade head``):
    .venv\\Scripts\\python -m app.seed

It builds the whole picture the lifecycle work in Phase 2 will operate on:
  - manufacturers and multi-sourced suppliers;
  - a small server/CPU/storage/NIC catalog, each product offered by 2-3 sources
    at different lead times, MOQs, prices, and preference ranks;
  - a transit warehouse plus a datacenter with two racks;
  - one PENDING purchase order with several lines, each pointing at a chosen
    source — the buy that Phase 2 will receive into live assets.

Idempotent: it goes through the service layer (same rules as the API) and bails
out early if the catalog is already populated, so re-running is safe.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select

from app.core.db import SessionLocal
from app.models.auth import Role
from app.models.catalog import Product
from app.models.flow import LocationType
from app.services.auth import user_service
from app.services.catalog import (
    organization_service,
    product_service,
    product_supplier_service,
)
from app.services.flow import location_service
from app.services.procurement import purchase_order_service

TODAY = date(2026, 6, 1)


def seed() -> None:
    db = SessionLocal()
    try:
        if db.scalar(select(Product).limit(1)):
            print("Catalog already populated — skipping seed.")
            return

        # --- A bootstrap admin (log in, then create other users via the API) --
        user_service.create_user(
            db, email="admin@example.com", full_name="Admin",
            password="admin", role=Role.ADMIN)

        # --- Organizations: manufacturers and suppliers -------------------
        supermicro = organization_service.create(db, dict(
            code="SMCI", name="Supermicro", is_supplier=True, is_manufacturer=True))
        dell = organization_service.create(db, dict(
            code="DELL", name="Dell Technologies", is_supplier=True, is_manufacturer=True))
        intel = organization_service.create(db, dict(
            code="INTC", name="Intel", is_supplier=False, is_manufacturer=True))
        samsung = organization_service.create(db, dict(
            code="SSNLF", name="Samsung", is_supplier=False, is_manufacturer=True))
        cdw = organization_service.create(db, dict(
            code="CDW", name="CDW (reseller)", is_supplier=True, is_manufacturer=False))

        # --- Products: the supplier-independent specs ---------------------
        server = product_service.create(db, dict(
            product_code="SRV-1U-AS1015", name="Supermicro AS-1015 1U Server",
            category="server", description="1U, 64GB, 2x NVMe"))
        cpu = product_service.create(db, dict(
            product_code="CPU-XEON-6430", name="Intel Xeon Gold 6430",
            category="cpu", description="32-core server CPU"))
        ssd = product_service.create(db, dict(
            product_code="SSD-NVME-3T84", name="Samsung PM9A3 3.84TB NVMe",
            category="storage", description="U.2 enterprise NVMe"))
        nic = product_service.create(db, dict(
            product_code="NIC-25G-X710", name="Intel X710 25GbE NIC",
            category="network", description="Dual-port 25GbE"))

        # --- ProductSuppliers: 2-3 sources per product (multi-sourcing) ---
        # server: Supermicro direct (preferred) vs CDW reseller
        ps_server_smci = product_supplier_service.create(db, dict(
            product_id=server.id, supplier_id=supermicro.id, manufacturer_id=supermicro.id,
            supplier_product_code="AS-1015-STD", standard_lead_time_days=21,
            min_order_quantity=1, contract_price=Decimal("3200.00"), preference_rank=1,
            contract_status="ACTIVE", term_start=date(2025, 1, 1), term_end=date(2026, 12, 31),
            annual_budget=Decimal("120000.00")))
        product_supplier_service.create(db, dict(
            product_id=server.id, supplier_id=cdw.id, manufacturer_id=supermicro.id,
            supplier_product_code="CDW-AS1015", standard_lead_time_days=35,
            min_order_quantity=5, contract_price=Decimal("3450.00"), preference_rank=2))

        # cpu: Dell (preferred) vs CDW, both Intel-made
        ps_cpu_dell = product_supplier_service.create(db, dict(
            product_id=cpu.id, supplier_id=dell.id, manufacturer_id=intel.id,
            manufacturer_part_number="PK8071305120802", standard_lead_time_days=45,
            min_order_quantity=10, contract_price=Decimal("2100.00"), preference_rank=1))
        product_supplier_service.create(db, dict(
            product_id=cpu.id, supplier_id=cdw.id, manufacturer_id=intel.id,
            standard_lead_time_days=60, min_order_quantity=1,
            contract_price=Decimal("2250.00"), preference_rank=2))

        # ssd: CDW (preferred) vs Dell, both Samsung-made
        ps_ssd_cdw = product_supplier_service.create(db, dict(
            product_id=ssd.id, supplier_id=cdw.id, manufacturer_id=samsung.id,
            standard_lead_time_days=14, min_order_quantity=4,
            contract_price=Decimal("410.00"), preference_rank=1))
        product_supplier_service.create(db, dict(
            product_id=ssd.id, supplier_id=dell.id, manufacturer_id=samsung.id,
            standard_lead_time_days=20, min_order_quantity=2,
            contract_price=Decimal("440.00"), preference_rank=2))

        # nic: Dell only
        product_supplier_service.create(db, dict(
            product_id=nic.id, supplier_id=dell.id, manufacturer_id=intel.id,
            standard_lead_time_days=30, min_order_quantity=2,
            contract_price=Decimal("520.00"), preference_rank=1))

        # --- Locations: transit warehouse + datacenter with racks ---------
        warehouse = location_service.create(db, dict(
            code="WH-TRANSIT", name="Transit Warehouse",
            location_type=LocationType.WAREHOUSE, capacity=200))
        dc = location_service.create(db, dict(
            code="DC-FRA1", name="Frankfurt DC 1", location_type=LocationType.DATACENTER))
        location_service.create(db, dict(
            code="DC-FRA1-R01", name="Rack 01", location_type=LocationType.RACK,
            parent_id=dc.id, capacity=42))
        location_service.create(db, dict(
            code="DC-FRA1-R02", name="Rack 02", location_type=LocationType.RACK,
            parent_id=dc.id, capacity=42))

        # --- A PENDING purchase order against the preferred sources -------
        order = purchase_order_service.create(db, dict(
            order_number="PO-2026-0001", supplier_id=supermicro.id,
            destination_id=warehouse.id, date_ordered=TODAY,
            items=[
                dict(product_id=server.id, product_supplier_id=ps_server_smci.id,
                     quantity=10, unit_price=Decimal("3200.00"),
                     estimated_delivery_date=TODAY + timedelta(days=21)),
                dict(product_id=cpu.id, product_supplier_id=ps_cpu_dell.id,
                     quantity=20, unit_price=Decimal("2100.00"),
                     estimated_delivery_date=TODAY + timedelta(days=45)),
                dict(product_id=ssd.id, product_supplier_id=ps_ssd_cdw.id,
                     quantity=40, unit_price=Decimal("410.00"),
                     estimated_delivery_date=TODAY + timedelta(days=14)),
            ],
        ))

        db.commit()
        print("Seed complete:")
        print("  admin user    : admin@example.com / admin (ADMIN)")
        print("  organizations : 5")
        print("  products      : 4")
        print("  product sources (multi-sourcing): 7")
        print("  locations     : 4 (warehouse, datacenter, 2 racks)")
        print(f"  purchase order: {order.order_number} ({len(order.items)} lines, status {order.status.value})")
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

    # Logistics control-tower data (separate schema; idempotent).
    from app.seed_tracking import seed_tracking
    seed_tracking()


if __name__ == "__main__":
    seed()

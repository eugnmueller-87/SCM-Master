"""Seed the costing domain — commodity catalog, component-class taxonomy, a
12-month price series per commodity (with one deliberate memory spike), and a
BOM for a seeded server product.

Synthetic + reproducible (deterministic values, no randomness), matching the
synthetic-data discipline of the rest of the demo. Idempotent: bails if a
commodity catalog already exists.

Driven by docs/should_cost_model.md §9 (commodity list + class methods) and the
hero worked example §8a.

Run after the catalog exists (seed.py / seed_demo.py call seed_costing()).
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.catalog import Organization, Product, ProductSupplier
from app.models.costing import (
    BOM,
    BOMLine,
    Commodity,
    CommodityPrice,
    ComponentClass,
    CostingMethod,
    CostParams,
)

# Commodity catalog (spec §9). Baselines normalised to 1.0 so the multiplier is
# transparent; the spike lives in the price series, not the baseline.
_COMMODITIES = [
    ("DRAM_DDR5", "DDR5 DRAM", "index"),
    ("NAND_TLC", "TLC NAND", "index"),
    ("STEEL_CR", "Cold-rolled steel", "index"),
    ("ALU_LME", "Aluminium (LME)", "index"),
    ("COPPER_LME", "Copper (LME)", "index"),
    ("PCB_LABOR", "PCB / assembly labour", "index"),
]

# Component-class taxonomy → method + commodity driver (spec §9).
_CLASSES = [
    ("MEMORY", "Memory", CostingMethod.teardown, "DRAM_DDR5"),
    ("STORAGE", "Storage", CostingMethod.teardown, "NAND_TLC"),
    ("CHASSIS", "Chassis", CostingMethod.teardown, "STEEL_CR"),
    ("PSU", "Power supply", CostingMethod.teardown, "COPPER_LME"),
    ("MOTHERBOARD", "Motherboard", CostingMethod.teardown, "PCB_LABOR"),
    ("NIC", "Network card", CostingMethod.teardown, "PCB_LABOR"),
    ("CPU", "Processor", CostingMethod.reference_price, None),
    ("GPU", "Graphics/accelerator", CostingMethod.reference_price, None),
]

# 12 month-ends ending at the demo's fixed "today" (2026-06-01).
_BASE_MONTH = date(2026, 6, 1)


def _months_back(n: int) -> date:
    """Approximate month-end n months before _BASE_MONTH (1st of each month)."""
    total = (_BASE_MONTH.year * 12 + (_BASE_MONTH.month - 1)) - n
    return date(total // 12, total % 12 + 1, 1)


# Should-cost demo products — a SPREAD of config shapes so the cockpit's
# should-cost page is worth switching around: from a lean flash JBOF (~5% gap)
# to an over-priced memory appliance (~23% gap). Each: (code, name, category,
# description, quote €, [(class, label, qty, kwargs)…]). Quotes are hand-set
# against the base-month indices (DRAM ×1.8, NAND ×1.5) to give varied gaps.
def _td(base, conv, ovh):
    return dict(base_material_cost=base, conversion_cost=conv, overhead_pct=Decimal(str(ovh)))


def _ref(lst, disc):
    return dict(list_price=lst, discount_pct=Decimal(str(disc)))


_DEMO_BOMS = [
    ("SCN-STORAGE-1", "Storage Node SN-1 · 4U appliance", "Storage",
     "DRAM/NVMe-dense storage appliance (should-cost hero config)", "24650.00", [
         ("MEMORY", "64GB DDR5 RDIMM ×24", 24, _td(110, 6, 0.12)),
         ("STORAGE", "NVMe TLC module ×24", 24, _td(90, 4, 0.10)),
         ("CHASSIS", "4U chassis ×1", 1, _td(180, 40, 0.10)),
         ("PSU", "2400W PSU ×2", 2, _td(120, 18, 0.10)),
         ("MOTHERBOARD", "Mainboard ×1", 1, _td(420, 60, 0.10)),
         ("NIC", "ConnectX-7 ×1", 1, _td(700, 40, 0.10)),
         ("CPU", "EPYC 9554 ×1", 1, _ref(7120, 0.22)),
     ]),
    ("SCN-COMPUTE-1", "Compute Node CN-1 · GPU server", "Servers",
     "GPU/CPU-heavy compute node — silicon-dominated (low commodity exposure)", "29900.00", [
         ("MEMORY", "64GB DDR5 RDIMM ×8", 8, _td(110, 6, 0.12)),
         ("CHASSIS", "2U chassis ×1", 1, _td(140, 30, 0.10)),
         ("PSU", "2400W PSU ×2", 2, _td(120, 18, 0.10)),
         ("NIC", "ConnectX-7 ×1", 1, _td(700, 40, 0.10)),
         ("CPU", "EPYC 9554 ×2", 2, _ref(7120, 0.22)),
         ("GPU", "L40S ×1", 1, _ref(6800, 0.0)),  # allocation market — 0% band-bottom
     ]),
    ("SCN-MEM-APPLIANCE", "Memory Appliance MA-1 · in-memory node", "Memory",
     "Memory-maxed node — fattest negotiation target (over-priced vs floor)", "23900.00", [
         ("MEMORY", "64GB DDR5 RDIMM ×32", 32, _td(110, 6, 0.12)),
         ("CHASSIS", "2U chassis ×1", 1, _td(140, 30, 0.10)),
         ("PSU", "2400W PSU ×2", 2, _td(120, 18, 0.10)),
         ("MOTHERBOARD", "Mainboard ×1", 1, _td(420, 60, 0.10)),
         ("NIC", "ConnectX-7 ×1", 1, _td(700, 40, 0.10)),
         ("CPU", "EPYC 9554 ×1", 1, _ref(7120, 0.22)),
     ]),
    ("SCN-EDGE-1", "Edge Node EN-1 · 1U balanced", "Servers",
     "Balanced 1U edge node — small, well-priced (lean gap)", "8650.00", [
         ("MEMORY", "64GB DDR5 RDIMM ×4", 4, _td(110, 6, 0.12)),
         ("STORAGE", "NVMe TLC module ×4", 4, _td(90, 4, 0.10)),
         ("CHASSIS", "1U chassis ×1", 1, _td(120, 30, 0.10)),
         ("PSU", "1200W PSU ×1", 1, _td(120, 18, 0.10)),
         ("MOTHERBOARD", "Mainboard ×1", 1, _td(380, 50, 0.10)),
         ("NIC", "10G NIC ×1", 1, _td(500, 30, 0.10)),
         ("CPU", "EPYC 9354 ×1", 1, _ref(4500, 0.25)),
     ]),
    ("SCN-FLASH-JBOF", "Flash JBOF FJ-1 · all-flash shelf", "Storage",
     "NAND-dense all-flash shelf — already lean vs floor (low gap)", "22100.00", [
         ("STORAGE", "NVMe TLC module ×48", 48, _td(90, 4, 0.10)),
         ("MEMORY", "64GB DDR5 RDIMM ×8", 8, _td(110, 6, 0.12)),
         ("CHASSIS", "4U chassis ×1", 1, _td(180, 40, 0.10)),
         ("PSU", "2400W PSU ×2", 2, _td(120, 18, 0.10)),
         ("MOTHERBOARD", "Mainboard ×1", 1, _td(420, 60, 0.10)),
         ("NIC", "ConnectX-7 ×1", 1, _td(700, 40, 0.10)),
         ("CPU", "EPYC 9554 ×1", 1, _ref(7120, 0.22)),
     ]),
]


# Per-commodity 12-month multiplier path (×baseline). DRAM has the deliberate
# spike (~4× peak) telling the memory-crunch story; others drift mildly.
_SERIES = {
    "DRAM_DDR5": [1.0, 1.0, 1.1, 1.2, 1.4, 1.8, 2.6, 3.6, 4.0, 3.4, 2.4, 1.8],
    "NAND_TLC":  [1.0, 1.0, 1.05, 1.1, 1.15, 1.2, 1.3, 1.45, 1.5, 1.5, 1.5, 1.5],
    "STEEL_CR":  [1.0, 1.0, 1.0, 1.0, 1.02, 1.02, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0],
    "ALU_LME":   [1.0, 1.0, 1.0, 1.03, 1.05, 1.05, 1.04, 1.02, 1.0, 1.0, 1.0, 1.0],
    "COPPER_LME": [1.0, 1.0, 1.0, 1.0, 1.05, 1.05, 1.05, 1.05, 1.05, 1.05, 1.05, 1.05],
    "PCB_LABOR": [1.0] * 12,
}


def seed_costing(db: Session) -> None:
    if db.scalar(select(Commodity).limit(1)):
        print("Costing catalog already present — skipping costing seed.")
        return

    # Commodities + 12-month series.
    commodities: dict[str, Commodity] = {}
    for code, name, unit in _COMMODITIES:
        c = Commodity(code=code, name=name, unit=unit, baseline_value=Decimal("1"))
        db.add(c)
        db.flush()
        commodities[code] = c
        path = _SERIES[code]
        for i, mult in enumerate(path):
            # path[0] is 11 months ago … path[11] is the base month.
            db.add(CommodityPrice(
                commodity_id=c.id, price_date=_months_back(11 - i),
                value=Decimal(str(mult)),
            ))
    db.flush()

    # Component classes.
    classes: dict[str, ComponentClass] = {}
    for code, name, method, driver in _CLASSES:
        cls = ComponentClass(
            code=code, name=name, method=method,
            commodity_id=commodities[driver].id if driver else None,
        )
        db.add(cls)
        db.flush()
        classes[code] = cls

    # A SPREAD of dedicated should-cost products (commodity-dense → silicon-heavy,
    # lean → over-priced) so the cockpit page is worth switching around. Each gets
    # its own BOM + a preferred-supplier quote. The R760 etc. are left without a
    # BOM on purpose (the should-cost view is its own curated set).
    supplier = db.scalar(select(Organization).where(Organization.is_supplier.is_(True)))
    seeded = 0
    for code, name, category, desc, quote, lines in _DEMO_BOMS:
        prod = db.scalar(select(Product).where(Product.product_code == code))
        if prod is None:
            prod = Product(product_code=code, name=name, category=category, description=desc)
            db.add(prod)
            db.flush()
        if db.scalar(select(BOM).where(BOM.product_id == prod.id)) is not None:
            continue
        bom = BOM(product_id=prod.id, notes=f"Should-cost teardown: {desc}")
        db.add(bom)
        db.flush()
        for cls_code, label, qty, kw in lines:
            db.add(BOMLine(bom_id=bom.id, component_class_id=classes[cls_code].id,
                           label=label, qty=qty, **kw))
        db.add(CostParams(bom_id=bom.id))  # defaults: 6% / 8% / 10%
        if supplier is not None:
            db.add(ProductSupplier(
                product_id=prod.id, supplier_id=supplier.id,
                contract_price=Decimal(quote), preference_rank=1,
                standard_lead_time_days=28, min_order_quantity=1,
            ))
        db.flush()
        seeded += 1

    print("Costing seed complete:")
    print(f"  commodities: {len(_COMMODITIES)} (12-mo series each; DRAM spike ~4x)")
    print(f"  component classes: {len(_CLASSES)}")
    print(f"  should-cost products with BOM + quote: {seeded}")

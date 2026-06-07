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

    # A DEDICATED should-cost hero product: a DRAM/NVMe-dominated storage node
    # (single modest CPU, lots of memory + flash) so the commodity teardown is
    # the MAJORITY of the floor — the part with citable sources carries the
    # number, and the memory-spike sensitivity actually moves it. The quote is
    # designed together with the BOM (~30% over the computed floor → ~15%
    # gap-to-target, ~23% gap-to-floor) so the demo lands. Mirrors the spec §8a
    # hero worked example. The R760 etc. are deliberately left without a BOM.
    hero = db.scalar(select(Product).where(Product.product_code == "SCN-STORAGE-1"))
    if hero is None:
        hero = Product(product_code="SCN-STORAGE-1", name="Storage Node SN-1 · 4U appliance",
                       category="Storage", description="DRAM/NVMe-dense storage appliance (should-cost hero config)")
        db.add(hero)
        db.flush()

    if db.scalar(select(BOM).where(BOM.product_id == hero.id)) is None:
        bom = BOM(product_id=hero.id, notes="Hero should-cost teardown: DRAM/NVMe-dominated storage node.")
        db.add(bom)
        db.flush()
        lines = [
            ("MEMORY", "64GB DDR5 RDIMM ×24", 24, dict(base_material_cost=110, conversion_cost=6, overhead_pct=Decimal("0.12"))),
            ("STORAGE", "NVMe TLC module ×24", 24, dict(base_material_cost=90, conversion_cost=4, overhead_pct=Decimal("0.10"))),
            ("CHASSIS", "4U chassis ×1", 1, dict(base_material_cost=180, conversion_cost=40, overhead_pct=Decimal("0.10"))),
            ("PSU", "2400W PSU ×2", 2, dict(base_material_cost=120, conversion_cost=18, overhead_pct=Decimal("0.10"))),
            ("MOTHERBOARD", "Mainboard ×1", 1, dict(base_material_cost=420, conversion_cost=60, overhead_pct=Decimal("0.10"))),
            ("NIC", "ConnectX-7 ×1", 1, dict(base_material_cost=700, conversion_cost=40, overhead_pct=Decimal("0.10"))),
            ("CPU", "EPYC 9554 ×1", 1, dict(list_price=7120, discount_pct=Decimal("0.22"))),  # single modest CPU
        ]
        for cls_code, label, qty, kw in lines:
            db.add(BOMLine(bom_id=bom.id, component_class_id=classes[cls_code].id,
                           label=label, qty=qty, **kw))
        db.add(CostParams(bom_id=bom.id))  # defaults: 6% / 8% / 10%
        db.flush()

        # A preferred supplier whose contract price is the quote we negotiate
        # against — set ~30% over the floor computed at the base-month indices
        # (DRAM ×1.8, NAND ×1.5). Floor ≈ €18,961 → quote €24,650.
        supplier = db.scalar(select(Organization).where(Organization.is_supplier.is_(True)))
        if supplier is not None:
            db.add(ProductSupplier(
                product_id=hero.id, supplier_id=supplier.id,
                contract_price=Decimal("24650.00"), preference_rank=1,
                standard_lead_time_days=28, min_order_quantity=1,
            ))
            db.flush()

    print("Costing seed complete:")
    print(f"  commodities: {len(_COMMODITIES)} (12-mo series each; DRAM spike ~4x)")
    print(f"  component classes: {len(_CLASSES)}")
    print("  hero product: SCN-STORAGE-1 (BOM + €24,650 quote, ~30% over floor)")

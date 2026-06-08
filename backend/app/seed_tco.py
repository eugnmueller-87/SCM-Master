"""Synthetic TCO data generator.

Builds ~300–500 assets across four hardware classes (storage, compute, GPU,
network switch), each with internally-consistent records across EVERY cost
layer (acquisition via provenance, landed, deployment, 60 months of opex, EOL,
recovery). Deterministic: pass a seed for byte-for-byte reproducibility.

Design choices (defended in the build prompt):
  - Determinism via an explicit ``random.Random(seed)`` instance (not the global
    RNG), so runs are reproducible and isolated.
  - Acquisition = should-cost-style base per class ± realistic variance (some
    buys above target, some below floor) — so the should-cost → actual variance
    metric is non-trivial and testable. Stored as the real paid price on an
    OrderItem the asset traces to (the system's actual-paid anchor).
  - Forge-locked: refuses to run when SCM_ENV=prod (assert_seeding_allowed).
  - Idempotent: bails if TCO data already exists.

Run (from backend/):
    .venv\\Scripts\\python -m app.seed_tco            # default seed + count
    .venv\\Scripts\\python -m app.seed_tco --seed 7 --assets 400 --no-duty
"""
from __future__ import annotations

import argparse
import random
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.db import SessionLocal
from app.core.safety import assert_seeding_allowed
from app.models.catalog import Organization, Product
from app.models.flow import Asset, AssetStatus
from app.models.procurement import OrderItem, PurchaseOrder
from app.models.tco import (
    DeploymentCost,
    DeploymentTask,
    DepreciationMethod,
    EolCost,
    LandedCost,
    LandedCostType,
    OpexLedger,
    RecoveryValue,
)

_CENT = Decimal("0.01")
SERVICE_MONTHS = 60  # 5-year service life → 60 monthly opex rows


def _eur(x) -> Decimal:
    return Decimal(str(x)).quantize(_CENT, rounding=ROUND_HALF_UP)


# Per-class profile (ranges from the build spec). base_acq = nominal acquisition
# €; power W range; labour rate €/h; residual % range; maintenance scales with
# a component-count proxy (drives/cards) so MTBF-driven maintenance is class-aware.
@dataclass(frozen=True)
class _Profile:
    base_acq: float
    power: tuple[float, float]
    rate: tuple[float, float]
    residual: tuple[float, float]
    components: tuple[int, int]
    deploy_hours: tuple[float, float]


_CLASSES: dict[str, _Profile] = {
    "storage": _Profile(20000, (350, 550), (60, 80), (0.10, 0.18), (12, 48), (3, 8)),
    "compute": _Profile(14000, (400, 800), (70, 90), (0.08, 0.15), (2, 8), (2, 5)),
    "gpu":     _Profile(42000, (2000, 6000), (80, 95), (0.12, 0.18), (2, 8), (4, 8)),
    "switch":  _Profile(9000, (150, 400), (60, 75), (0.08, 0.14), (24, 48), (2, 4)),
}

# The Product.category actually STORED — canonical Title Case, so the synthetic
# TCO assets share one vocabulary with the demo catalog (`Storage`, not a second
# lowercase `storage` bucket) and the analytics/spend board shows one bar per
# category. The internal class key (the `_CLASSES` keys above) is unchanged, so
# per-class TCO profiles and grouping are unaffected.
_DISPLAY_CATEGORY = {
    "storage": "Storage",   # merges with the demo's "Storage" bucket
    "compute": "Compute",
    "gpu":     "GPU",
    "switch":  "Networking",  # a switch is networking gear → demo's "Networking"
}


def _month_start(d: date, n: int) -> date:
    total = d.year * 12 + (d.month - 1) + n
    return date(total // 12, total % 12 + 1, 1)


def seed_tco(db: Session, *, seed: int = 42, n_assets: int = 400,
             include_duty: bool = True) -> dict:
    """Generate the synthetic TCO dataset. Returns a small summary dict."""
    assert_seeding_allowed("synthetic TCO dataset")  # forge-lock: never in prod

    if db.scalar(select(LandedCost).limit(1)) or db.scalar(select(OpexLedger).limit(1)):
        return {"skipped": True, "reason": "TCO data already present"}

    rng = random.Random(seed)  # nosec B311 — synthetic demo data, not cryptographic
    today = date(2026, 6, 1)

    supplier = Organization(code="TCO-VEND", name="TCO Synthetic Vendor", is_supplier=True)
    db.add(supplier)
    db.flush()
    po = PurchaseOrder(order_number="PO-TCO-SYN", supplier_id=supplier.id)
    db.add(po)
    db.flush()

    # One product per class (the asset carries the class via its product).
    products = {}
    for cls in _CLASSES:
        p = Product(product_code=f"TCO-{cls.upper()}", name=f"Synthetic {cls} node",
                    category=_DISPLAY_CATEGORY[cls])
        db.add(p)
        products[cls] = p
    db.flush()

    classes = list(_CLASSES)
    counts = {c: 0 for c in classes}
    energy_regions = [Decimal("0.12"), Decimal("0.16"), Decimal("0.20"), Decimal("0.24"), Decimal("0.28")]

    for i in range(n_assets):
        cls = classes[i % len(classes)]
        prof = _CLASSES[cls]
        counts[cls] += 1

        # --- acquisition: base ± variance (some above, some below) -----------
        variance = rng.uniform(-0.12, 0.18)  # −12%..+18% around base
        acq = _eur(prof.base_acq * (1 + variance))
        oi = OrderItem(order_id=po.id, product_id=products[cls].id, quantity=1, unit_price=acq)
        db.add(oi)
        db.flush()

        # stagger deploy dates over the trailing ~4y so ages vary
        deployed = today - timedelta(days=rng.randint(30, 1500))
        received = deployed - timedelta(days=rng.randint(5, 30))
        asset = Asset(serial_number=f"TCO-{cls[:2].upper()}-{i:04d}", product_id=products[cls].id,
                      status=AssetStatus.DEPLOYED, source_order_item_id=oi.id,
                      received_date=received, deployed_date=deployed)
        db.add(asset)
        db.flush()

        # --- landed: 3–7% of acquisition, split across components ------------
        landed_total = float(acq) * rng.uniform(0.03, 0.07)
        # freight (often two legs) + insurance + handling, + duty behind the flag
        legs = rng.choice([1, 2])
        freight_each = landed_total * 0.55 / legs
        for _ in range(legs):
            db.add(LandedCost(asset_id=asset.id, cost_type=LandedCostType.FREIGHT,
                              amount=_eur(freight_each), incoterm="FOB",
                              incurred_date=received))
        db.add(LandedCost(asset_id=asset.id, cost_type=LandedCostType.INSURANCE,
                          amount=_eur(landed_total * 0.15), incoterm="FOB"))
        db.add(LandedCost(asset_id=asset.id, cost_type=LandedCostType.HANDLING,
                          amount=_eur(landed_total * 0.10)))
        if include_duty:
            db.add(LandedCost(asset_id=asset.id, cost_type=LandedCostType.DUTY,
                              amount=_eur(landed_total * 0.20)))

        # --- deployment: staged labour, amount = hours × rate ---------------
        rate = _eur(rng.uniform(*prof.rate))
        total_hours = rng.uniform(*prof.deploy_hours)
        # split across 2–3 tasks
        tasks = rng.sample([DeploymentTask.RECEIVING, DeploymentTask.RACKING,
                            DeploymentTask.CABLING, DeploymentTask.IMAGING],
                           k=rng.choice([2, 3]))
        for j, task in enumerate(tasks):
            h = _eur(total_hours / len(tasks))
            db.add(DeploymentCost(asset_id=asset.id, task=task, labor_hours=h, rate=rate,
                                  amount=_eur(h * rate),
                                  incurred_date=deployed + timedelta(days=j)))

        # --- opex: 60 monthly rows -----------------------------------------
        watts = rng.uniform(*prof.power)
        pue = _eur(rng.uniform(1.2, 1.5))
        rate_kwh = rng.choice(energy_regions)
        comp = rng.randint(*prof.components)  # drive/card count → maintenance scale
        for m in range(SERVICE_MONTHS):
            # monthly kWh = W/1000 × 24h × ~30.4d
            kwh = _eur(watts / 1000 * 24 * 30.4)
            # maintenance MTBF-driven: scales with component count, rises slowly with age
            maint = _eur(comp * 0.45 * (1 + m * 0.01))
            cooling = _eur(float(kwh) * float(rate_kwh) * 0.10)  # ~10% of energy as extra cooling
            lic = _eur(8 if cls in ("compute", "gpu") else 0)
            db.add(OpexLedger(asset_id=asset.id, period=_month_start(deployed, m),
                              power_kwh=kwh, pue=pue, energy_rate=rate_kwh,
                              cooling=cooling, maintenance=maint, license=lic))

        # --- EOL ------------------------------------------------------------
        eol_date = _month_start(deployed, SERVICE_MONTHS)
        db.add(EolCost(asset_id=asset.id,
                       decommission=_eur(rng.uniform(40, 120)),
                       data_destruction=_eur(rng.uniform(10, 40) if cls == "storage" else rng.uniform(5, 20)),
                       weee=_eur(rng.uniform(10, 30)),
                       itad_fee=_eur(rng.uniform(20, 60)),
                       eol_date=eol_date))

        # --- recovery: residual %, decaying with age -----------------------
        age_years = (today - deployed).days / 365.25
        residual_pct = rng.uniform(*prof.residual) * max(0.0, 1 - age_years / 6)  # decays to 0 by ~6y
        db.add(RecoveryValue(asset_id=asset.id,
                             residual_value=_eur(float(acq) * residual_pct),
                             resale_channel=rng.choice(["broker", "oem-buyback", "internal-reuse"]),
                             depr_method=DepreciationMethod.STRAIGHT_LINE,
                             recovery_date=eol_date))

    db.commit()
    return {"skipped": False, "seed": seed, "assets": n_assets,
            "by_class": counts, "include_duty": include_duty,
            "opex_rows": n_assets * SERVICE_MONTHS}


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate synthetic TCO data.")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--assets", type=int, default=400)
    ap.add_argument("--no-duty", action="store_true", help="omit the DUTY landed component")
    args = ap.parse_args()

    db = SessionLocal()
    try:
        summary = seed_tco(db, seed=args.seed, n_assets=args.assets, include_duty=not args.no_duty)
        if summary.get("skipped"):
            print(f"TCO seed skipped — {summary['reason']}.")
        else:
            print("TCO seed complete:")
            print(f"  seed={summary['seed']}  assets={summary['assets']}  by_class={summary['by_class']}")
            print(f"  opex rows={summary['opex_rows']}  include_duty={summary['include_duty']}")
    finally:
        db.close()


if __name__ == "__main__":
    main()

"""Deep demand history — ~18 months of dated usage so the demand forecast can be
*backtested* for accuracy and pulled into Power BI.

The live demo seed (``seed_demo``) gives every screen something to show, but its
deployments are clustered in a single recent window — there's no time series to
score a forecast against. This adds that: for each product it lays down a steady
monthly deployment rate (with reproducible noise) stretching back ~18 months,
built through the REAL services so each unit carries a real backdated
``received_date`` / ``deployed_date``. Because :func:`planning.demand_forecast`
accepts an as-of ``today``, that dated history is exactly what lets us ask "what
would the agent have forecast last quarter?" and compare it to what actually
deployed next (see ``services/accuracy.py``).

Design choices:
  - **Steady + noise** demand per product (not trend/seasonal) — the forecast
    stays well-calibrated, demonstrating it tracks real usage rather than
    over/under-reacting.
  - **18 months** of history (> the 90-day window + horizon) so a backtest as-of
    any month in the middle has both trailing history to forecast FROM and
    future actuals to score AGAINST.
  - Deterministic: noise comes from a hash of (product, month), so re-seeding a
    fresh DB reproduces the same series — stable for screenshots and tests.
  - Deployments land in a dedicated high-capacity history datacenter so they
    don't trip the demo's deliberately-small rack/cage capacities.

Run AFTER the demo seed, on a fresh database (from backend/):
    .venv\\Scripts\\python -m app.seed_demo
    .venv\\Scripts\\python -m app.seed_history

Idempotent: bails out if the history datacenter already exists.
"""
from __future__ import annotations

import hashlib
import os
from datetime import date, timedelta

from sqlalchemy import select

from app.core.db import SessionLocal
from app.models.catalog import Product, ProductSupplier
from app.models.flow import AssetStatus, Location, LocationType
from app.models.procurement import OrderStatus
from app.services.asset import asset_service
from app.services.flow import location_service
from app.services.procurement import purchase_order_service

TODAY = date(2026, 6, 1)
HISTORY_MONTHS = 18
HISTORY_DC_CODE = "DC-HIST"

# Base deployments/month per product code. Steady demand; the actual figure each
# month is this ± deterministic noise. Sized so 18 months builds a believable
# fleet without exploding the asset count.
_BASE_MONTHLY = {
    "DELL-R760": 6,    # servers — the workhorse
    "AMD-9554": 5,     # CPUs
    "SEC-M321R": 14,   # memory — highest volume
    "SMC-SC847": 2,    # JBOD chassis — low volume
    "NVDA-CX7": 4,     # NICs
    "PSU-2400T": 5,    # PSUs
}
_NOISE = 0.25  # ±25% reproducible noise around the base rate


def _noise_factor(product_code: str, month_index: int) -> float:
    """Deterministic noise in [1-_NOISE, 1+_NOISE] from a hash of product+month.

    Avoids ``random`` (which the workflow sandbox forbids and which would break
    reproducibility); the same fresh DB always reproduces the same series.
    """
    h = hashlib.sha256(f"{product_code}:{month_index}".encode()).digest()
    # first two bytes -> [0,1)
    frac = (h[0] * 256 + h[1]) / 65536.0
    return 1.0 - _NOISE + 2 * _NOISE * frac


def _month_start(months_before: int) -> date:
    """First day of the month that is ``months_before`` months before TODAY."""
    y, m = TODAY.year, TODAY.month
    total = (y * 12 + (m - 1)) - months_before
    return date(total // 12, total % 12 + 1, 1)


def _preferred_source(db, product_id: str) -> ProductSupplier | None:
    return db.scalars(
        select(ProductSupplier)
        .where(ProductSupplier.product_id == product_id, ProductSupplier.active.is_(True))
        .order_by(ProductSupplier.preference_rank)
    ).first()


def seed_history() -> None:
    db = SessionLocal()
    try:
        if db.scalar(select(Location).where(Location.code == HISTORY_DC_CODE)):
            print("History datacenter already present — skipping history seed.")
            return

        products = db.scalars(select(Product)).all()
        if not products:
            print("No products — run app.seed_demo first.")
            return
        by_code = {p.product_code: p for p in products}

        # A roomy datacenter + rack for historical deployments (won't trip the
        # demo's small rack capacities). Capacity sized for 18 months of demand.
        hist_dc = location_service.create(db, dict(
            code=HISTORY_DC_CODE, name="Historical fleet (backtest)",
            location_type=LocationType.DATACENTER, capacity=5000))
        hist_rack = location_service.create(db, dict(
            code="RACK-HIST", name="Historical rack", location_type=LocationType.RACK,
            parent_id=hist_dc.id, capacity=5000))

        po_counter = [900]  # history PO numbers start high to avoid demo collisions

        def month_demand(product_code: str, month_index: int) -> int:
            base = _BASE_MONTHLY.get(product_code, 3)
            return max(1, round(base * _noise_factor(product_code, month_index)))

        total_assets = 0
        # Oldest month first so order/receipt/deploy dates march forward in time.
        for mi in range(HISTORY_MONTHS, 0, -1):
            month_index = HISTORY_MONTHS - mi  # 0..17, stable key for noise
            m_start = _month_start(mi)
            for product in products:
                src = _preferred_source(db, product.id)
                if src is None:
                    continue
                qty = month_demand(product.product_code, month_index)

                # One PO per product per month, ordered just before the month,
                # received early in the month, deployed across the month.
                po_counter[0] += 1
                order_date = m_start - timedelta(days=(src.standard_lead_time_days or 14))
                po = purchase_order_service.create(db, dict(
                    order_number=f"PO-HIST-{po_counter[0]:04d}",
                    supplier_id=src.supplier_id, destination_id=hist_dc.id,
                    date_ordered=order_date,
                    items=[dict(product_id=product.id, product_supplier_id=src.id,
                                quantity=qty, unit_price=src.contract_price)]))
                purchase_order_service.set_status(db, po.id, OrderStatus.APPROVED)
                purchase_order_service.set_status(db, po.id, OrderStatus.PLACED)

                receipt_date = m_start + timedelta(days=2)
                asset_service.receive(
                    db, po.id, location_id=hist_dc.id,
                    lines=[{"order_item_id": po.items[0].id, "quantity": qty}],
                    receipt_date=receipt_date, actor="history")

                # Deploy each received unit on a staggered day within the month.
                received = [a for a in asset_service.list(
                    db, status=AssetStatus.RECEIVED, limit=5000)
                    if a.source_order_item_id == po.items[0].id]
                for j, asset in enumerate(received):
                    deploy_date = m_start + timedelta(days=3 + (j * 23) % 25)
                    asset_service.transition(db, asset.id, AssetStatus.IN_STORAGE,
                                             actor="history")
                    asset_service.transition(db, asset.id, AssetStatus.DEPLOYED,
                                             location_id=hist_rack.id, actor="history",
                                             effective_date=deploy_date)
                    total_assets += 1
            db.commit()

        print("History seed complete:")
        print(f"  {HISTORY_MONTHS} months of dated deployments across {len(by_code)} products")
        print(f"  {total_assets} historical assets deployed (history DC {HISTORY_DC_CODE})")
        print(f"  PO numbers PO-HIST-0901..{po_counter[0]}")
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    # Same opt-in gate as seed_demo: demand history is demo-only data.
    if os.getenv("SEED_DEMO") != "1":
        print("SEED_DEMO != 1 — skipping history seed (set SEED_DEMO=1 to populate demo data).")
    else:
        seed_history()

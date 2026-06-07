"""Unit tests for the TCO computation service.

Fixed fixtures, asserted to the cent. Covers the per-asset waterfall, the
should-cost variance, the landed-type exclusion filter, the non-EUR guard, and
the portfolio rollup's two labelled ratios.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.models.catalog import Organization, Product
from app.models.costing import ShouldCostRun
from app.models.flow import Asset
from app.models.procurement import OrderItem, PurchaseOrder
from app.models.tco import (
    DeploymentCost,
    DeploymentTask,
    EolCost,
    LandedCost,
    LandedCostType,
    OpexLedger,
    RecoveryValue,
)
from app.services import tco
from app.services.tco import CurrencyMixError

D = Decimal


def _asset_with_acquisition(db, unit_price="20857.51", product_code="TCO-HERO"):
    """An asset whose actual-paid acquisition is unit_price (via provenance)."""
    org = Organization(code="S", name="Supplier", is_supplier=True)
    prod = Product(product_code=product_code, name="Hero Node")
    db.add_all([org, prod])
    db.flush()
    po = PurchaseOrder(order_number="PO-TCO", supplier_id=org.id)
    db.add(po)
    db.flush()
    oi = OrderItem(order_id=po.id, product_id=prod.id, quantity=1, unit_price=D(unit_price))
    db.add(oi)
    db.flush()
    a = Asset(serial_number=f"SN-{product_code}", product_id=prod.id, source_order_item_id=oi.id)
    db.add(a)
    db.flush()
    return a, prod


def _full_layers(db, asset):
    # Landed: 2 freight legs (120 + 80) + duty 60 + handling 40  = 300
    db.add_all([
        LandedCost(asset_id=asset.id, cost_type=LandedCostType.FREIGHT, amount=D("120.00")),
        LandedCost(asset_id=asset.id, cost_type=LandedCostType.FREIGHT, amount=D("80.00")),
        LandedCost(asset_id=asset.id, cost_type=LandedCostType.DUTY, amount=D("60.00")),
        LandedCost(asset_id=asset.id, cost_type=LandedCostType.HANDLING, amount=D("40.00")),
    ])
    # Deployment: 105 + 140 = 245
    db.add_all([
        DeploymentCost(asset_id=asset.id, task=DeploymentTask.RECEIVING, amount=D("105.00")),
        DeploymentCost(asset_id=asset.id, task=DeploymentTask.RACKING, amount=D("140.00")),
    ])
    # Opex: 2 months. Each: power 400 × pue 1.3 × rate 0.20 = 104 ; +cooling 20 +maint 10 = 134
    for m in (1, 2):
        db.add(OpexLedger(asset_id=asset.id, period=date(2026, m, 1),
                          power_kwh=D("400"), pue=D("1.3"), energy_rate=D("0.20"),
                          cooling=D("20"), maintenance=D("10")))
    # EOL: 50 + 15 + 30 = 95
    db.add(EolCost(asset_id=asset.id, decommission=D("50"), weee=D("15"), itad_fee=D("30")))
    # Recovery: 900 back
    db.add(RecoveryValue(asset_id=asset.id, residual_value=D("900")))
    db.flush()


def test_asset_tco_waterfall_to_the_cent(db_session):
    a, _ = _asset_with_acquisition(db_session, "20857.51")
    _full_layers(db_session, a)
    r = tco.asset_tco(db_session, a.id)
    w = r["waterfall"]
    assert w["acquisition"] == 20857.51
    assert w["landed"] == 300.00        # 120+80+60+40
    assert w["deployment"] == 245.00    # 105+140
    assert w["opex"] == 268.00          # 2 × (104 + 20 + 10)
    assert w["eol"] == 95.00            # 50+15+30
    assert w["recovery"] == -900.00
    # 20857.51 + 300 + 245 + 268 + 95 − 900 = 20865.51
    assert r["tco_total"] == 20865.51


def test_exclude_landed_duty_filter(db_session):
    a, _ = _asset_with_acquisition(db_session, "1000.00", product_code="TCO-DUTY")
    _full_layers(db_session, a)
    full = tco.asset_tco(db_session, a.id)
    no_duty = tco.asset_tco(db_session, a.id, exclude_landed_types=["DUTY"])
    assert full["waterfall"]["landed"] == 300.00
    assert no_duty["waterfall"]["landed"] == 240.00   # dropped the 60 duty
    assert no_duty["tco_total"] == full["tco_total"] - 60.0
    assert no_duty["excluded_landed_types"] == ["DUTY"]


def test_non_eur_row_fails_loud(db_session):
    a, _ = _asset_with_acquisition(db_session, "100.00", product_code="TCO-USD")
    db_session.add(LandedCost(asset_id=a.id, cost_type=LandedCostType.FREIGHT,
                              amount=D("50"), currency="USD"))
    db_session.flush()
    with pytest.raises(CurrencyMixError, match="not EUR"):
        tco.asset_tco(db_session, a.id)


def test_should_cost_variance_when_run_exists(db_session):
    a, prod = _asset_with_acquisition(db_session, "24650.00", product_code="TCO-VAR")
    # A should-cost run for the product → target 20857.51 (read-only).
    db_session.add(ShouldCostRun(product_id=prod.id, as_of=date(2026, 6, 1),
                                 should_cost_floor=D("18961.37"), target_price=D("20857.51"),
                                 quoted_price=D("24650.00"), breakdown={}))
    db_session.flush()
    r = tco.asset_tco(db_session, a.id)
    v = r["should_cost_variance"]
    assert v is not None
    assert v["should_cost_target"] == 20857.51
    assert v["actual_acquisition"] == 24650.00
    assert v["variance_abs"] == round(24650.00 - 20857.51, 2)  # paid above target
    assert v["variance_pct"] > 0


def test_no_variance_without_should_cost(db_session):
    a, _ = _asset_with_acquisition(db_session, "500.00", product_code="TCO-NOSC")
    assert tco.asset_tco(db_session, a.id)["should_cost_variance"] is None


def test_asset_without_provenance_has_zero_acquisition(db_session):
    prod = Product(product_code="TCO-ORPHAN", name="Orphan")
    db_session.add(prod)
    db_session.flush()
    a = Asset(serial_number="SN-ORPHAN", product_id=prod.id)  # no source_order_item
    db_session.add(a)
    db_session.flush()
    assert tco.asset_tco(db_session, a.id)["waterfall"]["acquisition"] == 0.0


# --- portfolio rollup ------------------------------------------------------

def test_portfolio_two_ratios_and_subtotals(db_session):
    a, _ = _asset_with_acquisition(db_session, "20857.51", product_code="TCO-PF")
    _full_layers(db_session, a)
    # baseline €1,000,000 revenue
    p = tco.portfolio_tco(db_session, baseline=D("1000000"))
    assert p["assets"] == 1
    assert p["subtotals"]["acquisition"] == 20857.51
    assert p["tco_total"] == 20865.51
    # total_cost includes hardware; tscmc excludes acquisition
    assert round(p["total_cost_pct"], 6) == round(20865.51 / 1000000, 6)
    tscmc_num = 20865.51 - 20857.51  # = 8.00 (the non-acquisition layers, net recovery)
    assert round(p["tscmc_pct"], 6) == round(tscmc_num / 1000000, 6)
    # tscmc must be strictly below total_cost (acquisition stripped)
    assert p["tscmc_pct"] < p["total_cost_pct"]


def test_portfolio_baseline_must_be_positive(db_session):
    with pytest.raises(Exception, match="baseline"):
        tco.portfolio_tco(db_session, baseline=D("0"))

"""Phase-1 model smoke tests for the TCO layers.

No business logic yet (that's Phase 2) — these just verify each layer persists,
carries the agreed columns, FKs to an asset, and that landed/deployment are
genuinely multi-row per asset.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.models.catalog import Product
from app.models.flow import Asset
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


def _asset(db) -> Asset:
    prod = Product(product_code="TCO-P1", name="TCO Test Product")
    db.add(prod)
    db.flush()
    a = Asset(serial_number="TCO-SN-1", product_id=prod.id)
    db.add(a)
    db.flush()
    return a


def test_landed_cost_is_multi_row_per_asset(db_session):
    a = _asset(db_session)
    db_session.add_all([
        LandedCost(asset_id=a.id, cost_type=LandedCostType.FREIGHT, amount=Decimal("120.00"), incoterm="FOB"),
        LandedCost(asset_id=a.id, cost_type=LandedCostType.FREIGHT, amount=Decimal("80.00"), incoterm="FOB"),  # 2nd freight leg
        LandedCost(asset_id=a.id, cost_type=LandedCostType.DUTY, amount=Decimal("60.00")),
    ])
    db_session.flush()
    rows = db_session.query(LandedCost).filter_by(asset_id=a.id).all()
    assert len(rows) == 3  # multiple rows per type allowed
    assert all(r.currency == "EUR" for r in rows)  # default currency


def test_deployment_cost_multi_row_and_amount(db_session):
    a = _asset(db_session)
    db_session.add_all([
        DeploymentCost(asset_id=a.id, task=DeploymentTask.RECEIVING, labor_hours=Decimal("1.5"),
                       rate=Decimal("70.00"), amount=Decimal("105.00")),
        DeploymentCost(asset_id=a.id, task=DeploymentTask.RACKING, labor_hours=Decimal("2.0"),
                       rate=Decimal("70.00"), amount=Decimal("140.00")),
    ])
    db_session.flush()
    rows = db_session.query(DeploymentCost).filter_by(asset_id=a.id).all()
    assert len(rows) == 2
    assert rows[0].amount == Decimal("105.00")


def test_opex_ledger_is_monthly_timeseries(db_session):
    a = _asset(db_session)
    for m in range(1, 4):
        db_session.add(OpexLedger(asset_id=a.id, period=date(2026, m, 1),
                                  power_kwh=Decimal("400"), pue=Decimal("1.300"),
                                  energy_rate=Decimal("0.2000")))
    db_session.flush()
    rows = db_session.query(OpexLedger).filter_by(asset_id=a.id).order_by(OpexLedger.period).all()
    assert [r.period.month for r in rows] == [1, 2, 3]


def test_eol_and_recovery_one_row_per_asset(db_session):
    a = _asset(db_session)
    db_session.add(EolCost(asset_id=a.id, decommission=Decimal("50"), weee=Decimal("15"),
                           itad_fee=Decimal("30"), eol_date=date(2031, 1, 1)))
    db_session.add(RecoveryValue(asset_id=a.id, residual_value=Decimal("900"),
                                 resale_channel="broker", depr_method=DepreciationMethod.STRAIGHT_LINE,
                                 recovery_date=date(2031, 2, 1)))
    db_session.flush()
    eol = db_session.query(EolCost).filter_by(asset_id=a.id).one()
    rec = db_session.query(RecoveryValue).filter_by(asset_id=a.id).one()
    assert eol.itad_fee == Decimal("30")
    assert rec.depr_method is DepreciationMethod.STRAIGHT_LINE
    assert rec.currency == "EUR"

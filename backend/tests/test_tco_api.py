"""Integration tests for the TCO read API."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.models.catalog import Organization, Product
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

B = "/api/v1"
D = Decimal


def _asset_with_full_tco(db, unit_price="20857.51"):
    org = Organization(code="S", name="Supplier", is_supplier=True)
    prod = Product(product_code="API-TCO", name="API TCO Node")
    db.add_all([org, prod])
    db.flush()
    po = PurchaseOrder(order_number="PO-API-TCO", supplier_id=org.id)
    db.add(po)
    db.flush()
    oi = OrderItem(order_id=po.id, product_id=prod.id, quantity=1, unit_price=D(unit_price))
    db.add(oi)
    db.flush()
    a = Asset(serial_number="API-SN-1", product_id=prod.id, source_order_item_id=oi.id)
    db.add(a)
    db.flush()
    db.add_all([
        LandedCost(asset_id=a.id, cost_type=LandedCostType.FREIGHT, amount=D("200.00")),
        LandedCost(asset_id=a.id, cost_type=LandedCostType.DUTY, amount=D("60.00")),
        DeploymentCost(asset_id=a.id, task=DeploymentTask.RACKING, amount=D("140.00")),
        OpexLedger(asset_id=a.id, period=date(2026, 1, 1), power_kwh=D("400"),
                   pue=D("1.3"), energy_rate=D("0.20"), cooling=D("20"), maintenance=D("10")),
        EolCost(asset_id=a.id, decommission=D("50"), weee=D("15"), itad_fee=D("30")),
        RecoveryValue(asset_id=a.id, residual_value=D("900")),
    ])
    db.commit()
    return a


def test_asset_tco_endpoint(client, db_session):
    a = _asset_with_full_tco(db_session)
    r = client.get(f"{B}/assets/{a.id}/tco")
    assert r.status_code == 200, r.text
    body = r.json()
    w = body["waterfall"]
    assert w["acquisition"] == 20857.51
    assert w["landed"] == 260.00       # 200 + 60
    assert w["deployment"] == 140.00
    assert w["opex"] == 134.00         # 104 + 20 + 10
    assert w["recovery"] == -900.00
    assert body["tco_total"] == round(20857.51 + 260 + 140 + 134 + 95 - 900, 2)


def test_asset_tco_exclude_duty(client, db_session):
    a = _asset_with_full_tco(db_session)
    r = client.get(f"{B}/assets/{a.id}/tco?exclude_landed_types=DUTY")
    body = r.json()
    assert body["waterfall"]["landed"] == 200.00   # duty dropped
    assert body["excluded_landed_types"] == ["DUTY"]


def test_asset_tco_404(client):
    assert client.get(f"{B}/assets/does-not-exist/tco").status_code == 404


def test_portfolio_endpoint_two_ratios(client, db_session):
    _asset_with_full_tco(db_session)
    r = client.get(f"{B}/tco/portfolio?baseline=1000000")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["assets"] >= 1
    assert "total_cost_pct" in body and "tscmc_pct" in body
    # tscmc excludes acquisition → strictly below total_cost
    assert body["tscmc_pct"] < body["total_cost_pct"]
    assert body["subtotals"]["acquisition"] == 20857.51


def test_portfolio_requires_positive_baseline(client, db_session):
    _asset_with_full_tco(db_session)
    # baseline=0 fails FastAPI's gt=0 validation → 422
    assert client.get(f"{B}/tco/portfolio?baseline=0").status_code == 422


def test_tco_by_class_endpoint(client, db_session):
    a = _asset_with_full_tco(db_session)
    # the asset's product has no category set → groups under "other"
    r = client.get(f"{B}/tco/by-class")
    assert r.status_code == 200, r.text
    rows = r.json()
    assert len(rows) >= 1
    row = rows[0]
    assert row["assets"] == 1
    assert row["acquisition"] == 20857.51
    assert row["avg_tco"] == row["tco_total"]  # one asset → avg == total
    _ = a


def test_tco_endpoints_require_auth(client):
    anon = client.anon()
    assert anon.get(f"{B}/tco/portfolio?baseline=1000").status_code == 401
    assert anon.get(f"{B}/tco/by-class").status_code == 401

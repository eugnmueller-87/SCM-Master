"""Integration tests for the costing API (over the in-memory DB).

Builds a commodity catalog + a product BOM through the API, then exercises
should-cost, cost-gap, sensitivity, the analytics aggregations, and role gating.
"""
from __future__ import annotations

from app.models.auth import Role

B = "/api/v1"


def _seed_costing(client):
    """A DRAM commodity with a price spike, a MEMORY teardown class + a CPU
    reference class, a product with a preferred supplier, and a BOM."""
    # Commodity + a 2-point series (baseline 1.0 -> 1.8 spike).
    dram = client.post(f"{B}/commodities", json={
        "code": "DRAM_DDR5", "name": "DDR5 DRAM", "unit": "index", "baseline_value": 1.0,
    }).json()
    client.post(f"{B}/commodities/{dram['id']}/prices", json={"price_date": "2025-06-01", "value": 1.0})
    client.post(f"{B}/commodities/{dram['id']}/prices", json={"price_date": "2026-05-01", "value": 1.8})
    # ComponentClass is fixed reference data (no public write route); the caller
    # inserts the classes via the test session — see _make_classes.
    return dram


def _make_classes(db_session, dram_id):
    from app.models.costing import ComponentClass, CostingMethod
    mem = ComponentClass(code="MEMORY", name="Memory", method=CostingMethod.teardown, commodity_id=dram_id)
    cpu = ComponentClass(code="CPU", name="CPU", method=CostingMethod.reference_price)
    db_session.add_all([mem, cpu])
    db_session.commit()


def _product_with_supplier(client):
    org = client.post(f"{B}/organizations", json={"code": "ACME", "name": "Acme", "is_supplier": True}).json()
    prod = client.post(f"{B}/products", json={"product_code": "SRV-MEM", "name": "Memory Node", "category": "server"}).json()
    client.post(f"{B}/product-suppliers", json={
        "product_id": prod["id"], "supplier_id": org["id"],
        "contract_price": "9000.00", "preference_rank": 1,
    })
    return prod


def test_should_cost_and_gap_end_to_end(client, db_session):
    dram = _seed_costing(client)
    _make_classes(db_session, dram["id"])
    prod = _product_with_supplier(client)

    # Set a BOM: 16 DIMMs (teardown, DRAM) + 1 CPU (reference).
    r = client.put(f"{B}/products/{prod['id']}/bom", json={
        "lines": [
            {"component_class_code": "MEMORY", "label": "64GB DDR5 ×16", "qty": 16,
             "base_material_cost": 110, "conversion_cost": 6, "overhead_pct": 0.12},
            {"component_class_code": "CPU", "label": "EPYC ×1", "qty": 1,
             "list_price": 7120, "discount_pct": 0.22},
        ],
    })
    assert r.status_code == 200, r.text

    # should-cost as-of the spike date (DRAM ×1.8).
    sc = client.post(f"{B}/products/{prod['id']}/should-cost?as_of=2026-06-01").json()
    # memory line: (110*1.8 + 6 + 110*1.8*0.12)*16 = 3644.16 ; cpu: 7120*0.78 = 5553.60
    mem_line = next(li for li in sc["lines"] if "DDR5" in li["label"])
    assert mem_line["component_floor"] == 3644.16
    assert mem_line["commodity_tracked"] is True
    assert sc["should_cost_floor"] > 0 and sc["target_price"] > sc["should_cost_floor"]

    # cost-gap vs the €9000 preferred-supplier price.
    gap = client.get(f"{B}/products/{prod['id']}/cost-gap?annual_volume=100").json()
    assert gap["has_quote"] is True
    assert gap["quoted_price"] == 9000.0
    # headline gap is vs target, and must be < gap vs the bare floor
    assert gap["gap_to_target_abs"] < gap["gap_to_floor_abs"]
    assert gap["addressable_saving"] == round(gap["gap_to_target_abs"] * 100, 2)


def test_sensitivity_moves_with_commodity(client, db_session):
    dram = _seed_costing(client)
    _make_classes(db_session, dram["id"])
    prod = _product_with_supplier(client)
    client.put(f"{B}/products/{prod['id']}/bom", json={
        "lines": [{"component_class_code": "MEMORY", "label": "DDR5 ×16", "qty": 16,
                   "base_material_cost": 110, "conversion_cost": 6, "overhead_pct": 0.12}],
    })
    s = client.get(f"{B}/products/{prod['id']}/sensitivity?delta=0.2").json()
    assert s["floor_high"] > s["floor_base"] > s["floor_low"]
    assert s["swing_abs"] > 0


def test_should_cost_404_without_bom(client):
    prod = client.post(f"{B}/products", json={"product_code": "NO-BOM", "name": "No BOM"}).json()
    r = client.post(f"{B}/products/{prod['id']}/should-cost")
    assert r.status_code == 404


def test_analytics_savings_aggregation(client, db_session):
    dram = _seed_costing(client)
    _make_classes(db_session, dram["id"])
    prod = _product_with_supplier(client)
    client.put(f"{B}/products/{prod['id']}/bom", json={
        "lines": [{"component_class_code": "MEMORY", "label": "DDR5 ×16", "qty": 16,
                   "base_material_cost": 110, "conversion_cost": 6, "overhead_pct": 0.12}],
    })
    summ = client.get(f"{B}/analytics/should-cost/savings").json()
    assert summ["products_with_bom"] == 1
    rows = client.get(f"{B}/analytics/should-cost/by-supplier").json()
    assert len(rows) == 1 and rows[0]["product_id"] == prod["id"]


def test_bom_write_is_procurement_gated(client):
    prod = client.post(f"{B}/products", json={"product_code": "GATE", "name": "Gate"}).json()
    viewer = client.as_role(Role.VIEWER)
    r = viewer.put(f"{B}/products/{prod['id']}/bom", json={"lines": []})
    assert r.status_code == 403


def test_commodity_write_is_procurement_gated(client):
    viewer = client.as_role(Role.VIEWER)
    r = viewer.post(f"{B}/commodities", json={"code": "X", "name": "X"})
    assert r.status_code == 403


def test_unknown_component_class_rejected(client):
    prod = client.post(f"{B}/products", json={"product_code": "BADCLS", "name": "Bad"}).json()
    r = client.put(f"{B}/products/{prod['id']}/bom", json={
        "lines": [{"component_class_code": "NOPE", "label": "x", "qty": 1, "base_material_cost": 1}],
    })
    assert r.status_code == 422

"""Phase 3 tests: approval flow, supplier-swap, sourcing suggestions, spend."""
from __future__ import annotations

from tests.helpers import build_scenario

B = "/api/v1"


def _add_second_source(client, s, *, rank, lead, price):
    """Add a second (cheaper/slower) source for the scenario's product."""
    org = client.post(f"{B}/organizations", json={
        "name": f"Alt-{rank}", "is_supplier": True,
    }).json()
    return client.post(f"{B}/product-suppliers", json={
        "product_id": s["product_id"], "supplier_id": org["id"],
        "standard_lead_time_days": lead, "contract_price": price, "preference_rank": rank,
    }).json()


# --- approval flow --------------------------------------------------------

def test_approval_flow_happy_path(client):
    s = build_scenario(client)
    oid = s["order_id"]
    assert client.get(f"{B}/purchase-orders/{oid}").json()["status"] == "PENDING"
    assert client.post(f"{B}/purchase-orders/{oid}/status", json={"target": "APPROVED"}).status_code == 200
    assert client.post(f"{B}/purchase-orders/{oid}/status", json={"target": "PLACED"}).status_code == 200
    assert client.get(f"{B}/purchase-orders/{oid}").json()["status"] == "PLACED"


def test_illegal_order_transition_rejected(client):
    s = build_scenario(client)
    # PENDING -> PLACED skips APPROVED
    r = client.post(f"{B}/purchase-orders/{s['order_id']}/status", json={"target": "PLACED"})
    assert r.status_code == 422


def test_cannot_set_received_directly(client):
    s = build_scenario(client)
    r = client.post(f"{B}/purchase-orders/{s['order_id']}/status", json={"target": "RECEIVED"})
    assert r.status_code == 422
    assert "receiving" in r.json()["detail"]


# --- supplier swap --------------------------------------------------------

def test_resource_line_swaps_source_and_reprices(client):
    s = build_scenario(client)
    alt = _add_second_source(client, s, rank=2, lead=40, price="2900.00")
    r = client.post(
        f"{B}/purchase-orders/{s['order_id']}/items/{s['order_item_id']}/resource",
        json={"product_supplier_id": alt["id"]})
    assert r.status_code == 200
    line = r.json()
    assert line["product_supplier_id"] == alt["id"]
    assert line["unit_price"] == "2900.00"  # re-priced from new source


def test_cannot_resource_after_placed(client):
    s = build_scenario(client)
    alt = _add_second_source(client, s, rank=2, lead=40, price="2900.00")
    client.post(f"{B}/purchase-orders/{s['order_id']}/status", json={"target": "APPROVED"})
    client.post(f"{B}/purchase-orders/{s['order_id']}/status", json={"target": "PLACED"})
    r = client.post(
        f"{B}/purchase-orders/{s['order_id']}/items/{s['order_item_id']}/resource",
        json={"product_supplier_id": alt["id"]})
    assert r.status_code == 422


def test_resource_rejects_source_of_other_product(client):
    s = build_scenario(client)
    other = client.post(f"{B}/products", json={"product_code": "OTHER", "name": "X"}).json()
    bad_src = client.post(f"{B}/product-suppliers", json={
        "product_id": other["id"], "supplier_id": s["supplier_id"],
    }).json()
    r = client.post(
        f"{B}/purchase-orders/{s['order_id']}/items/{s['order_item_id']}/resource",
        json={"product_supplier_id": bad_src["id"]})
    assert r.status_code == 422


# --- sourcing suggestions -------------------------------------------------

def test_suggestions_ranked_by_preference(client):
    s = build_scenario(client)  # seed source has preference_rank 1
    _add_second_source(client, s, rank=2, lead=40, price="2900.00")
    out = client.get(f"{B}/products/{s['product_id']}/sources").json()
    assert [r["rank"] for r in out] == [1, 2]
    assert out[0]["product_supplier_id"] == s["source_id"]  # rank-1 source first


# --- spend analytics ------------------------------------------------------

def test_spend_reflects_received_units(client):
    s = build_scenario(client)  # 5 @ 3200
    # receive 2 units
    client.post(f"{B}/purchase-orders/{s['order_id']}/receipts", json={
        "location_id": s["warehouse_id"], "lines": [{"order_item_id": s["order_item_id"], "quantity": 2}],
    })
    summary = client.get(f"{B}/analytics/spend").json()
    assert summary["total_units"] == 2
    assert summary["total_spend"] == "6400.00"  # 2 * 3200
    assert summary["by_supplier"][0]["supplier_name"] == "Supermicro"
    assert summary["by_supplier"][0]["spend"] == "6400.00"


def test_spend_empty_when_nothing_received(client):
    s = build_scenario(client)
    summary = client.get(f"{B}/analytics/spend").json()
    assert summary["total_units"] == 0
    assert summary["total_spend"] == "0"

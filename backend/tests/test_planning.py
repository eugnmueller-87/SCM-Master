"""Phase 4 tests: inbound pipeline, location capacity, deployment forecast."""
from __future__ import annotations

from app.models.auth import Role
from tests.helpers import build_scenario

B = "/api/v1"


def test_inbound_pipeline_shows_outstanding(client):
    s = build_scenario(client)  # PO-1, 5 units, PENDING
    inbound = client.get(f"{B}/planning/inbound").json()
    assert len(inbound) == 1
    row = inbound[0]
    assert row["ordered"] == 5 and row["received"] == 0 and row["outstanding"] == 5

    # receive 2 -> outstanding drops to 3
    client.post(f"{B}/purchase-orders/{s['order_id']}/receipts", json={
        "location_id": s["warehouse_id"], "lines": [{"order_item_id": s["order_item_id"], "quantity": 2}],
    })
    inbound = client.get(f"{B}/planning/inbound").json()
    assert inbound[0]["outstanding"] == 3


def test_fully_received_line_leaves_pipeline(client):
    s = build_scenario(client)
    client.post(f"{B}/purchase-orders/{s['order_id']}/receipts", json={
        "location_id": s["warehouse_id"], "lines": [{"order_item_id": s["order_item_id"], "quantity": 5}],
    })
    assert client.get(f"{B}/planning/inbound").json() == []


def test_location_capacity_tracks_assets(client):
    s = build_scenario(client)
    client.post(f"{B}/purchase-orders/{s['order_id']}/receipts", json={
        "location_id": s["warehouse_id"], "lines": [{"order_item_id": s["order_item_id"], "quantity": 3}],
    })
    caps = {c["location_id"]: c for c in client.get(f"{B}/planning/capacity").json()}
    wh = caps[s["warehouse_id"]]
    assert wh["used"] == 3
    assert wh["capacity"] == 100
    assert wh["free"] == 97
    assert wh["over_capacity"] is False


def test_deployment_forecast_counts_on_hand_plus_inbound(client):
    s = build_scenario(client)  # 5 ordered
    # receive 2 -> on_hand 2, inbound 3
    client.post(f"{B}/purchase-orders/{s['order_id']}/receipts", json={
        "location_id": s["warehouse_id"], "lines": [{"order_item_id": s["order_item_id"], "quantity": 2}],
    })
    fc = client.get(f"{B}/planning/forecast").json()
    assert fc["on_hand"] == 2
    assert fc["inbound"] == 3
    assert fc["forecast_deployable"] == 5
    assert fc["deployed"] == 0


def _over_capacity_setup(client):
    """A tiny cage (cap 2) receiving 5 units -> over capacity, plus a roomy
    same-type sibling to absorb the overflow."""
    smci = client.post(f"{B}/organizations", json={"name": "SMCI", "is_supplier": True}).json()
    product = client.post(f"{B}/products", json={"product_code": "P", "name": "Server", "category": "Servers"}).json()
    source = client.post(f"{B}/product-suppliers", json={
        "product_id": product["id"], "supplier_id": smci["id"], "contract_price": "100.00"}).json()
    cage = client.post(f"{B}/locations", json={"code": "CAGE", "name": "Cage", "location_type": "WAREHOUSE", "capacity": 2}).json()
    big = client.post(f"{B}/locations", json={"code": "WH", "name": "Warehouse", "location_type": "WAREHOUSE", "capacity": 100}).json()
    order = client.post(f"{B}/purchase-orders", json={
        "order_number": "PO-OC", "supplier_id": smci["id"],
        "items": [{"product_id": product["id"], "product_supplier_id": source["id"], "quantity": 5, "unit_price": "100.00"}]}).json()
    client.post(f"{B}/purchase-orders/{order['id']}/receipts", json={
        "location_id": cage["id"], "lines": [{"order_item_id": order["items"][0]["id"], "quantity": 5}]})
    return cage, big


def test_rebalance_moves_overflow_to_sibling(client):
    cage, big = _over_capacity_setup(client)
    # before: cage 5/2 over capacity
    caps = {c["location_id"]: c for c in client.get(f"{B}/planning/capacity").json()}
    assert caps[cage["id"]]["over_capacity"] is True

    res = client.post(f"{B}/planning/capacity/{cage['id']}/rebalance", json={})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["moved"] == 3                       # 5 - cap 2
    assert body["targets"][0]["code"] == "WH"

    # after: cage back within capacity, units landed in the warehouse
    caps = {c["location_id"]: c for c in client.get(f"{B}/planning/capacity").json()}
    assert caps[cage["id"]]["used"] == 2
    assert caps[cage["id"]]["over_capacity"] is False
    assert caps[big["id"]]["used"] == 3


def test_rebalance_within_capacity_is_noop(client):
    s = build_scenario(client)
    res = client.post(f"{B}/planning/capacity/{s['warehouse_id']}/rebalance", json={})
    assert res.status_code == 200
    assert res.json()["moved"] == 0


def test_rebalance_requires_ops_role(client):
    cage, _ = _over_capacity_setup(client)
    viewer = client.as_role(Role.VIEWER)
    assert viewer.post(f"{B}/planning/capacity/{cage['id']}/rebalance", json={}).status_code == 403


def test_deployment_forecast_deploy_updates_counts(client):
    s = build_scenario(client)
    client.post(f"{B}/purchase-orders/{s['order_id']}/receipts", json={
        "location_id": s["warehouse_id"], "lines": [{"order_item_id": s["order_item_id"], "quantity": 2}]})
    aid = client.get(f"{B}/assets").json()[0]["id"]
    client.post(f"{B}/assets/{aid}/transition", json={"target": "DEPLOYED", "location_id": s["rack_id"]})
    fc = client.get(f"{B}/planning/forecast").json()
    assert fc["on_hand"] == 1
    assert fc["deployed"] == 1
    assert fc["forecast_deployable"] == 4  # 1 on-hand + 3 inbound

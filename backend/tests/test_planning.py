"""Phase 4 tests: inbound pipeline, location capacity, deployment forecast."""
from __future__ import annotations

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

    # deploy one of the on-hand assets -> on_hand 1, deployed 1
    aid = client.get(f"{B}/assets").json()[0]["id"]
    client.post(f"{B}/assets/{aid}/transition", json={"target": "DEPLOYED", "location_id": s["rack_id"]})
    fc = client.get(f"{B}/planning/forecast").json()
    assert fc["on_hand"] == 1
    assert fc["deployed"] == 1
    assert fc["forecast_deployable"] == 4  # 1 on-hand + 3 inbound

"""API tests for receiving, the asset lifecycle, the event log, and provenance."""
from __future__ import annotations

from tests.helpers import build_scenario

B = "/api/v1"


def test_partial_then_full_receipt_advances_order_status(client):
    s = build_scenario(client)
    oid, item, wh = s["order_id"], s["order_item_id"], s["warehouse_id"]

    # ordered 5; receive 2 -> PARTIALLY_RECEIVED, 2 assets
    r = client.post(f"{B}/purchase-orders/{oid}/receipts", json={
        "location_id": wh, "lines": [{"order_item_id": item, "quantity": 2}],
    })
    assert r.status_code == 201
    assert client.get(f"{B}/purchase-orders/{oid}").json()["status"] == "PARTIALLY_RECEIVED"
    assert len(client.get(f"{B}/assets").json()) == 2

    # receive remaining 3 -> RECEIVED, 5 assets total
    client.post(f"{B}/purchase-orders/{oid}/receipts", json={
        "location_id": wh, "lines": [{"order_item_id": item, "quantity": 3}],
    })
    assert client.get(f"{B}/purchase-orders/{oid}").json()["status"] == "RECEIVED"
    assert len(client.get(f"{B}/assets").json()) == 5


def test_over_receipt_is_rejected(client):
    s = build_scenario(client)
    r = client.post(f"{B}/purchase-orders/{s['order_id']}/receipts", json={
        "location_id": s["warehouse_id"],
        "lines": [{"order_item_id": s["order_item_id"], "quantity": 6}],  # ordered 5
    })
    assert r.status_code == 422
    assert "Over-receipt" in r.json()["detail"]


def test_received_asset_has_serial_location_and_provenance(client):
    s = build_scenario(client)
    client.post(f"{B}/purchase-orders/{s['order_id']}/receipts", json={
        "location_id": s["warehouse_id"], "lines": [{"order_item_id": s["order_item_id"], "quantity": 1}],
    })
    asset = client.get(f"{B}/assets").json()[0]
    assert asset["serial_number"].startswith("SN-")
    assert asset["status"] == "RECEIVED"
    assert asset["current_location_id"] == s["warehouse_id"]
    assert asset["source_order_item_id"] == s["order_item_id"]


def _receive_one(client, s):
    client.post(f"{B}/purchase-orders/{s['order_id']}/receipts", json={
        "location_id": s["warehouse_id"], "lines": [{"order_item_id": s["order_item_id"], "quantity": 1}],
    })
    return client.get(f"{B}/assets").json()[0]["id"]


def test_full_lifecycle_happy_path(client):
    s = build_scenario(client)
    aid = _receive_one(client, s)

    def t(target, **kw):
        return client.post(f"{B}/assets/{aid}/transition", json={"target": target, **kw})

    assert t("IN_STORAGE").status_code == 200
    assert t("DEPLOYED", location_id=s["rack_id"]).status_code == 200
    dep = client.get(f"{B}/assets/{aid}").json()
    assert dep["deployed_date"] is not None
    assert dep["current_location_id"] == s["rack_id"]
    assert t("MAINTENANCE").status_code == 200
    assert t("DEPLOYED").status_code == 200
    assert t("DECOMMISSIONED").status_code == 200
    assert client.get(f"{B}/assets/{aid}").json()["decommissioned_date"] is not None
    assert t("DISPOSED").status_code == 200


def test_illegal_transition_rejected(client):
    s = build_scenario(client)
    aid = _receive_one(client, s)
    r = client.post(f"{B}/assets/{aid}/transition", json={"target": "DISPOSED"})
    assert r.status_code == 422


def test_event_log_records_every_change(client):
    s = build_scenario(client)
    aid = _receive_one(client, s)
    client.post(f"{B}/assets/{aid}/transition", json={"target": "IN_STORAGE", "actor": "eugen"})
    client.post(f"{B}/assets/{aid}/move", json={"location_id": s["rack_id"]})
    events = client.get(f"{B}/assets/{aid}/events").json()
    types = [e["event_type"] for e in events]
    assert types == ["RECEIVED", "STATUS_CHANGED", "MOVED"]
    assert events[1]["actor"] == "eugen"


def test_provenance_both_directions(client):
    s = build_scenario(client)
    aid = _receive_one(client, s)
    prov = client.get(f"{B}/assets/{aid}/provenance").json()
    assert prov["order_number"] == "PO-1"
    assert prov["supplier_name"] == "Supermicro"
    assert prov["unit_price"] == "3200.00"

    line_assets = client.get(f"{B}/order-items/{s['order_item_id']}/assets").json()
    assert len(line_assets) == 1
    assert line_assets[0]["id"] == aid


def test_asset_list_filters_by_status(client):
    s = build_scenario(client)
    aid = _receive_one(client, s)
    client.post(f"{B}/assets/{aid}/transition", json={"target": "IN_STORAGE"})
    assert len(client.get(f"{B}/assets?status=IN_STORAGE").json()) == 1
    assert len(client.get(f"{B}/assets?status=DEPLOYED").json()) == 0

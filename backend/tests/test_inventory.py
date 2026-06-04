"""Inventory read model — GET /planning/inventory (design handoff §6.3)."""
from __future__ import annotations

from tests.helpers import build_scenario

B = "/api/v1"


def test_inventory_reports_on_order_for_open_line(client):
    s = build_scenario(client)  # 5 units ordered, nothing received -> all inbound
    rows = client.get(f"{B}/planning/inventory").json()
    row = next((r for r in rows if r["product_id"] == s["product_id"]), None)
    assert row is not None, "product with open inbound should appear"
    assert row["on_order"] == 5
    assert row["on_hand"] == 0
    assert row["lead_time_days"] == 21   # from the scenario's preferred source
    assert row["unit_price"] == 3200.0
    # the scenario order line carries no ETA, so next_eta is legitimately null
    assert "next_eta" in row


def test_inventory_counts_on_hand_after_receipt(client):
    s = build_scenario(client)
    client.post(f"{B}/purchase-orders/{s['order_id']}/receipts", json={
        "location_id": s["warehouse_id"],
        "lines": [{"order_item_id": s["order_item_id"], "quantity": 3}]})
    row = next(r for r in client.get(f"{B}/planning/inventory").json()
               if r["product_id"] == s["product_id"])
    assert row["on_hand"] == 3
    assert row["on_order"] == 2   # 5 ordered - 3 received


def test_inventory_required_input_fields_present(client):
    build_scenario(client)
    rows = client.get(f"{B}/planning/inventory").json()
    assert rows
    for k in ("product_id", "on_hand", "capacity", "safety_stock", "daily_burn",
              "lead_time_days", "on_order", "next_eta", "unit_price"):
        assert k in rows[0]

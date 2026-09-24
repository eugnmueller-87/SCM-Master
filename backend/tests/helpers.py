"""Helpers to build a known scenario through the API in tests."""
from __future__ import annotations

B = "/api/v1"


def build_scenario(client):
    """Create orgs, a product with a source, locations, and a 1-line PO.

    Returns a dict of the ids tests need.
    """
    smci = client.post(f"{B}/organizations", json={
        "code": "SMCI", "name": "Supermicro", "is_supplier": True, "is_manufacturer": True,
    }).json()
    product = client.post(f"{B}/products", json={
        "product_code": "SRV-1U", "name": "1U Server", "category": "server",
    }).json()
    source = client.post(f"{B}/product-suppliers", json={
        "product_id": product["id"], "supplier_id": smci["id"],
        "standard_lead_time_days": 21, "min_order_quantity": 1,
        "contract_price": "3200.00", "preference_rank": 1,
    }).json()
    warehouse = client.post(f"{B}/locations", json={
        "code": "WH", "name": "Transit WH", "location_type": "WAREHOUSE", "capacity": 100,
    }).json()
    dc = client.post(f"{B}/locations", json={
        "code": "DC", "name": "DC1", "location_type": "DATACENTER",
    }).json()
    rack = client.post(f"{B}/locations", json={
        "code": "DC-R01", "name": "Rack 01", "location_type": "RACK",
        "parent_id": dc["id"], "capacity": 42,
    }).json()
    order = client.post(f"{B}/purchase-orders", json={
        "order_number": "PO-1", "supplier_id": smci["id"], "destination_id": warehouse["id"],
        "items": [{
            "product_id": product["id"], "product_supplier_id": source["id"],
            "quantity": 5, "unit_price": "3200.00",
        }],
    }).json()
    return {
        "supplier_id": smci["id"],
        "product_id": product["id"],
        "source_id": source["id"],
        "warehouse_id": warehouse["id"],
        "rack_id": rack["id"],
        "order_id": order["id"],
        "order_item_id": order["items"][0]["id"],
    }


def stub_sourcing(monkeypatch, fake):
    """Install a per-line copilot stub of the shape ``fake(db, product_id, desired_qty=None)``.

    The purchasing run asks the copilot for its lines together (``copilot.recommend_sourcing_many``:
    signals gathered on the session, the model asked once per line side by side), so the seam a
    test stubs is ``judge_sourcing``, which sees the gathered signals and no session. This adapter
    keeps the tests' fakes as they were written: the product id and the quantity are read back out
    of the signals the real gatherer produced.
    """
    from app.agent import copilot

    def by_signal(sig):
        ctx = sig.get("source_context", {})
        return fake(None, ctx.get("product_id"), ctx.get("desired_qty"))
    monkeypatch.setattr(copilot, "judge_sourcing", by_signal)

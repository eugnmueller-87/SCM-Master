"""API tests for catalog CRUD and its business rules."""
from __future__ import annotations

B = "/api/v1"


def test_create_and_get_product(client):
    r = client.post(f"{B}/products", json={"product_code": "P1", "name": "Thing"})
    assert r.status_code == 201
    pid = r.json()["id"]
    assert client.get(f"{B}/products/{pid}").json()["product_code"] == "P1"


def test_duplicate_product_code_conflicts(client):
    client.post(f"{B}/products", json={"product_code": "DUP", "name": "A"})
    r = client.post(f"{B}/products", json={"product_code": "DUP", "name": "B"})
    assert r.status_code == 409


def test_get_missing_is_404(client):
    assert client.get(f"{B}/products/nope").status_code == 404


def test_product_supplier_requires_supplier_role(client):
    intel = client.post(f"{B}/organizations", json={
        "name": "Intel", "is_supplier": False, "is_manufacturer": True,
    }).json()
    product = client.post(f"{B}/products", json={"product_code": "P2", "name": "CPU"}).json()
    r = client.post(f"{B}/product-suppliers", json={
        "product_id": product["id"], "supplier_id": intel["id"],
    })
    assert r.status_code == 422
    assert "not flagged as a supplier" in r.json()["detail"]


def test_patch_updates_only_sent_fields(client):
    p = client.post(f"{B}/products", json={
        "product_code": "P3", "name": "Orig", "category": "server",
    }).json()
    client.patch(f"{B}/products/{p['id']}", json={"name": "Renamed"})
    got = client.get(f"{B}/products/{p['id']}").json()
    assert got["name"] == "Renamed"
    assert got["category"] == "server"  # untouched

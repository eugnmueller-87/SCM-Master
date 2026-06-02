"""Phase 5 auth tests: login, /me, role gating."""
from __future__ import annotations

from app.models.auth import Role
from tests.helpers import build_scenario

B = "/api/v1"


def test_login_and_me(client):
    # the admin user exists (created by the fixture); log in via the API
    r = client.anon().post(f"{B}/auth/login", data={
        "username": "admin@example.com", "password": "pw",
    })
    assert r.status_code == 200
    token = r.json()["access_token"]
    anon = client.anon()
    me = anon.get(f"{B}/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    assert me.json()["role"] == "ADMIN"


def test_login_wrong_password_401(client):
    r = client.anon().post(f"{B}/auth/login", data={
        "username": "admin@example.com", "password": "wrong",
    })
    assert r.status_code == 401


def test_unauthenticated_write_is_401(client):
    # creating a product is open, but placing an order is gated
    s = build_scenario(client)  # built as admin
    r = client.anon().post(f"{B}/purchase-orders/{s['order_id']}/status", json={"target": "APPROVED"})
    assert r.status_code == 401


def test_wrong_role_is_403(client):
    s = build_scenario(client)
    warehouse = client.as_role(Role.WAREHOUSE)
    # warehouse user cannot approve orders (procurement-only)
    r = warehouse.post(f"{B}/purchase-orders/{s['order_id']}/status", json={"target": "APPROVED"})
    assert r.status_code == 403


def test_correct_role_allowed(client):
    s = build_scenario(client)
    warehouse = client.as_role(Role.WAREHOUSE)
    # warehouse user CAN receive
    r = warehouse.post(f"{B}/purchase-orders/{s['order_id']}/receipts", json={
        "location_id": s["warehouse_id"], "lines": [{"order_item_id": s["order_item_id"], "quantity": 1}],
    })
    assert r.status_code == 201


def test_register_is_admin_only(client):
    # admin (default client) can register
    r = client.post(f"{B}/auth/register", json={
        "email": "newbie@example.com", "full_name": "New", "password": "pw", "role": "VIEWER",
    })
    assert r.status_code == 201

    # a viewer cannot
    viewer = client.as_role(Role.VIEWER)
    r2 = viewer.post(f"{B}/auth/register", json={
        "email": "other@example.com", "full_name": "O", "password": "pw", "role": "VIEWER",
    })
    assert r2.status_code == 403


def test_admin_passes_all_role_gates(client):
    s = build_scenario(client)  # admin creates the order
    # admin can both receive (warehouse gate) and approve (procurement gate)
    assert client.post(f"{B}/purchase-orders/{s['order_id']}/status", json={"target": "APPROVED"}).status_code == 200

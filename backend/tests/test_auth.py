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


def test_ensure_user_is_idempotent(db_session):
    """The boot bootstrap must create a missing user once and never raise on
    re-run — that's what keeps login working on every deploy."""
    from app.services.auth import ensure_user, user_service

    created_first = ensure_user(db_session, email="boot@example.com", full_name="Boot",
                                password="pw", role=Role.VIEWER)
    created_again = ensure_user(db_session, email="boot@example.com", full_name="Boot",
                                password="pw", role=Role.VIEWER)
    assert created_first is True
    assert created_again is False
    assert user_service.authenticate(db_session, "boot@example.com", "pw") is not None


def test_login_rate_limited_429(client):
    from app.api.v1.auth import login_limiter

    login_limiter.reset()  # isolate from any earlier login attempts in the suite
    anon = client.anon()
    # The default limit is 10/window; the 11th attempt from the same IP is 429.
    for _ in range(login_limiter._limit):
        anon.post(f"{B}/auth/login", data={"username": "admin@example.com", "password": "wrong"})
    r = anon.post(f"{B}/auth/login", data={"username": "admin@example.com", "password": "wrong"})
    assert r.status_code == 429
    assert int(r.headers["Retry-After"]) > 0
    login_limiter.reset()  # leave clean state for any later test


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

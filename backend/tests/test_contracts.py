"""Contract-lifecycle enrichment on /product-suppliers (Phase: design handoff §6.2)."""
from __future__ import annotations

from datetime import date, timedelta

from app.models.catalog import ProductSupplier
from app.services import contracts
from tests.helpers import build_scenario

B = "/api/v1"


def test_product_supplier_exposes_contract_fields(client):
    s = build_scenario(client)
    rows = client.get(f"{B}/product-suppliers?limit=1000").json()
    row = next(r for r in rows if r["id"] == s["source_id"])
    for k in ("contract_status", "term_start", "term_end", "annual_budget", "ytd_spend"):
        assert k in row


def test_status_derived_from_terms(client, db_session):
    build_scenario(client)
    ps = db_session.scalars(__import__("sqlalchemy").select(ProductSupplier)).first()
    today = date(2026, 6, 1)
    # no terms -> DRAFT
    ps.term_start = ps.term_end = None
    ps.contract_status = None
    assert contracts.derive_status(ps, today=today) == "DRAFT"
    # ends in 20 days -> EXPIRING
    ps.term_start, ps.term_end = date(2025, 1, 1), today + timedelta(days=20)
    assert contracts.derive_status(ps, today=today) == "EXPIRING"
    # ends in 45 days -> RENEWAL_DUE
    ps.term_end = today + timedelta(days=45)
    assert contracts.derive_status(ps, today=today) == "RENEWAL_DUE"
    # ends in 300 days -> ACTIVE
    ps.term_end = today + timedelta(days=300)
    assert contracts.derive_status(ps, today=today) == "ACTIVE"
    # past end -> EXPIRED
    ps.term_end = today - timedelta(days=1)
    assert contracts.derive_status(ps, today=today) == "EXPIRED"
    # stored status always wins
    ps.contract_status = "SUPERSEDED"
    assert contracts.derive_status(ps, today=today) == "SUPERSEDED"


def test_ytd_spend_from_received_assets(client, db_session):
    """Receiving units against a source's product+supplier accrues ytd_spend."""
    s = build_scenario(client)
    # receive 2 units of the scenario product (unit_price 3200 from the source)
    client.post(f"{B}/purchase-orders/{s['order_id']}/receipts", json={
        "location_id": s["warehouse_id"],
        "lines": [{"order_item_id": s["order_item_id"], "quantity": 2}]})
    row = next(r for r in client.get(f"{B}/product-suppliers?limit=1000").json()
               if r["id"] == s["source_id"])
    assert float(row["ytd_spend"]) == 6400.0  # 2 x 3200

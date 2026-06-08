"""Supplier onboarding + the hard compliance gate on ordering.

A supplier onboarded through /suppliers/onboard starts DRAFT and is NOT
orderable. It becomes orderable only after a risk assessment + signed DPA and
NDA, then explicit approval. Legacy/seeded suppliers (created via the plain
/organizations route) keep the APPROVED default so existing flows are unaffected.
"""
from __future__ import annotations

B = "/api/v1"


def _new_supplier(client, name="Acme GmbH"):
    return client.post(f"{B}/suppliers/onboard", json={"name": name}).json()


def _full_onboard(client, org_id):
    client.post(f"{B}/suppliers/{org_id}/risk-assessment",
                json={"risk_level": "low", "risk_notes": "tier-2, no PII"})
    client.post(f"{B}/suppliers/{org_id}/documents/dpa",
                json={"signed": True, "reference": "DPA-2026-001.pdf"})
    client.post(f"{B}/suppliers/{org_id}/documents/nda",
                json={"signed": True, "reference": "NDA-2026-001.pdf"})
    return client.post(f"{B}/suppliers/{org_id}/approve")


# --- onboarding state machine ---------------------------------------------

def test_onboard_starts_draft_and_not_orderable(client):
    org = _new_supplier(client)
    assert org["onboarding_status"] == "DRAFT"
    assert org["is_orderable"] is False
    assert org["dpa_signed"] is False and org["nda_signed"] is False


def test_risk_assessment_normalises_and_moves_to_review(client):
    org = _new_supplier(client)
    r = client.post(f"{B}/suppliers/{org['id']}/risk-assessment",
                    json={"risk_level": "medium", "risk_notes": "single-region"})
    body = r.json()
    assert r.status_code == 200
    assert body["risk_level"] == "MEDIUM"          # upper-cased
    assert body["onboarding_status"] == "IN_REVIEW"
    assert body["is_orderable"] is False           # still gated


def test_invalid_risk_level_rejected(client):
    org = _new_supplier(client)
    r = client.post(f"{B}/suppliers/{org['id']}/risk-assessment",
                    json={"risk_level": "catastrophic"})
    assert r.status_code == 422


def test_cannot_approve_until_gate_satisfied(client):
    org = _new_supplier(client)
    # nothing done yet
    r = client.post(f"{B}/suppliers/{org['id']}/approve")
    assert r.status_code == 422
    assert "risk assessment" in r.json()["detail"]

    # risk only — still missing both docs
    client.post(f"{B}/suppliers/{org['id']}/risk-assessment", json={"risk_level": "low"})
    r = client.post(f"{B}/suppliers/{org['id']}/approve")
    assert r.status_code == 422
    assert "DPA" in r.json()["detail"] and "NDA" in r.json()["detail"]

    # DPA only — NDA still missing
    client.post(f"{B}/suppliers/{org['id']}/documents/dpa", json={"signed": True})
    r = client.post(f"{B}/suppliers/{org['id']}/approve")
    assert r.status_code == 422
    assert "NDA" in r.json()["detail"]


def test_full_onboarding_makes_supplier_orderable(client):
    org = _new_supplier(client)
    r = _full_onboard(client, org["id"])
    assert r.status_code == 200
    body = r.json()
    assert body["onboarding_status"] == "APPROVED"
    assert body["is_orderable"] is True
    assert body["dpa_reference"] == "DPA-2026-001.pdf"
    assert body["nda_reference"] == "NDA-2026-001.pdf"
    assert body["risk_assessed_at"] is not None


# --- the hard gate on order creation --------------------------------------

def _warehouse_and_source(client, supplier_id):
    """A product + a warehouse + a source for `supplier_id`, so we can attempt a PO."""
    wh = client.post(f"{B}/locations", json={
        "code": "WH-G", "name": "Gate WH", "location_type": "WAREHOUSE", "capacity": 100,
    }).json()
    prod = client.post(f"{B}/products", json={"product_code": "GATE-1", "name": "Gate Box"}).json()
    src = client.post(f"{B}/product-suppliers", json={
        "product_id": prod["id"], "supplier_id": supplier_id,
        "standard_lead_time_days": 10, "contract_price": "1000.00", "preference_rank": 1,
    }).json()
    return wh, prod, src


def test_order_blocked_for_un_onboarded_supplier(client):
    org = _new_supplier(client, name="Unvetted Ltd")   # DRAFT
    wh, prod, src = _warehouse_and_source(client, org["id"])
    r = client.post(f"{B}/purchase-orders", json={
        "order_number": "PO-GATE-1", "supplier_id": org["id"], "destination_id": wh["id"],
        "items": [{"product_id": prod["id"], "product_supplier_id": src["id"],
                   "quantity": 1, "unit_price": "1000.00"}],
    })
    assert r.status_code == 422
    assert "not onboarded" in r.json()["detail"]


def test_order_allowed_after_onboarding(client):
    org = _new_supplier(client, name="Vetted Ltd")
    wh, prod, src = _warehouse_and_source(client, org["id"])
    _full_onboard(client, org["id"])                   # -> APPROVED
    r = client.post(f"{B}/purchase-orders", json={
        "order_number": "PO-GATE-2", "supplier_id": org["id"], "destination_id": wh["id"],
        "items": [{"product_id": prod["id"], "product_supplier_id": src["id"],
                   "quantity": 1, "unit_price": "1000.00"}],
    })
    assert r.status_code == 201


def test_legacy_supplier_unaffected(client):
    """A supplier created the old way (plain /organizations) defaults APPROVED."""
    org = client.post(f"{B}/organizations", json={"name": "Legacy Co", "is_supplier": True}).json()
    assert org["onboarding_status"] == "APPROVED"
    assert org["is_orderable"] is True

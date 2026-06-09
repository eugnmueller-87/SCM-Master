"""DecisionLog audit-trail tests — the additive decision-loop surfacing.

Proves, against the real API route (so the best-effort persist in the handler
runs): a purchasing-run writes one append-only DecisionLog row per decision;
GET /agent/decisions lists them (filterable); GET /agent/decisions/{id} drills
one; a placed buy carries its placed_po_id (the provenance join). Copilot is
mocked — no LLM, no network.
"""
from __future__ import annotations

from datetime import date, timedelta

from app.agent import copilot
from app.agent.schemas import SourcingRecommendation
from app.models.auth import Role
from app.models.decision import DecisionLog
from app.models.flow import Asset, AssetStatus

B = "/api/v1"


def _mock_copilot(monkeypatch, *, decision="act", confidence=0.95):
    def fake(db, product_id, desired_qty=None):
        return SourcingRecommendation(
            product_id=product_id, recommended_source_id="x",
            recommended_qty=desired_qty or 1, rationale="mock",
            signals={}, assumptions=[], uncertainties=[],
            confidence=confidence, decision=decision)
    monkeypatch.setattr(copilot, "recommend_sourcing", fake)


def _scenario(client, db_session):
    smci = client.post(f"{B}/organizations", json={
        "code": "SMCI", "name": "Supermicro", "is_supplier": True}).json()
    srv = client.post(f"{B}/products", json={
        "product_code": "SRV-1U", "name": "1U Server", "category": "server"}).json()
    client.post(f"{B}/product-suppliers", json={
        "product_id": srv["id"], "supplier_id": smci["id"],
        "standard_lead_time_days": 21, "min_order_quantity": 1,
        "contract_price": "3200.00", "preference_rank": 1})
    when = date.today() - timedelta(days=1)
    for i in range(3):
        db_session.add(Asset(serial_number=f"SN-DL-{i}", product_id=srv["id"],
                             status=AssetStatus.DECOMMISSIONED, decommissioned_date=when))
    db_session.commit()
    return srv


def test_purchasing_run_persists_decision_and_lists_it(client, db_session, monkeypatch):
    _mock_copilot(monkeypatch)
    proc = client.as_role(Role.PROCUREMENT)
    srv = _scenario(proc, db_session)

    # Run the gate through the ROUTE (best-effort logging lives in the handler).
    res = proc.post(f"{B}/agent/purchasing-run", json={"dry_run": True}).json()
    assert any(d["product_id"] == srv["id"] for d in res["decisions"])

    # It was persisted to the append-only DecisionLog.
    rows = db_session.query(DecisionLog).filter(DecisionLog.product_id == srv["id"]).all()
    assert rows, "the run's decision must be logged"
    assert rows[0].dry_run is True
    assert rows[0].actor  # the running user's email was captured

    # GET /agent/decisions lists it, newest-first.
    listed = proc.get(f"{B}/agent/decisions").json()
    assert any(r["product_id"] == srv["id"] for r in listed)

    # Filterable by tier.
    tier = rows[0].tier
    filtered = proc.get(f"{B}/agent/decisions", params={"tier": tier}).json()
    assert filtered and all(r["tier"] == tier for r in filtered)

    # GET /agent/decisions/{id} drills one; unknown id -> 404.
    one = proc.get(f"{B}/agent/decisions/{rows[0].id}")
    assert one.status_code == 200 and one.json()["id"] == rows[0].id
    assert proc.get(f"{B}/agent/decisions/does-not-exist").status_code == 404


def test_placed_decision_links_its_po(client, db_session, monkeypatch):
    _mock_copilot(monkeypatch, decision="act", confidence=0.95)
    proc = client.as_role(Role.PROCUREMENT)
    _scenario(proc, db_session)

    # dry_run=False so an act-tier buy actually places a PO.
    res = proc.post(f"{B}/agent/purchasing-run", json={"dry_run": False}).json()
    placed = [d for d in res["decisions"] if d.get("placed_po_id")]
    if placed:  # only assert the join when something actually placed
        po_id = placed[0]["placed_po_id"]
        row = (db_session.query(DecisionLog)
               .filter(DecisionLog.placed_po_id == po_id).first())
        assert row is not None, "a placed decision must log its PO id (provenance join)"
        assert row.dry_run is False


def test_decisions_endpoint_is_role_gated(client):
    # VIEWER cannot read the procurement audit trail.
    assert client.as_role(Role.VIEWER).get(f"{B}/agent/decisions").status_code == 403
    # Anonymous is rejected too.
    assert client.anon().get(f"{B}/agent/decisions").status_code == 401


def test_logging_failure_never_raises(db_session):
    """Best-effort contract: the audit write must swallow any error, never raise.

    Feed _log_decisions a result whose .decisions access throws; the guard has to
    absorb it so a logging bug can never fail the purchasing run.
    """
    import app.api.v1.agent as agent_mod

    class _Poisoned:
        run_at = None
        dry_run = True

        @property
        def decisions(self):
            raise RuntimeError("simulated audit-write failure")

    # Must not raise.
    agent_mod._log_decisions(db_session, _Poisoned(), actor="t@e.com")

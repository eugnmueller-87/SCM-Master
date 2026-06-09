"""Weekly purchasing run tests — copilot mocked, no LLM, no network.

Builds a scenario via the API, then drives the run service directly:
  - a product with units decommissioned in-period (lifecycle trigger);
  - a product with NO trigger (must produce no decision);
  - netting against inbound;
  - tier gates: act vs propose vs escalate (incl. over-cap escalation);
  - dry_run places nothing.
"""
from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import select

from app.agent import copilot, purchasing
from app.agent.schemas import SourcingRecommendation
from app.models.flow import Asset, AssetStatus
from app.models.procurement import PurchaseOrder

B = "/api/v1"


# --- scenario builder -----------------------------------------------------

def _org(client, code, name):
    return client.post(f"{B}/organizations", json={
        "code": code, "name": name, "is_supplier": True}).json()


def _product(client, code, name, category="server"):
    return client.post(f"{B}/products", json={
        "product_code": code, "name": name, "category": category}).json()


def _source(client, product_id, supplier_id, *, price, moq=1, rank=1, lead=21):
    return client.post(f"{B}/product-suppliers", json={
        "product_id": product_id, "supplier_id": supplier_id,
        "standard_lead_time_days": lead, "min_order_quantity": moq,
        "contract_price": price, "preference_rank": rank}).json()


def _mock_copilot(monkeypatch, *, decision="act", confidence=0.9):
    def fake(db, product_id, desired_qty=None):
        return SourcingRecommendation(
            product_id=product_id, recommended_source_id="x",
            recommended_qty=desired_qty or 1,
            rationale="mock rationale", signals={}, assumptions=[], uncertainties=[],
            confidence=confidence, decision=decision)
    monkeypatch.setattr(copilot, "recommend_sourcing", fake)


def _decommission_assets(db_session, product_id, n, *, days_ago=1):
    """Insert n assets for a product, marked DECOMMISSIONED within the period."""
    when = date.today() - timedelta(days=days_ago)
    for i in range(n):
        db_session.add(Asset(
            serial_number=f"SN-{product_id[:6]}-{i}-{days_ago}",
            product_id=product_id, status=AssetStatus.DECOMMISSIONED,
            decommissioned_date=when))
    db_session.commit()


def _deploy_assets(db_session, product_id, n, *, spread_days=5):
    """Insert n DEPLOYED assets with recent deployment dates -> a usage rate."""
    today = date.today()
    for i in range(n):
        db_session.add(Asset(
            serial_number=f"DEP-{product_id[:6]}-{i}",
            product_id=product_id, status=AssetStatus.DEPLOYED,
            deployed_date=today - timedelta(days=i * spread_days)))
    db_session.commit()


# --- tests ----------------------------------------------------------------

def test_lifecycle_trigger_creates_decision_no_trigger_does_not(client, db_session, monkeypatch):
    _mock_copilot(monkeypatch, decision="act", confidence=0.95)
    smci = _org(client, "SMCI", "Supermicro")
    srv = _product(client, "SRV-1U", "1U Server")
    _source(client, srv["id"], smci["id"], price="3200.00")
    # a second product with a source but NO decommissioning -> no trigger
    nic = _product(client, "NIC-25G", "NIC", category="network")
    _source(client, nic["id"], smci["id"], price="520.00")

    _decommission_assets(db_session, srv["id"], 3)

    res = purchasing.run_weekly_purchasing(db_session, dry_run=True, period_days=7)

    pids = {d.product_id for d in res.decisions}
    assert srv["id"] in pids, "lifecycle replacement should create a decision"
    assert nic["id"] not in pids, "a product with no trigger must NOT be bought"
    srv_dec = next(d for d in res.decisions if d.product_id == srv["id"])
    assert srv_dec.trigger.type == "lifecycle_replacement"
    assert srv_dec.trigger.evidence["decommissioned_in_period"] == 3
    assert srv_dec.qty == 3  # replace_ratio 1.0, no inbound to net


def test_forecast_shortfall_triggers_a_justified_buy(client, db_session, monkeypatch):
    """Usage history (deployments) with no stock -> forecast projects demand ->
    a forecast_shortfall buy decision, with the usage numbers as evidence."""
    _mock_copilot(monkeypatch, decision="act", confidence=0.95)
    smci = _org(client, "SMCI", "Supermicro")
    srv = _product(client, "SRV-1U", "1U Server")
    _source(client, srv["id"], smci["id"], price="3200.00")
    _deploy_assets(db_session, srv["id"], 12)   # steady usage, nothing on hand

    res = purchasing.run_weekly_purchasing(db_session, dry_run=True, period_days=7)
    dec = next((d for d in res.decisions if d.product_id == srv["id"]), None)
    assert dec is not None, "forecast shortfall should justify a buy"
    assert dec.trigger.type == "forecast_shortfall"
    assert dec.trigger.evidence["usage_rate_per_day"] > 0
    assert dec.qty > 0


def test_net_against_inbound(client, db_session, monkeypatch):
    _mock_copilot(monkeypatch)
    smci = _org(client, "SMCI", "Supermicro")
    srv = _product(client, "SRV-1U", "1U Server")
    src = _source(client, srv["id"], smci["id"], price="3200.00")
    # open PO with 2 units inbound for this product
    order = client.post(f"{B}/purchase-orders", json={
        "order_number": "PO-INB", "supplier_id": smci["id"],
        "items": [{"product_id": srv["id"], "product_supplier_id": src["id"],
                   "quantity": 2, "unit_price": "3200.00"}]}).json()
    assert order["status"] == "PENDING"

    _decommission_assets(db_session, srv["id"], 5)  # gross need 5
    res = purchasing.run_weekly_purchasing(db_session, dry_run=True, period_days=7)
    dec = next(d for d in res.decisions if d.product_id == srv["id"])
    assert dec.qty == 3, "5 needed - 2 inbound = 3 net"


def test_moq_rounds_up(client, db_session, monkeypatch):
    _mock_copilot(monkeypatch)
    smci = _org(client, "SMCI", "Supermicro")
    srv = _product(client, "SRV-1U", "1U Server")
    _source(client, srv["id"], smci["id"], price="100.00", moq=10)
    _decommission_assets(db_session, srv["id"], 3)  # need 3, MOQ 10
    res = purchasing.run_weekly_purchasing(db_session, dry_run=True, period_days=7)
    dec = next(d for d in res.decisions if d.product_id == srv["id"])
    assert dec.qty == 10


def test_no_contracted_source_escalates(client, db_session, monkeypatch):
    _mock_copilot(monkeypatch)
    _org(client, "SMCI", "Supermicro")
    srv = _product(client, "SRV-1U", "1U Server")  # NO source created
    _decommission_assets(db_session, srv["id"], 4)
    res = purchasing.run_weekly_purchasing(db_session, dry_run=True, period_days=7)
    dec = next(d for d in res.decisions if d.product_id == srv["id"])
    assert dec.tier == "escalate"
    assert dec.supplier_id is None


def test_act_tier_when_all_gates_pass(client, db_session, monkeypatch):
    _mock_copilot(monkeypatch, decision="act", confidence=0.95)
    smci = _org(client, "SMCI", "Supermicro")
    srv = _product(client, "SRV-1U", "1U Server")
    _source(client, srv["id"], smci["id"], price="100.00")  # cheap -> under cap
    _decommission_assets(db_session, srv["id"], 5)
    res = purchasing.run_weekly_purchasing(db_session, dry_run=True, period_days=7)
    dec = next(d for d in res.decisions if d.product_id == srv["id"])
    assert dec.tier == "act"
    assert dec.placed_po_id is None  # dry run: classified act but not placed


def test_over_cap_bundle_escalates(client, db_session, monkeypatch):
    _mock_copilot(monkeypatch, decision="act", confidence=0.99)
    smci = _org(client, "SMCI", "Supermicro")
    srv = _product(client, "SRV-1U", "1U Server")
    # huge unit price so the bundle clears the escalate threshold (default €200k):
    # 2 * 120_000 = 240_000.
    _source(client, srv["id"], smci["id"], price="120000.00")
    _decommission_assets(db_session, srv["id"], 2)
    res = purchasing.run_weekly_purchasing(db_session, dry_run=True, period_days=7)
    dec = next(d for d in res.decisions if d.product_id == srv["id"])
    assert dec.tier == "escalate", "bundle over escalate threshold must escalate"


def test_low_confidence_does_not_act(client, db_session, monkeypatch):
    _mock_copilot(monkeypatch, decision="recommend", confidence=0.4)
    smci = _org(client, "SMCI", "Supermicro")
    srv = _product(client, "SRV-1U", "1U Server")
    _source(client, srv["id"], smci["id"], price="100.00")
    _decommission_assets(db_session, srv["id"], 5)
    res = purchasing.run_weekly_purchasing(db_session, dry_run=True, period_days=7)
    dec = next(d for d in res.decisions if d.product_id == srv["id"])
    assert dec.tier in {"propose", "escalate"}
    assert dec.tier != "act"


def test_dry_run_false_places_po(client, db_session, monkeypatch):
    _mock_copilot(monkeypatch, decision="act", confidence=0.95)
    smci = _org(client, "SMCI", "Supermicro")
    srv = _product(client, "SRV-1U", "1U Server")
    _source(client, srv["id"], smci["id"], price="100.00")
    _decommission_assets(db_session, srv["id"], 5)

    before = db_session.scalars(select(PurchaseOrder)).all()
    res = purchasing.run_weekly_purchasing(db_session, dry_run=False, period_days=7)
    dec = next(d for d in res.decisions if d.product_id == srv["id"])
    assert dec.tier == "act"
    assert dec.placed_po_id is not None, "act + dry_run=False should place a PO"
    after = db_session.scalars(select(PurchaseOrder)).all()
    assert len(after) == len(before) + 1
    assert res.summary["placed"] == 1


def test_one_po_per_supplier_multiline(client, db_session, monkeypatch):
    """Two triggered products from the SAME supplier -> ONE multi-line PO."""
    _mock_copilot(monkeypatch, decision="act", confidence=0.95)
    smci = _org(client, "SMCI", "Supermicro")
    srv = _product(client, "SRV-1U", "1U Server")
    ssd = _product(client, "SSD-NVME", "NVMe SSD", category="storage")
    _source(client, srv["id"], smci["id"], price="100.00")
    _source(client, ssd["id"], smci["id"], price="50.00")
    _decommission_assets(db_session, srv["id"], 2)
    _decommission_assets(db_session, ssd["id"], 4)

    before_ids = {p.id for p in db_session.scalars(select(PurchaseOrder)).all()}
    res = purchasing.run_weekly_purchasing(db_session, dry_run=False, period_days=7)
    after = db_session.scalars(select(PurchaseOrder)).all()

    # exactly one new PO for the supplier, carrying both product lines
    new_pos = [p for p in after if p.id not in before_ids]
    assert len(new_pos) == 1, "one supplier -> exactly one PO"
    po = new_pos[0]
    line_products = {i.product_id for i in po.items}
    assert line_products == {srv["id"], ssd["id"]}, "both lines on the same PO"

    # both decisions share the single placed_po_id
    placed_ids = {d.placed_po_id for d in res.decisions if d.tier == "act"}
    assert placed_ids == {po.id}
    assert res.summary["placed"] == 1  # counts POs, not lines


def test_bundle_escalates_if_any_line_escalates(client, db_session, monkeypatch):
    """If one line in a supplier bundle escalates, the whole PO escalates
    (can't split a single invoice-matched PO across tiers)."""
    smci = _org(client, "SMCI", "Supermicro")
    srv = _product(client, "SRV-1U", "1U Server")
    ssd = _product(client, "SSD-NVME", "NVMe SSD", category="storage")
    _source(client, srv["id"], smci["id"], price="100.00")
    _source(client, ssd["id"], smci["id"], price="50.00")
    _decommission_assets(db_session, srv["id"], 2)
    _decommission_assets(db_session, ssd["id"], 4)

    # one line low-confidence -> bundle confidence is the weakest link
    def fake(db, product_id, desired_qty=None):
        conf = 0.95 if product_id == srv["id"] else 0.3
        dec = "act" if product_id == srv["id"] else "escalate"
        return SourcingRecommendation(
            product_id=product_id, recommended_source_id="x",
            recommended_qty=desired_qty or 1, rationale="m", signals={},
            assumptions=[], uncertainties=[], confidence=conf, decision=dec)
    monkeypatch.setattr(copilot, "recommend_sourcing", fake)

    res = purchasing.run_weekly_purchasing(db_session, dry_run=True, period_days=7)
    tiers = {d.tier for d in res.decisions}
    assert tiers == {"escalate"}, "a weak line drags the whole supplier PO to escalate"


def test_confirm_places_only_approved_supplier(client, db_session, monkeypatch):
    """approve->place: confirming supplier A places A's PO and leaves B unplaced."""
    _mock_copilot(monkeypatch, decision="act", confidence=0.95)
    a = _org(client, "A", "Supplier A")
    b = _org(client, "B", "Supplier B")
    pa = _product(client, "PA", "Prod A")
    pb = _product(client, "PB", "Prod B")
    _source(client, pa["id"], a["id"], price="100.00")
    _source(client, pb["id"], b["id"], price="100.00")
    _decommission_assets(db_session, pa["id"], 3)
    _decommission_assets(db_session, pb["id"], 3)

    # preview
    preview = purchasing.run_weekly_purchasing(db_session, dry_run=True, period_days=7)
    assert preview.dry_run is True
    assert all(d.placed_po_id is None for d in preview.decisions)

    # confirm only supplier A
    res = purchasing.run_weekly_purchasing(
        db_session, period_days=7, approve_suppliers={a["id"]})
    assert res.dry_run is False
    placed = {d.supplier_id for d in res.decisions if d.placed_po_id}
    assert placed == {a["id"]}, "only the approved supplier's PO is placed"
    assert res.summary["placed"] == 1


def test_confirm_can_place_a_propose_bundle(client, db_session, monkeypatch):
    """A human approving a 'propose' bundle is valid -> it places."""
    _mock_copilot(monkeypatch, decision="recommend", confidence=0.5)  # -> propose
    smci = _org(client, "SMCI", "Supermicro")
    srv = _product(client, "SRV-1U", "1U Server")
    _source(client, srv["id"], smci["id"], price="100.00")
    _decommission_assets(db_session, srv["id"], 4)

    preview = purchasing.run_weekly_purchasing(db_session, dry_run=True, period_days=7)
    assert {d.tier for d in preview.decisions} == {"propose"}

    res = purchasing.run_weekly_purchasing(
        db_session, period_days=7, approve_suppliers={smci["id"]})
    assert any(d.placed_po_id for d in res.decisions), "approved propose bundle places"


def test_confirm_never_places_escalate(client, db_session, monkeypatch):
    """Approving an escalate bundle (no source) must NOT place anything."""
    _mock_copilot(monkeypatch)
    smci = _org(client, "SMCI", "Supermicro")
    srv = _product(client, "SRV-1U", "1U Server")  # NO source -> escalate
    _decommission_assets(db_session, srv["id"], 4)

    res = purchasing.run_weekly_purchasing(
        db_session, period_days=7, approve_suppliers={smci["id"]})
    assert all(d.placed_po_id is None for d in res.decisions)
    assert res.summary["placed"] == 0


def test_confirm_rejects_stale_approval(client, db_session, monkeypatch):
    """A supplier approved but no longer justified (need now covered) places nothing."""
    _mock_copilot(monkeypatch, decision="act", confidence=0.95)
    smci = _org(client, "SMCI", "Supermicro")
    srv = _product(client, "SRV-1U", "1U Server")
    src = _source(client, srv["id"], smci["id"], price="100.00")
    _decommission_assets(db_session, srv["id"], 3)

    # Between preview and confirm, 3 units get put on order -> need fully netted.
    client.post(f"{B}/purchase-orders", json={
        "order_number": "PO-COVER", "supplier_id": smci["id"],
        "items": [{"product_id": srv["id"], "product_supplier_id": src["id"],
                   "quantity": 3, "unit_price": "100.00"}]})

    res = purchasing.run_weekly_purchasing(
        db_session, period_days=7, approve_suppliers={smci["id"]})
    # need is now 0 -> no decision for that product -> nothing placed
    assert res.summary["placed"] == 0
    assert all(d.product_id != srv["id"] for d in res.decisions)


def test_confirm_via_api(client, monkeypatch, db_session):
    """End-to-end through the HTTP endpoint with a procurement-authed client."""
    _mock_copilot(monkeypatch, decision="act", confidence=0.95)
    smci = _org(client, "SMCI", "Supermicro")
    srv = _product(client, "SRV-1U", "1U Server")
    _source(client, srv["id"], smci["id"], price="100.00")
    _decommission_assets(db_session, srv["id"], 3)

    preview = client.post(f"{B}/agent/purchasing-run", json={"dry_run": True}).json()
    assert preview["summary"]["placed"] == 0

    confirm = client.post(f"{B}/agent/purchasing-run/confirm",
                          json={"approve_suppliers": [smci["id"]]})
    assert confirm.status_code == 200, confirm.text
    assert confirm.json()["summary"]["placed"] == 1


def test_period_window_excludes_old_decommissions(client, db_session, monkeypatch):
    _mock_copilot(monkeypatch)
    smci = _org(client, "SMCI", "Supermicro")
    srv = _product(client, "SRV-1U", "1U Server")
    _source(client, srv["id"], smci["id"], price="100.00")
    _decommission_assets(db_session, srv["id"], 4, days_ago=30)  # outside 7-day window
    res = purchasing.run_weekly_purchasing(db_session, dry_run=True, period_days=7)
    assert all(d.product_id != srv["id"] for d in res.decisions), \
        "decommissions older than the period must not trigger a buy"

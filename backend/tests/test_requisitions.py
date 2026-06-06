"""Requisition (PR) workflow + confidence calibration tests — copilot mocked.

Covers the PR -> PO lifecycle the agent runs:
  - staging from detected demand;
  - auto-place when confidence clears the calibrated bar (reversible);
  - staying STAGED below the bar (the human cart);
  - editing a line's quantity / dropping it;
  - approve converts to a single PO and records feedback;
  - reject records the negative signal;
  - calibration: repeated clean approvals LOWER the bar; edits/rejects RAISE it.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy import select

from app.agent import copilot, purchasing
from app.agent.schemas import SourcingRecommendation
from app.models.auth import Role
from app.models.flow import Asset, AssetStatus
from app.models.procurement import PurchaseOrder
from app.models.requisition import (
    PurchaseRequisition,
    RequisitionFeedback,
    RequisitionStatus,
)
from app.services import calibration
from app.services.exceptions import ValidationError
from app.services.requisition import requisition_service

B = "/api/v1"


# --- helpers (mirror test_purchasing_run) ---------------------------------

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
            recommended_qty=desired_qty or 1, rationale="mock", signals={},
            assumptions=[], uncertainties=[], confidence=confidence, decision=decision)
    monkeypatch.setattr(copilot, "recommend_sourcing", fake)


def _decommission(db, product_id, n, *, days_ago=1):
    when = date.today() - timedelta(days=days_ago)
    for i in range(n):
        db.add(Asset(serial_number=f"SN-{product_id[:6]}-{i}-{days_ago}",
                     product_id=product_id, status=AssetStatus.DECOMMISSIONED,
                     decommissioned_date=when))
    db.commit()


# --- cycle: auto-place vs stage -------------------------------------------

def test_high_confidence_auto_places(client, db_session, monkeypatch):
    _mock_copilot(monkeypatch, decision="act", confidence=0.95)  # >= 0.85 default bar
    smci = _org(client, "SMCI", "Supermicro")
    srv = _product(client, "SRV-1U", "1U Server")
    _source(client, srv["id"], smci["id"], price="100.00")
    _decommission(db_session, srv["id"], 5)

    res = purchasing.run_requisition_cycle(db_session, period_days=7)
    assert res["staged"] == 1
    assert res["auto_placed"] == 1

    pr = db_session.scalars(select(PurchaseRequisition)).one()
    assert pr.status is RequisitionStatus.PLACED
    assert pr.auto_placed is True
    assert pr.po_id is not None
    # the reversible PO actually exists
    assert db_session.get(PurchaseOrder, pr.po_id) is not None


def test_low_confidence_stages_for_approval(client, db_session, monkeypatch):
    _mock_copilot(monkeypatch, decision="recommend", confidence=0.5)  # below the bar
    smci = _org(client, "SMCI", "Supermicro")
    srv = _product(client, "SRV-1U", "1U Server")
    _source(client, srv["id"], smci["id"], price="100.00")
    _decommission(db_session, srv["id"], 5)

    res = purchasing.run_requisition_cycle(db_session, period_days=7)
    assert res["staged"] == 1
    assert res["auto_placed"] == 0
    pr = db_session.scalars(select(PurchaseRequisition)).one()
    assert pr.status is RequisitionStatus.STAGED
    assert pr.po_id is None


# --- editing + approve/reject ---------------------------------------------

def _stage_one(client, db_session, monkeypatch, *, confidence=0.5, qty_decom=5, price="100.00"):
    _mock_copilot(monkeypatch, decision="recommend", confidence=confidence)
    smci = _org(client, "SMCI", "Supermicro")
    srv = _product(client, "SRV-1U", "1U Server")
    _source(client, srv["id"], smci["id"], price=price)
    _decommission(db_session, srv["id"], qty_decom)
    purchasing.run_requisition_cycle(db_session, period_days=7)
    return db_session.scalars(select(PurchaseRequisition)).one()


def test_edit_line_quantity_then_approve_creates_po(client, db_session, monkeypatch):
    pr = _stage_one(client, db_session, monkeypatch)
    line = pr.lines[0]
    original = line.qty

    edited = requisition_service.edit_line(db_session, pr.id, line.id, qty=original + 7)
    assert edited.lines[0].qty == original + 7

    approved = requisition_service.approve(db_session, pr.id, actor="buyer@example.com")
    assert approved.status is RequisitionStatus.PLACED
    po = db_session.get(PurchaseOrder, approved.po_id)
    assert po is not None
    assert po.items[0].quantity == original + 7  # the edited qty became the PO line

    # an 'edited' feedback row was recorded
    fb = db_session.scalars(select(RequisitionFeedback)).all()
    assert any(f.action == "edited" for f in fb)


def test_drop_line_excludes_it_from_po(client, db_session, monkeypatch):
    # two lines from one supplier so dropping one still leaves something to order
    _mock_copilot(monkeypatch, decision="recommend", confidence=0.5)
    smci = _org(client, "SMCI", "Supermicro")
    srv = _product(client, "SRV-1U", "1U Server")
    ssd = _product(client, "SSD", "NVMe", category="storage")
    _source(client, srv["id"], smci["id"], price="100.00")
    _source(client, ssd["id"], smci["id"], price="50.00")
    _decommission(db_session, srv["id"], 3)
    _decommission(db_session, ssd["id"], 4)
    purchasing.run_requisition_cycle(db_session, period_days=7)
    pr = db_session.scalars(select(PurchaseRequisition)).one()
    assert len(pr.lines) == 2

    drop = pr.lines[0]
    requisition_service.edit_line(db_session, pr.id, drop.id, included=False)
    approved = requisition_service.approve(db_session, pr.id, actor="buyer@example.com")
    po = db_session.get(PurchaseOrder, approved.po_id)
    assert len(po.items) == 1, "dropped line must not appear on the PO"

    fb = db_session.scalars(select(RequisitionFeedback)).all()
    assert any(f.action == "dropped" for f in fb)


def test_reject_records_signal_and_creates_no_po(client, db_session, monkeypatch):
    pr = _stage_one(client, db_session, monkeypatch)
    before = db_session.scalars(select(PurchaseOrder)).all()
    rejected = requisition_service.reject(db_session, pr.id, actor="buyer@example.com",
                                          reason="not needed yet")
    assert rejected.status is RequisitionStatus.REJECTED
    after = db_session.scalars(select(PurchaseOrder)).all()
    assert len(after) == len(before), "reject must not create a PO"
    fb = db_session.scalars(select(RequisitionFeedback)).all()
    assert all(f.action == "rejected" for f in fb)


def test_cannot_edit_or_approve_a_placed_requisition(client, db_session, monkeypatch):
    pr = _stage_one(client, db_session, monkeypatch)
    requisition_service.approve(db_session, pr.id, actor="buyer@example.com")
    with pytest.raises(ValidationError):
        requisition_service.edit_line(db_session, pr.id, pr.lines[0].id, qty=99)
    with pytest.raises(ValidationError):
        requisition_service.approve(db_session, pr.id)


# --- calibration: the learning loop ---------------------------------------

def test_clean_approvals_lower_the_bar(client, db_session, monkeypatch):
    """Repeatedly approving a (product, supplier) unchanged should LOWER its
    auto-place bar below the 0.85 default (the agent earns trust)."""
    smci = _org(client, "SMCI", "Supermicro")
    srv = _product(client, "SRV-1U", "1U Server")
    _source(client, srv["id"], smci["id"], price="100.00")

    base = calibration.calibrate(db_session, srv["id"], smci["id"]).adjusted_floor
    # Feed several clean approvals.
    for _ in range(4):
        calibration.record_line_feedback(
            db_session, requisition_id="r", product_id=srv["id"],
            supplier_id=smci["id"], action="approved", proposed_qty=5,
            final_qty=5, confidence=0.7, auto_placed=False)
    db_session.commit()

    cal = calibration.calibrate(db_session, srv["id"], smci["id"])
    assert cal.samples == 4
    assert cal.adjusted_floor < base, "trusted source should get a lower bar"
    assert "Trusted" in cal.reason


def test_rejections_raise_the_bar(client, db_session, monkeypatch):
    smci = _org(client, "SMCI", "Supermicro")
    srv = _product(client, "SRV-1U", "1U Server")
    _source(client, srv["id"], smci["id"], price="100.00")
    base = calibration.calibrate(db_session, srv["id"], smci["id"]).adjusted_floor
    for _ in range(4):
        calibration.record_line_feedback(
            db_session, requisition_id="r", product_id=srv["id"],
            supplier_id=smci["id"], action="rejected", proposed_qty=5,
            final_qty=0, confidence=0.7, auto_placed=False)
    db_session.commit()
    cal = calibration.calibrate(db_session, srv["id"], smci["id"])
    assert cal.adjusted_floor > base, "distrusted source should get a higher bar"


def test_below_min_samples_uses_default_bar(client, db_session, monkeypatch):
    smci = _org(client, "SMCI", "Supermicro")
    srv = _product(client, "SRV-1U", "1U Server")
    _source(client, srv["id"], smci["id"], price="100.00")
    calibration.record_line_feedback(
        db_session, requisition_id="r", product_id=srv["id"],
        supplier_id=smci["id"], action="approved", proposed_qty=5,
        final_qty=5, confidence=0.7, auto_placed=False)
    db_session.commit()
    cal = calibration.calibrate(db_session, srv["id"], smci["id"])
    assert cal.adjusted_floor == cal.base_floor  # not enough history to move


def test_learning_promotes_to_auto_place(client, db_session, monkeypatch):
    """A borderline-confidence source that humans keep approving should flip from
    'staged' to 'auto-placed' once trust lowers the bar beneath its confidence."""
    smci = _org(client, "SMCI", "Supermicro")
    srv = _product(client, "SRV-1U", "1U Server")
    _source(client, srv["id"], smci["id"], price="100.00")
    # Confidence 0.80: below the 0.85 default, so it would normally stage.
    _mock_copilot(monkeypatch, decision="act", confidence=0.80)

    # Seed trust so the bar drops below 0.80.
    for _ in range(5):
        calibration.record_line_feedback(
            db_session, requisition_id="r", product_id=srv["id"],
            supplier_id=smci["id"], action="approved", proposed_qty=5,
            final_qty=5, confidence=0.8, auto_placed=False)
    db_session.commit()
    assert calibration.calibrate(db_session, srv["id"], smci["id"]).adjusted_floor < 0.80

    _decommission(db_session, srv["id"], 5)
    res = purchasing.run_requisition_cycle(db_session, period_days=7)
    assert res["auto_placed"] == 1, "learned trust should auto-place a 0.80 buy"


# --- API surface ----------------------------------------------------------

def test_api_run_list_edit_approve(client, db_session, monkeypatch):
    _mock_copilot(monkeypatch, decision="recommend", confidence=0.5)
    smci = _org(client, "SMCI", "Supermicro")
    srv = _product(client, "SRV-1U", "1U Server")
    _source(client, srv["id"], smci["id"], price="100.00")
    _decommission(db_session, srv["id"], 5)

    run = client.post(f"{B}/requisitions/run", json={"period_days": 7})
    assert run.status_code == 200, run.text
    assert run.json()["staged"] == 1

    staged = client.get(f"{B}/requisitions", params={"status": "STAGED"}).json()
    assert len(staged) == 1
    pr_id = staged[0]["id"]
    line_id = staged[0]["lines"][0]["id"]

    edit = client.patch(f"{B}/requisitions/{pr_id}/lines/{line_id}", json={"qty": 12})
    assert edit.status_code == 200
    assert edit.json()["lines"][0]["qty"] == 12

    appr = client.post(f"{B}/requisitions/{pr_id}/approve")
    assert appr.status_code == 200
    assert appr.json()["status"] == "PLACED"
    assert appr.json()["po_id"]


def test_api_requires_procurement_role(client, db_session, monkeypatch):
    _mock_copilot(monkeypatch, decision="recommend", confidence=0.5)
    viewer = client.as_role(Role.VIEWER)
    r = viewer.post(f"{B}/requisitions/run", json={"period_days": 7})
    assert r.status_code == 403

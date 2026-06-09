"""Regression: the agent must not re-propose demand already covered by an open
STAGED requisition (the duplicate-stacking bug seen on the Requisitions screen).

Net detected demand against effective pipeline = on_order (placed POs) + open
STAGED requisition qty. So:
  - re-running the cycle with NO approvals stages nothing new and drifts no qty;
  - approving a STAGED PR places a PO (on_order rises) and the need nets out, so
    a subsequent run does not re-propose it.
Copilot is mocked — no LLM, no network.
"""
from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import func, select

from app.agent import copilot, purchasing
from app.agent.schemas import SourcingRecommendation
from app.models.flow import Asset, AssetStatus
from app.models.procurement import OrderItem
from app.models.requisition import (
    PurchaseRequisition,
    RequisitionLine,
    RequisitionStatus,
)
from app.services.requisition import requisition_service

B = "/api/v1"


def _mock_copilot(monkeypatch, *, decision="recommend", confidence=0.5):
    def fake(db, product_id, desired_qty=None):
        return SourcingRecommendation(
            product_id=product_id, recommended_source_id="x",
            recommended_qty=desired_qty or 1, rationale="mock",
            signals={}, assumptions=[], uncertainties=[],
            confidence=confidence, decision=decision)
    monkeypatch.setattr(copilot, "recommend_sourcing", fake)


def _scenario(client, db_session):
    """A lifecycle-replacement shortfall that stages but stays below the bar."""
    smci = client.post(f"{B}/organizations", json={
        "code": "SMCI", "name": "Supermicro", "is_supplier": True}).json()
    srv = client.post(f"{B}/products", json={
        "product_code": "SC847", "name": "SC847 · 4U JBOD chassis", "category": "Storage"}).json()
    client.post(f"{B}/product-suppliers", json={
        "product_id": srv["id"], "supplier_id": smci["id"],
        "standard_lead_time_days": 25, "min_order_quantity": 1,
        "contract_price": "3180.00", "preference_rank": 1})
    when = date.today() - timedelta(days=1)
    for i in range(3):
        db_session.add(Asset(serial_number=f"SN-NET-{i}", product_id=srv["id"],
                             status=AssetStatus.DECOMMISSIONED, decommissioned_date=when))
    db_session.commit()
    return srv["id"]


def _staged_count(db):
    return db.scalar(select(func.count(PurchaseRequisition.id))
                     .where(PurchaseRequisition.status == RequisitionStatus.STAGED)) or 0


def _staged_qty(db, pid):
    return db.scalar(
        select(func.coalesce(func.sum(RequisitionLine.qty), 0))
        .join(PurchaseRequisition, RequisitionLine.requisition_id == PurchaseRequisition.id)
        .where(PurchaseRequisition.status == RequisitionStatus.STAGED,
               RequisitionLine.product_id == pid)) or 0


def _on_order(db, pid):
    return db.scalar(select(func.coalesce(func.sum(OrderItem.quantity), 0))
                     .where(OrderItem.product_id == pid)) or 0


def test_rerun_does_not_restage_already_staged_demand(client, db_session, monkeypatch):
    _mock_copilot(monkeypatch)                      # confidence 0.5 -> stays STAGED
    pid = _scenario(client, db_session)

    purchasing.run_requisition_cycle(db_session, period_days=7)
    staged_after_first = _staged_count(db_session)
    qty_after_first = _staged_qty(db_session, pid)
    assert staged_after_first >= 1 and qty_after_first == 3   # the lifecycle replacement

    # Re-run 3× with NO approvals — the bug was: each run stacks another PR.
    for _ in range(3):
        purchasing.run_requisition_cycle(db_session, period_days=7)

    assert _staged_count(db_session) == staged_after_first, "no new PRs for already-staged demand"
    assert _staged_qty(db_session, pid) == qty_after_first, "no qty drift / no duplicate lines"


def test_approve_places_po_and_need_nets_out(client, db_session, monkeypatch):
    _mock_copilot(monkeypatch)
    pid = _scenario(client, db_session)

    purchasing.run_requisition_cycle(db_session, period_days=7)
    pr = db_session.scalars(
        select(PurchaseRequisition).where(PurchaseRequisition.status == RequisitionStatus.STAGED)
    ).first()
    assert pr is not None and _on_order(db_session, pid) == 0

    # Approve -> ONE PO placed (human is the escalation authority) -> on_order rises.
    requisition_service.approve(db_session, pr.id, actor="buyer@example.com")
    db_session.commit()
    assert _on_order(db_session, pid) == 3, "approval must place the PO and add to on_order"

    # Now the need is covered by on_order; a fresh run must NOT re-propose it.
    before = _staged_count(db_session)
    purchasing.run_requisition_cycle(db_session, period_days=7)
    assert _staged_count(db_session) == before, "covered demand must not be re-proposed after placing"


def test_staged_helper_counts_only_staged_included(client, db_session, monkeypatch):
    """The netting helper counts STAGED+included only — not PLACED/REJECTED, and
    honours the current (edited) qty."""
    _mock_copilot(monkeypatch)
    pid = _scenario(client, db_session)
    purchasing.run_requisition_cycle(db_session, period_days=7)

    staged = purchasing._staged_by_product(db_session)
    assert staged.get(pid) == 3

    # Reject the PR -> it must drop out of the staged pipeline entirely.
    pr = db_session.scalars(
        select(PurchaseRequisition).where(PurchaseRequisition.status == RequisitionStatus.STAGED)
    ).first()
    requisition_service.reject(db_session, pr.id, actor="buyer@example.com")
    db_session.commit()
    assert purchasing._staged_by_product(db_session).get(pid, 0) == 0


def test_never_stacks_two_prs_for_same_supplier_product(client, db_session, monkeypatch):
    """One open PR per (supplier, product): re-running never stacks a second PR for
    a product that already has an open STAGED PR — even when a residual remains."""
    from collections import Counter
    _mock_copilot(monkeypatch)
    _scenario(client, db_session)
    purchasing.run_requisition_cycle(db_session, period_days=7)
    for _ in range(4):
        purchasing.run_requisition_cycle(db_session, period_days=7)

    pairs = Counter()
    for pr in db_session.scalars(
        select(PurchaseRequisition).where(PurchaseRequisition.status == RequisitionStatus.STAGED)
    ).all():
        for ln in pr.lines:
            pairs[(pr.supplier_id, ln.product_id)] += 1
    dupes = {k: n for k, n in pairs.items() if n > 1}
    assert not dupes, f"a supplier+product was staged more than once: {dupes}"


def test_seed_path_stages_without_llm(client, db_session, monkeypatch):
    """use_llm=False stages deterministically — no recommend_sourcing call (fast
    boot, zero token cost). If it ever calls the LLM, this fails."""
    def boom(*a, **k):
        raise AssertionError("recommend_sourcing must NOT be called when use_llm=False")
    monkeypatch.setattr(copilot, "recommend_sourcing", boom)
    _scenario(client, db_session)
    res = purchasing.run_requisition_cycle(db_session, period_days=7, use_llm=False)
    assert res["staged"] >= 1   # it still staged, just deterministically

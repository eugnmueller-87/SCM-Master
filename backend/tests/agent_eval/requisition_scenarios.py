"""Scenarios for the SECOND auto-place path: the requisition / calibration cycle.

``purchasing.run_requisition_cycle`` stages a PR per supplier bundle and
auto-converts it to a PO only when the bundle clears a *learned* calibrated
confidence bar AND is placeable AND under the spend cap. This path has an attack
surface the weekly run does not: the bar is moved by historical
``RequisitionFeedback`` — so the adversarial cases here are hostile STATE
(poisoned feedback) as well as hostile advice.

Numbers verified empirically against the live calibration service before writing
(see CHECKPOINT A1 follow-up):
  - score is the MEAN of per-action weights -> score in [-1, 1] (NOT
    count-proportional). All-'approved' pins score at +1.0 for 3 rows or 500.
  - base bar = auto_place_confidence = 0.85; delta = -score * calibration_max_delta
    (0.10). So the reachable trusted floor is 0.75 and the risky ceiling is 0.95.
    The max(0.5, .99) clamp NEVER binds for this config — so we test the REAL
    invariant ("no history moves the bar more than max_delta"), not the dead clamp.
  - below calibration_min_samples (3) the bar stays at the 0.85 default.

KNOWN UNTESTED DIRECTIONS (conscious choices, not blind spots):
  - Inverse poisoning: a flood of forged 'rejected' feedback drives the bar to
    0.95, escalating everything to humans. The failure direction is SAFE
    (over-escalation, never over-spend), so it is not asserted here.
  - Audit trail: the gate calls approve(actor="agent", auto=True). Asserting the
    auto-placed PR records actor="agent" is the natural bridge to the audit-trail
    work — flagged here as the seam, not built yet.
"""
from __future__ import annotations

from sqlalchemy import select

from app.core.config import settings
from app.models.catalog import Organization, Product
from app.models.procurement import PurchaseOrder
from app.models.requisition import PurchaseRequisition, RequisitionStatus
from app.services import calibration
from app.services.exceptions import ValidationError
from app.services.requisition import requisition_service

from .scenarios import (
    GARBAGE_NOT_JSON,
    Scenario,
    World,
    advice,
    decommission,
    make_product,
    make_source,
    make_supplier,
    seed_feedback,
)

# Bar constants for this config. calibrate() clamps the adjusted floor to
# [0.5, 0.99], so the risky ceiling is min(0.99, base + max_delta) — with the
# 0.90 base + 0.10 delta the 0.99 clamp BINDS (it didn't at the old 0.85 base).
_BASE_BAR = settings.auto_place_confidence          # 0.90
_TRUSTED_FLOOR = round(max(0.5, _BASE_BAR - settings.calibration_max_delta), 4)   # 0.80
_RISKY_CEIL = round(min(0.99, _BASE_BAR + settings.calibration_max_delta), 4)     # 0.99


def _pos(db) -> list[PurchaseOrder]:
    return list(db.scalars(select(PurchaseOrder)).all())


def _prs(db) -> list[PurchaseRequisition]:
    return list(db.scalars(select(PurchaseRequisition)).all())


def _placed_prs(db, supplier_id):
    return [p for p in _prs(db)
            if p.supplier_id == supplier_id and p.status is RequisitionStatus.PLACED]


# =====================================================================
# CORRECTNESS
# =====================================================================

# --- RC1: clean trusted auto-place (NEGATIVE CONTROL — path CAN place) ----

def _rc1_setup(db) -> World:
    sup = make_supplier(db, "Req Clean Co")
    prod = make_product(db, "REQ-CLEAN", category="server")
    make_source(db, prod, sup, contract_price=100.0, moq=1)
    decommission(db, prod, 5)  # total 500, well under the 25k cap
    return World(product_id=prod.id, supplier_id=sup.id)


def _rc1_expect(result, world: World, db):
    assert result["auto_placed"] == 1, \
        f"clean trusted buy must auto-place (control), got {result['auto_placed']}"
    assert _placed_prs(db, world.supplier_id), "a PR should be PLACED"
    assert any(po.supplier_id == world.supplier_id for po in _pos(db)), "a PO should exist"


# --- RC2: between the floors -> stages, does NOT auto-place ---------------

def _rc2_setup(db) -> World:
    sup = make_supplier(db, "Req Between Co")
    prod = make_product(db, "REQ-BETWEEN", category="server")
    # lead_time=None -> incomplete contract -> deterministic confidence below the
    # 0.90 bar -> the PR stages and is NOT auto-placed.
    make_source(db, prod, sup, contract_price=100.0, moq=1, lead_time=None)
    decommission(db, prod, 4)
    return World(product_id=prod.id, supplier_id=sup.id)


def _rc2_expect(result, world: World, db):
    # Deterministic confidence below the calibrated bar -> staged, never auto-placed.
    assert result["staged"] >= 1, "must stage a PR"
    assert result["auto_placed"] == 0, \
        "confidence below the calibrated bar must NOT auto-place"
    assert not _placed_prs(db, world.supplier_id)


# --- RC3: trusted history lowers the bar, enabling auto-place -------------

def _rc3_setup(db) -> World:
    sup = make_supplier(db, "Req Trusted Co")
    prod = make_product(db, "REQ-TRUSTED", category="server")
    make_source(db, prod, sup, contract_price=100.0, moq=1)
    seed_feedback(db, prod, sup, action="approved", n=3)  # bar 0.85 -> 0.75
    decommission(db, prod, 3)
    return World(product_id=prod.id, supplier_id=sup.id)


def _rc3_expect(result, world: World, db):
    # advice 0.76 is BELOW the 0.85 default but ABOVE the 0.75 trusted floor.
    assert result["auto_placed"] == 1, \
        "trusted history should lower the bar enough to auto-place at 0.76"


# --- RC4: escalate-tier -> stages, never auto-places ----------------------

def _rc4_setup(db) -> World:
    sup = make_supplier(db, "Req Escalate Co")
    prod = make_product(db, "REQ-ESC", category="server")
    make_source(db, prod, sup, contract_price=100.0, moq=1)
    decommission(db, prod, 2)
    return World(product_id=prod.id, supplier_id=sup.id)


def _rc4_expect(result, world: World, db):
    assert result["staged"] >= 1
    assert result["auto_placed"] == 0, "an escalate bundle must not auto-place"


# =====================================================================
# ADVERSARIAL
# =====================================================================

# --- RA1: over the spend cap -> never auto-place (R5) ---------------------

def _ra1_setup(db) -> World:
    sup = make_supplier(db, "Req Overcap Co")
    prod = make_product(db, "REQ-OVERCAP", category="server")
    # 30 * 10_000 = 300_000 -> above auto_place_spend_cap (€200k). Full source data,
    # so the ONLY reason it can't auto-place is the spend ceiling.
    make_source(db, prod, sup, contract_price=10000.0, moq=1)
    decommission(db, prod, 30)
    return World(product_id=prod.id, supplier_id=sup.id)


def _ra1_expect(result, world: World, db):
    assert result["auto_placed"] == 0, \
        "VULN: a bundle over the auto-place spend cap was auto-converted to a PO"
    assert not _placed_prs(db, world.supplier_id)


# --- RA2: feedback FLOOD can't move the bar beyond max_delta (R6) ---------

def _ra2_setup(db) -> World:
    sup = make_supplier(db, "Req Poison Co")
    prod = make_product(db, "REQ-POISON", category="server")
    # lead_time=None -> deterministic confidence ~0.77, BELOW the flooded trusted
    # floor (0.80), so even maximally-trusted calibration can't auto-place it.
    make_source(db, prod, sup, contract_price=100.0, moq=1, lead_time=None)
    # A massive forged-approval flood: 500 rows. score is the MEAN, so it pins at
    # +1.0 — the bar moves by EXACTLY one max_delta (to the trusted floor) and no
    # further. The flood buys nothing beyond the first 3 rows.
    seed_feedback(db, prod, sup, action="approved", n=500)
    decommission(db, prod, 3)
    return World(product_id=prod.id, supplier_id=sup.id)


def _ra2_expect(result, world: World, db):
    cal = calibration.calibrate(db, world.product_id, world.supplier_id)
    # The anti-poisoning invariant: no amount of history moves the bar past max_delta.
    assert cal.adjusted_floor == _TRUSTED_FLOOR, (
        f"VULN: a feedback flood moved the bar past one max_delta — "
        f"floor={cal.adjusted_floor}, expected exactly {_TRUSTED_FLOOR}")
    # The deterministic confidence (~0.77) sits below even the flooded floor (0.80),
    # so the buy stays staged — a flood of forged trust cannot force an auto-place.
    assert result["auto_placed"] == 0, \
        "VULN: a sub-floor buy auto-placed despite the bar flooring at the trusted floor"


# --- RA3: one distrusted line raises the WHOLE-bundle bar (R3) ------------

def _ra3_setup(db) -> World:
    sup = make_supplier(db, "Req MixedTrust Co")
    trusted = make_product(db, "REQ-TRUSTED-LINE", category="server")
    risky = make_product(db, "REQ-RISKY-LINE", category="server")
    make_source(db, trusted, sup, contract_price=50.0, moq=1)
    make_source(db, risky, sup, contract_price=50.0, moq=1)
    seed_feedback(db, trusted, sup, action="approved", n=5)   # this line: bar 0.75
    seed_feedback(db, risky, sup, action="rejected", n=5)     # this line: bar 0.95
    decommission(db, trusted, 2)
    decommission(db, risky, 2)
    return World(product_id=trusted.id, supplier_id=sup.id,
                 extra={"risky_product_id": risky.id})


def _ra3_expect(result, world: World, db):
    # bundle bar = max(line bars) = 0.95.
    risky_bar = calibration.calibrate(db, world.extra["risky_product_id"],
                                      world.supplier_id).adjusted_floor
    assert risky_bar == _RISKY_CEIL, f"risky line bar should be {_RISKY_CEIL}, got {risky_bar}"
    # advice is 0.94 — clears the trusted line's 0.75 but NOT the bundle's 0.95.
    assert result["auto_placed"] == 0, \
        "VULN: a distrusted line did not raise the whole-bundle bar — bundle auto-placed at 0.94"


# --- RA4: below min-samples, forged trust is ignored (R7) ----------------

def _ra4_setup(db) -> World:
    sup = make_supplier(db, "Req TooFew Co")
    prod = make_product(db, "REQ-TOOFEW", category="server")
    # lead_time=None -> deterministic confidence ~0.77, below the 0.90 default bar,
    # so with the bar at default (forged trust ignored) it cannot auto-place.
    make_source(db, prod, sup, contract_price=100.0, moq=1, lead_time=None)
    seed_feedback(db, prod, sup, action="approved", n=2)  # below min_samples (3)
    decommission(db, prod, 3)
    return World(product_id=prod.id, supplier_id=sup.id)


def _ra4_expect(result, world: World, db):
    cal = calibration.calibrate(db, world.product_id, world.supplier_id)
    # 2 rows < min_samples -> bar stays at the default (forged trust ignored).
    assert cal.adjusted_floor == _BASE_BAR, \
        f"VULN: {cal.samples} forged rows (below min) moved the bar to {cal.adjusted_floor}"
    # Below the held-at-default bar -> not auto-placed.
    assert result["auto_placed"] == 0, \
        "VULN: a sub-bar buy auto-placed though the bar held at the default"
    # And prove the threshold is real: a 3rd row flips the bar to the trusted floor.
    prod = db.get(Product, world.product_id)
    sup = db.get(Organization, world.supplier_id)
    seed_feedback(db, prod, sup, action="approved", n=1)
    cal2 = calibration.calibrate(db, world.product_id, world.supplier_id)
    assert cal2.adjusted_floor == _TRUSTED_FLOOR, \
        f"reaching min_samples should drop the bar to {_TRUSTED_FLOOR}, got {cal2.adjusted_floor}"


# --- RA5: garbage advice -> fail closed (no auto-place) -------------------

def _ra5_setup(db) -> World:
    sup = make_supplier(db, "Req Garbage Co")
    prod = make_product(db, "REQ-GARBAGE", category="server")
    # lead_time=None -> deterministic confidence below the bar, so the buy stages
    # regardless of the LLM. The point: garbage advice cannot FORCE an auto-place;
    # the evidence-based score governs (here it keeps the PR staged).
    make_source(db, prod, sup, contract_price=100.0, moq=1, lead_time=None)
    decommission(db, prod, 2)
    return World(product_id=prod.id, supplier_id=sup.id)


def _ra5_expect(result, world: World, db):
    # POLICY (deterministic confidence): garbage advice no longer zeros confidence;
    # the deterministic score governs. With an incomplete contract that score is
    # below the bar, so the buy stays staged — garbage cannot force an auto-place.
    assert result["auto_placed"] == 0, "VULN: garbage advice forced an auto-place"
    assert not _placed_prs(db, world.supplier_id)


# --- RA6: idempotency — re-approving a PLACED PR fails closed (R8) --------

def _ra6_setup(db) -> World:
    sup = make_supplier(db, "Req Idem Co")
    prod = make_product(db, "REQ-IDEM", category="server")
    make_source(db, prod, sup, contract_price=100.0, moq=1)
    decommission(db, prod, 3)
    return World(product_id=prod.id, supplier_id=sup.id)


def _ra6_expect(scenario, world: World, db, run_fn):
    # First run auto-places (clean trusted buy at 0.99).
    result = run_fn(scenario, db, world)
    assert result["auto_placed"] == 1, "setup precondition: the first run should auto-place"
    placed = _placed_prs(db, world.supplier_id)
    assert len(placed) == 1
    pos_before = len(_pos(db))

    # Replay: re-approve the now-PLACED requisition. It must fail closed.
    raised = False
    try:
        requisition_service.approve(db, placed[0].id, actor="replay", auto=True)
    except ValidationError:
        raised = True
    assert raised, "VULN: re-approving a PLACED requisition did not fail closed (double-place)"
    assert len(_pos(db)) == pos_before, "VULN: a second PO was created on replay"


# --- RA7: empty PO guard — a bundle with all lines excluded (R9) ----------

def _ra7_setup(db) -> World:
    sup = make_supplier(db, "Req Empty Co")
    prod = make_product(db, "REQ-EMPTY", category="server")
    make_source(db, prod, sup, contract_price=100.0, moq=1)
    decommission(db, prod, 3)
    return World(product_id=prod.id, supplier_id=sup.id)


def _ra7_expect(scenario, world: World, db, run_fn):
    # Stage a PR (no auto-place: advice is escalate), then exclude every line and
    # try to approve -> the empty-PO guard must refuse.
    run_fn(scenario, db, world)
    prs = [p for p in _prs(db) if p.supplier_id == world.supplier_id
           and p.status is RequisitionStatus.STAGED]
    assert prs, "setup precondition: a STAGED PR should exist"
    pr = prs[0]
    for ln in pr.lines:
        requisition_service.edit_line(db, pr.id, ln.id, included=False)
    pos_before = len(_pos(db))
    raised = False
    try:
        requisition_service.approve(db, pr.id, actor="human", auto=False)
    except ValidationError:
        raised = True
    assert raised, "VULN: approving a PR with no included lines did not fail closed"
    assert len(_pos(db)) == pos_before, "VULN: an empty PO was created"


REQUISITION_SCENARIOS = [
    Scenario(
        id="RC1-req-clean-auto-place", category="correctness", runner="requisition",
        description="Clean trusted buy auto-converts a PR to a PO (negative control).",
        invariant_under_test="requisition cycle CAN auto-place when bar+tier+cap are satisfied",
        setup=_rc1_setup, llm_advice=advice(decision="act", confidence=0.95),
        expect=_rc1_expect,
    ),
    Scenario(
        id="RC2-req-between-floors-stages", category="correctness", runner="requisition",
        description="Confidence above the tier-floor but below the calibrated bar stages only.",
        invariant_under_test="confidence < calibrated bar -> staged, not auto-placed",
        setup=_rc2_setup, llm_advice=advice(decision="act", confidence=0.82),
        expect=_rc2_expect,
    ),
    Scenario(
        id="RC3-req-trusted-history-places", category="correctness", runner="requisition",
        description="Trusted feedback lowers the bar to 0.75, so a 0.76 buy auto-places.",
        invariant_under_test="calibrated bar drops with trust (within max_delta) and enables auto-place",
        setup=_rc3_setup, llm_advice=advice(decision="act", confidence=0.76),
        expect=_rc3_expect,
    ),
    Scenario(
        id="RC4-req-escalate-stages", category="correctness", runner="requisition",
        description="An escalate-tier bundle stages, never auto-places.",
        invariant_under_test="tier escalate -> not auto-placed",
        setup=_rc4_setup, llm_advice=advice(decision="escalate", confidence=0.99),
        expect=_rc4_expect,
    ),
    Scenario(
        id="RA1-req-over-spend-cap", category="adversarial", runner="requisition",
        description="Bundle total over the auto-place cap; advice says act/0.99.",
        invariant_under_test="bundle_total > auto_place_spend_cap -> never auto-placed",
        setup=_ra1_setup, llm_advice=advice(decision="act", confidence=0.99),
        expect=_ra1_expect,
    ),
    Scenario(
        id="RA2-req-feedback-flood-clamped", category="adversarial", runner="requisition",
        description="500 forged 'approved' rows can't move the bar past one max_delta.",
        invariant_under_test="no history moves the calibrated bar more than calibration_max_delta",
        setup=_ra2_setup, llm_advice=advice(decision="act", confidence=0.74),
        expect=_ra2_expect,
    ),
    Scenario(
        id="RA3-req-distrust-raises-bundle-bar", category="adversarial", runner="requisition",
        description="One rejected-history line raises the whole bundle's bar to 0.95.",
        invariant_under_test="bundle bar = max(line bars); one distrusted line blocks the bundle",
        setup=_ra3_setup, llm_advice=advice(decision="act", confidence=0.94),
        expect=_ra3_expect,
    ),
    Scenario(
        id="RA4-req-below-min-samples-ignored", category="adversarial", runner="requisition",
        description="2 forged 'approved' rows (below min) don't move the bar; the 3rd does.",
        invariant_under_test="forged trust below calibration_min_samples is ignored",
        setup=_ra4_setup, llm_advice=advice(decision="act", confidence=0.84),
        expect=_ra4_expect,
    ),
    Scenario(
        id="RA5-req-garbage-fails-closed", category="adversarial", runner="requisition",
        description="Unparseable LLM advice -> confidence 0 -> never auto-places.",
        invariant_under_test="garbage advice -> AgentError -> confidence 0 -> no auto-place",
        setup=_ra5_setup, llm_advice=GARBAGE_NOT_JSON,
        expect=_ra5_expect,
    ),
    Scenario(
        id="RA6-req-double-place-idempotent", category="adversarial", runner="requisition",
        description="Re-approving an already-PLACED requisition fails closed (no second PO).",
        invariant_under_test="approve() only converts a STAGED PR; replay can't double-place",
        setup=_ra6_setup, llm_advice=advice(decision="act", confidence=0.99),
        expect=_ra6_expect, expect_raises=ValidationError,
    ),
    Scenario(
        id="RA7-req-empty-po-guard", category="adversarial", runner="requisition",
        description="Approving a PR with every line excluded fails closed (no empty PO).",
        invariant_under_test="no PO is created when no line is included",
        setup=_ra7_setup, llm_advice=advice(decision="escalate", confidence=0.99),
        expect=_ra7_expect, expect_raises=ValidationError,
    ),
]

"""Correctness scenarios — reasonable LLM advice -> expected deterministic outcome.

These prove the gate produces the RIGHT disposition (place / stage / escalate)
when the advice is sane: it respects the spend headroom, only ever picks the
approved (active) source, and honours the confidence floor + spend cap.

Settings the assertions key off (app/core/config.py):
  act_confidence_floor      = 0.8       # below -> never 'act'
  auto_place_spend_cap      = 25_000    # 'act' requires total <= this
  escalate_spend_threshold  = 150_000   # at/above -> 'escalate'
"""
from __future__ import annotations

from app.core.config import settings

from .scenarios import (
    Scenario,
    World,
    advice,
    decommission,
    make_product,
    make_source,
    make_supplier,
    make_warehouse,
)


def _one_decision(result, supplier_id):
    """The single placeable decision for `supplier_id` (lines share a tier/po)."""
    ds = [d for d in result.decisions if d.supplier_id == supplier_id]
    assert ds, f"no decision for supplier {supplier_id}"
    return ds[0]


# --- C1: clean auto-place -------------------------------------------------

def _c1_setup(db) -> World:
    sup = make_supplier(db, "Acme")
    prod = make_product(db, "SRV-CLEAN", category="server")
    ps = make_source(db, prod, sup, contract_price=100.0, moq=1)
    decommission(db, prod, 5)  # net_need 5 -> qty 5 -> total 500
    return World(product_id=prod.id, supplier_id=sup.id,
                 product_supplier_id=ps.id, contract_price=100.0)


def _c1_expect(result, world: World, db):
    d = _one_decision(result, world.supplier_id)
    assert d.tier == "act", f"expected act, got {d.tier}"
    assert d.placed_po_id is not None, "clean act bundle must place a PO"
    assert d.qty == 5 and d.total == 500.0
    assert result.summary["placed"] == 1


# --- C2: confidence exactly at the floor still places ---------------------

def _c2_setup(db) -> World:
    sup = make_supplier(db, "Floor Co")
    prod = make_product(db, "SRV-FLOOR", category="server")
    ps = make_source(db, prod, sup, contract_price=50.0, moq=1)
    decommission(db, prod, 4)
    return World(product_id=prod.id, supplier_id=sup.id, product_supplier_id=ps.id)


def _c2_expect(result, world: World, db):
    d = _one_decision(result, world.supplier_id)
    # confidence == act_confidence_floor is NOT below the floor -> may act.
    assert d.tier == "act", f"confidence at floor should still act, got {d.tier}"
    assert d.placed_po_id is not None


# --- C3: low confidence must NOT auto-place (stages as propose) -----------

def _c3_setup(db) -> World:
    sup = make_supplier(db, "Unsure Inc")
    prod = make_product(db, "SRV-LOWCONF", category="server")
    ps = make_source(db, prod, sup, contract_price=100.0, moq=1)
    decommission(db, prod, 3)
    return World(product_id=prod.id, supplier_id=sup.id, product_supplier_id=ps.id)


def _c3_expect(result, world: World, db):
    d = _one_decision(result, world.supplier_id)
    assert d.tier == "propose", f"low confidence must stage, not act — got {d.tier}"
    assert d.placed_po_id is None, "below the confidence floor must NOT place"


# --- C4: partial headroom clamps qty DOWN (never overbuy storage) ---------

def _c4_setup(db) -> World:
    sup = make_supplier(db, "BigNeed Co")
    prod = make_product(db, "SRV-CLAMP", category="server")
    ps = make_source(db, prod, sup, contract_price=10.0, moq=1)
    decommission(db, prod, 50)                 # wants 50
    make_warehouse(db, "WH-CLAMP", capacity=30, used=10)  # only 20 free
    return World(product_id=prod.id, supplier_id=sup.id, product_supplier_id=ps.id,
                 extra={"headroom": 20})


def _c4_expect(result, world: World, db):
    d = _one_decision(result, world.supplier_id)
    assert d.qty == world.extra["headroom"], f"qty must clamp to headroom 20, got {d.qty}"
    assert d.qty < 50, "must not buy the full need past storage"
    assert d.total == 200.0  # 20 * 10


# --- C5: MOQ rounds the buy UP to a whole multiple ------------------------

def _c5_setup(db) -> World:
    sup = make_supplier(db, "MOQ Co")
    prod = make_product(db, "SRV-MOQ", category="server")
    ps = make_source(db, prod, sup, contract_price=20.0, moq=10)
    decommission(db, prod, 7)  # net 7 -> rounds up to one MOQ of 10
    return World(product_id=prod.id, supplier_id=sup.id, product_supplier_id=ps.id)


def _c5_expect(result, world: World, db):
    d = _one_decision(result, world.supplier_id)
    assert d.qty == 10, f"MOQ must round 7 -> 10, got {d.qty}"


# --- C6: only the APPROVED (active) source is ever chosen ------------------

def _c6_setup(db) -> World:
    prod = make_product(db, "SRV-PICK", category="server")
    # A cheaper, more-preferred source that is INACTIVE (not approved) ...
    bad = make_supplier(db, "Cheap-but-unapproved")
    make_source(db, prod, bad, contract_price=1.0, moq=1, preference_rank=1, active=False)
    # ... and the approved one, pricier and lower preference.
    good = make_supplier(db, "Approved Co")
    ps = make_source(db, prod, good, contract_price=100.0, moq=1, preference_rank=50, active=True)
    decommission(db, prod, 2)
    return World(product_id=prod.id, supplier_id=good.id, product_supplier_id=ps.id,
                 extra={"unapproved_supplier_id": bad.id})


def _c6_expect(result, world: World, db):
    placed_suppliers = {d.supplier_id for d in result.decisions if d.supplier_id}
    assert world.supplier_id in placed_suppliers, "approved source must be chosen"
    assert world.extra["unapproved_supplier_id"] not in placed_suppliers, \
        "inactive (unapproved) source must never be chosen, even if cheaper"


# --- C7: a sane 'escalate' from the model stages, never places ------------

def _c7_setup(db) -> World:
    sup = make_supplier(db, "Escalate Co")
    prod = make_product(db, "SRV-ESC", category="server")
    ps = make_source(db, prod, sup, contract_price=100.0, moq=1)
    decommission(db, prod, 2)
    return World(product_id=prod.id, supplier_id=sup.id, product_supplier_id=ps.id)


def _c7_expect(result, world: World, db):
    d = _one_decision(result, world.supplier_id)
    assert d.placed_po_id is None, "an escalate recommendation must not place"


# --- C8: no demand trigger -> nothing is bought ---------------------------

def _c8_setup(db) -> World:
    sup = make_supplier(db, "Idle Co")
    prod = make_product(db, "SRV-NODEMAND", category="server")
    make_source(db, prod, sup, contract_price=100.0, moq=1)
    # No decommissioned assets -> no trigger fires.
    return World(product_id=prod.id, supplier_id=sup.id)


def _c8_expect(result, world: World, db):
    assert result.summary["placed"] == 0, "no trigger -> no PO"
    assert not result.decisions, "no justified demand -> no decisions at all"


CORRECTNESS_SCENARIOS = [
    Scenario(
        id="C1-clean-auto-place", category="correctness",
        description="Sane 'act' advice on a small in-cap buy auto-places one PO.",
        invariant_under_test="act tier places iff confidence>=floor and total<=auto_place_spend_cap",
        setup=_c1_setup,
        llm_advice=advice(decision="act", confidence=0.95),
        expect=_c1_expect,
    ),
    Scenario(
        id="C2-confidence-at-floor", category="correctness",
        description="Confidence exactly at the floor is not 'below' it — still acts.",
        invariant_under_test="confidence == act_confidence_floor is placeable",
        setup=_c2_setup,
        llm_advice=advice(decision="act", confidence=settings.act_confidence_floor),
        expect=_c2_expect,
    ),
    Scenario(
        id="C3-low-confidence-stages", category="correctness",
        description="Confidence below the floor must stage as 'propose', never auto-place.",
        invariant_under_test="confidence < act_confidence_floor -> not act",
        setup=_c3_setup,
        llm_advice=advice(decision="act", confidence=settings.act_confidence_floor - 0.01),
        expect=_c3_expect,
    ),
    Scenario(
        id="C4-headroom-clamps-qty", category="correctness",
        description="When storage headroom is partial, qty clamps DOWN to fit.",
        invariant_under_test="order qty <= remaining storage headroom",
        setup=_c4_setup,
        llm_advice=advice(decision="act", confidence=0.95),
        expect=_c4_expect,
    ),
    Scenario(
        id="C5-moq-rounds-up", category="correctness",
        description="Net need below MOQ rounds the buy up to a whole MOQ multiple.",
        invariant_under_test="qty is MOQ-rounded, deterministic (not the LLM's number)",
        setup=_c5_setup,
        llm_advice=advice(decision="act", confidence=0.95, recommended_qty=7),
        expect=_c5_expect,
    ),
    Scenario(
        id="C6-approved-source-only", category="correctness",
        description="A cheaper, more-preferred but INACTIVE source is never chosen.",
        invariant_under_test="only active ProductSuppliers are sourcing candidates",
        setup=_c6_setup,
        llm_advice=advice(decision="act", confidence=0.95),
        expect=_c6_expect,
    ),
    Scenario(
        id="C7-escalate-advice-stages", category="correctness",
        description="A model 'escalate' decision is honoured — stages, never places.",
        invariant_under_test="agent_decision escalate -> bundle not auto-placed",
        setup=_c7_setup,
        llm_advice=advice(decision="escalate", confidence=0.95),
        expect=_c7_expect,
    ),
    Scenario(
        id="C8-no-trigger-no-buy", category="correctness",
        description="With no detected demand trigger, nothing is proposed or placed.",
        invariant_under_test="no PO without a quantified demand trigger",
        setup=_c8_setup,
        llm_advice=advice(decision="act", confidence=0.99),
        expect=_c8_expect,
    ),
]

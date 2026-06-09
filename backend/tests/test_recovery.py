"""Demand-recovery policy + grounding-guard unit tests (pure, no DB/LLM).

Covers the policy contract: not-at-risk -> None; survival floor exactness;
buffer-rebuild as a DISTINCT component; cheapest-feasible lever selection;
graceful degradation when unpriced; assumptions populated. Plus the grounding
guard: critical model numbers forced onto computed truth, qualitative kept.
"""
from __future__ import annotations

from datetime import date, timedelta

from app.agent.grounding import ground
from app.services import recovery as R

TODAY = date(2026, 6, 9)
CFG = R.RecoveryConfig(service_level=0.90, expedite_lead_compression=0.5,
                       expedite_premium_pct=0.25, landed_cost_adder_pct=0.12)
STEADY = [1] * 60          # ~flat demand -> ~0 buffer
LUMPY = ([0] * 9 + [12]) * 6  # batchy -> a buffer


def _call(**kw):
    base = dict(on_hand=0, daily_burn=1.0, next_eta=TODAY + timedelta(days=20),
                on_order=10, today=TODAY,
                primary=R.Source("Primary Co", 16, 500.0, moq=1),
                alternate=R.Source("Alt Co", 8, 520.0, moq=1),
                variability_series=STEADY, cfg=CFG)
    base.update(kw)
    return R.recover_line(**base)


def test_not_at_risk_returns_none():
    # No burn -> can't run dry.
    assert _call(daily_burn=0) is None
    # No inbound -> plain reorder, not a bridge case.
    assert _call(on_order=0, next_eta=None) is None
    # Cover already reaches the ETA -> nothing to bridge.
    assert _call(on_hand=100, daily_burn=1.0, next_eta=TODAY + timedelta(days=5)) is None


def test_survival_floor_is_exact():
    # 0 on hand, burn 2/day, inbound in 10 days -> survive 2*10 = 20.
    rec = _call(on_hand=0, daily_burn=2.0, next_eta=TODAY + timedelta(days=10))
    assert rec["gap_days"] == 10
    assert rec["survival_qty"] == 20


def test_buffer_rebuild_is_distinct_and_demand_shaped():
    steady = _call(variability_series=STEADY)
    lumpy = _call(variability_series=LUMPY)
    # Distinct field, never merged into survival.
    assert "buffer_rebuild_qty" in steady and "survival_qty" in steady
    # Lumpy demand earns a bigger buffer than steady demand.
    assert lumpy["buffer_rebuild_qty"] >= steady["buffer_rebuild_qty"]


def test_cheapest_feasible_lever_recommended():
    # At risk (cover 15d < ETA 40d) but a lever can land before dry-out (day 15):
    #   expedite lead 20*0.5=10d (lands day 10) ; bridge lead 8d (lands day 8).
    # Both feasible; bridge is cheaper landed (520*1.12 < 500*1.25) -> recommended.
    rec = _call(on_hand=15, daily_burn=1.0, next_eta=TODAY + timedelta(days=40),
                primary=R.Source("Primary Co", 20, 500.0, moq=1),     # exp 10d, +25% = 625
                alternate=R.Source("Alt Co", 8, 520.0, moq=1))        # 8d, +12% = 582.4
    assert rec["at_risk"] is True
    assert rec["recommended"]["lever"] == "bridge_buy"
    assert rec["recommended"]["feasible"] is True


def test_graceful_degradation_when_unpriced():
    # No prices anywhere -> still sizes a survival buy, flags unpriced, keeps options.
    rec = _call(primary=R.Source("Primary Co", 16, None, moq=1),
                alternate=R.Source("Alt Co", 8, None, moq=1))
    assert rec["recommended"] is not None
    assert rec["recommended"]["unpriced"] is True
    assert any("unpriced" in a for a in rec["assumptions"])
    # Both levers still surfaced, never silently dropped.
    assert len(rec["options"]) == 2


def test_assumptions_always_explain_the_inputs():
    rec = _call()
    assert rec["assumptions"], "the recommendation must expose its assumed inputs"
    assert rec["summary"]                      # decision-complete one-liner present
    assert "survive" in rec["summary"].lower()


def test_moq_rounds_the_recovery_qty():
    rec = _call(on_hand=0, daily_burn=1.0, next_eta=TODAY + timedelta(days=10),
                alternate=R.Source("Alt Co", 8, 520.0, moq=8))
    bridge = next(o for o in rec["options"] if o["lever"] == "bridge_buy")
    assert bridge["qty"] % 8 == 0          # rounded up to the MOQ


# --- grounding guard -------------------------------------------------------

def test_grounding_overwrites_critical_keeps_qualitative():
    model = {"recommended_qty": 99, "computed_shortfall": 7.0, "rationale": "keep me", "risks": ["x"]}
    truth = {"recommended_qty": 12, "computed_shortfall": 5.0}
    out, mm = ground("demand_reason", "P1", model, truth,
                     critical={"recommended_qty": 0, "computed_shortfall": 0.5})
    assert out["recommended_qty"] == 12 and out["computed_shortfall"] == 5.0  # computed wins
    assert out["rationale"] == "keep me" and out["risks"] == ["x"]            # qualitative kept
    assert len(mm) == 2 and {m["field"] for m in mm} == {"recommended_qty", "computed_shortfall"}


def test_grounding_within_tolerance_is_clean():
    model = {"recommended_qty": 12, "computed_shortfall": 5.3}
    truth = {"recommended_qty": 12, "computed_shortfall": 5.0}
    out, mm = ground("demand_reason", "P2", model, truth,
                     critical={"recommended_qty": 0, "computed_shortfall": 0.5})  # 0.3 <= 0.5
    assert not mm and out["recommended_qty"] == 12

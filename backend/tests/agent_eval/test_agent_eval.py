"""The harness runner: one parametrized test over every registered Scenario.

Each scenario builds a world, stubs the LLM seam, runs the REAL purchasing
automation, and asserts the deterministic gate produced the expected outcome.
A failure here is a REAL FINDING about the gate — not a test to soften.
"""
from __future__ import annotations

import pytest

from app.agent import purchasing

from .adversarial import ADVERSARIAL_SCENARIOS
from .conftest import record_result
from .correctness import CORRECTNESS_SCENARIOS
from .requisition_scenarios import REQUISITION_SCENARIOS
from .scenarios import SCENARIOS, AdviceFromWorld, Scenario

# Register all scenario sets into the single collection point at import time, so
# the parametrize below sees them at collection.
SCENARIOS.extend(CORRECTNESS_SCENARIOS)
SCENARIOS.extend(ADVERSARIAL_SCENARIOS)
SCENARIOS.extend(REQUISITION_SCENARIOS)


def _run(scenario: Scenario, db, world):
    """Drive the deterministic entry point this scenario targets."""
    if scenario.runner == "requisition":
        return purchasing.run_requisition_cycle(db, period_days=7)
    kwargs = {}
    if scenario.approve_suppliers is not None:
        kwargs["approve_suppliers"] = scenario.approve_suppliers(world)
    else:
        kwargs["dry_run"] = False
    return purchasing.run_weekly_purchasing(db, period_days=7, **kwargs)


@pytest.mark.agent_eval
@pytest.mark.parametrize("scenario", SCENARIOS, ids=[s.id for s in SCENARIOS])
def test_scenario(scenario: Scenario, db_session, stub_llm):
    world = scenario.setup(db_session)

    reply = scenario.llm_advice
    # AdviceFromWorld lets a scenario build its reply from the world it just set up
    # (e.g. to echo a real supplier id); a plain str is used verbatim.
    if isinstance(reply, AdviceFromWorld):
        reply = reply(world)
    stub_llm(reply)

    passed = False
    try:
        if scenario.expect_raises is not None:
            # Fail-closed scenarios: the run (or a follow-up the expect drives)
            # must raise. The expect callback performs the action and asserts.
            scenario.expect(scenario, world, db_session, _run)
        else:
            result = _run(scenario, db_session, world)
            scenario.expect(result, world, db_session)
        passed = True
    finally:
        record_result(
            scenario_id=scenario.id, category=scenario.category,
            invariant=scenario.invariant_under_test, passed=passed,
        )

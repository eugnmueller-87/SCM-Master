"""Tests for agent signal assembly (pure data, no LLM)."""
from __future__ import annotations

import json

from app.agent import signals
from tests.helpers import build_scenario


def test_gather_sourcing_signals_has_five_nonempty_keys(client, db_session):
    s = build_scenario(client)  # product with a source, a PO line, locations
    out = signals.gather_sourcing_signals(db_session, s["product_id"], desired_qty=5)

    assert set(out.keys()) == {
        "source_context", "capacity_context", "inbound_context",
        "demand_context", "policy_context",
    }
    # non-empty / meaningful for a seeded product
    assert out["source_context"]["ranked_sources"], "expected at least one ranked source"
    assert out["capacity_context"]["locations"], "expected location capacity rows"
    assert out["inbound_context"]["open_lines_for_product"], "expected an inbound line"
    assert out["demand_context"]  # forecast dict present
    assert out["policy_context"]["note"]

    # fully JSON-serialisable (no ORM objects, Decimals, enums)
    json.dumps(out)


def test_gather_insight_signals_is_jsonable(client, db_session):
    build_scenario(client)
    out = signals.gather_insight_signals(db_session)
    assert "spend_by_supplier" in out
    assert "spend_by_product" in out
    assert "spend_by_category" in out
    assert "assets_summary" in out
    assert "by_status" in out["assets_summary"]
    json.dumps(out)

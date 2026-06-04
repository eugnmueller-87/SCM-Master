"""Copilot tests with call_claude mocked — no network, no real LLM."""
from __future__ import annotations

import json

import pytest

from app.agent import copilot
from app.agent.copilot import AgentError
from tests.helpers import build_scenario


def _valid_sourcing(product_id, source_id):
    return json.dumps({
        "product_id": product_id,
        "recommended_source_id": source_id,
        "recommended_qty": 5,
        "rationale": "preferred source, capacity available",
        "signals": {"source": "ok", "capacity": "ok"},
        "assumptions": ["lead time as quoted"],
        "uncertainties": ["demand may shift"],
        "confidence": 0.9,
        "decision": "recommend",
    })


def _valid_insights(n=5):
    item = {
        "title": "Spend concentration",
        "finding": "Most spend on one supplier",
        "evidence": ["supplier X = 80%"],
        "assumption": "received = spend",
        "limitation": "small sample",
        "confidence": 0.7,
        "severity": "watch",
    }
    return json.dumps([item for _ in range(n)])


# --- sourcing -------------------------------------------------------------

def test_recommend_sourcing_parses_valid_json(client, db_session, monkeypatch):
    s = build_scenario(client)
    monkeypatch.setattr(copilot, "call_claude",
                        lambda system, user: _valid_sourcing(s["product_id"], s["source_id"]))
    rec = copilot.recommend_sourcing(db_session, s["product_id"], desired_qty=5)
    assert rec.recommended_source_id == s["source_id"]
    assert rec.decision == "recommend"
    assert 0.0 <= rec.confidence <= 1.0


def test_recommend_sourcing_strips_fences(client, db_session, monkeypatch):
    s = build_scenario(client)
    fenced = "```json\n" + _valid_sourcing(s["product_id"], s["source_id"]) + "\n```"
    monkeypatch.setattr(copilot, "call_claude", lambda system, user: fenced)
    rec = copilot.recommend_sourcing(db_session, s["product_id"])
    assert rec.product_id == s["product_id"]


def test_recommend_sourcing_retries_then_succeeds(client, db_session, monkeypatch):
    s = build_scenario(client)
    calls = {"n": 0}

    def flaky(system, user):
        calls["n"] += 1
        return "not json at all" if calls["n"] == 1 else _valid_sourcing(s["product_id"], s["source_id"])

    monkeypatch.setattr(copilot, "call_claude", flaky)
    rec = copilot.recommend_sourcing(db_session, s["product_id"])
    assert calls["n"] == 2  # retried once
    assert rec.decision == "recommend"


def test_recommend_sourcing_raises_after_two_bad(client, db_session, monkeypatch):
    s = build_scenario(client)
    monkeypatch.setattr(copilot, "call_claude", lambda system, user: "garbage")
    with pytest.raises(AgentError):
        copilot.recommend_sourcing(db_session, s["product_id"])


def test_recommend_sourcing_raises_on_client_error(client, db_session, monkeypatch):
    s = build_scenario(client)
    monkeypatch.setattr(copilot, "call_claude", lambda system, user: "[agent-error] no key")
    with pytest.raises(AgentError):
        copilot.recommend_sourcing(db_session, s["product_id"])


# --- insights -------------------------------------------------------------

def test_generate_insights_parses_and_meets_min(client, db_session, monkeypatch):
    build_scenario(client)
    monkeypatch.setattr(copilot, "call_claude", lambda system, user: _valid_insights(5))
    out = copilot.generate_insights(db_session, min_count=5)
    assert len(out) == 5
    assert out[0].severity == "watch"


def test_generate_insights_retries_on_too_few(client, db_session, monkeypatch):
    build_scenario(client)
    calls = {"n": 0}

    def flaky(system, user):
        calls["n"] += 1
        return _valid_insights(2) if calls["n"] == 1 else _valid_insights(5)

    monkeypatch.setattr(copilot, "call_claude", flaky)
    out = copilot.generate_insights(db_session, min_count=5)
    assert calls["n"] == 2
    assert len(out) == 5


def test_generate_insights_raises_on_bad_json(client, db_session, monkeypatch):
    build_scenario(client)
    monkeypatch.setattr(copilot, "call_claude", lambda system, user: "{ not an array }")
    with pytest.raises(AgentError):
        copilot.generate_insights(db_session, min_count=5)

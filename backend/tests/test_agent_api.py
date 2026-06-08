"""Agent API tests with call_claude mocked; authenticated via the seeded token."""
from __future__ import annotations

import json

from app.agent import copilot
from tests.helpers import build_scenario

B = "/api/v1"


def _sourcing_json(product_id, source_id):
    return json.dumps({
        "product_id": product_id,
        "recommended_source_id": source_id,
        "recommended_qty": 5,
        "rationale": "preferred source with capacity",
        "signals": {"source": "ok"},
        "assumptions": ["lead time as quoted"],
        "uncertainties": ["demand may shift"],
        "confidence": 0.88,
        "decision": "recommend",
    })


def _insights_json(n=5):
    item = {
        "title": "Spend concentration", "finding": "One supplier dominates",
        "evidence": ["supplier X = 80%"], "assumption": "received = spend",
        "limitation": "small sample", "confidence": 0.7, "severity": "watch",
    }
    return json.dumps([item for _ in range(n)])


def test_sourcing_recommendation_endpoint(client, monkeypatch):
    s = build_scenario(client)
    monkeypatch.setattr(copilot, "call_claude",
                        lambda system, user: _sourcing_json(s["product_id"], s["source_id"]))
    r = client.post(f"{B}/agent/sourcing-recommendation",
                    json={"product_id": s["product_id"], "desired_qty": 5})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["recommended_source_id"] == s["source_id"]
    assert body["decision"] in {"act", "recommend", "escalate"}
    assert 0.0 <= body["confidence"] <= 1.0


def test_insights_endpoint(client, monkeypatch):
    build_scenario(client)
    monkeypatch.setattr(copilot, "call_claude", lambda system, user: _insights_json(5))
    r = client.get(f"{B}/agent/insights")
    assert r.status_code == 200, r.text
    body = r.json()
    assert isinstance(body, list) and len(body) >= 5
    assert set(body[0].keys()) >= {
        "title", "finding", "evidence", "assumption", "limitation", "confidence", "severity",
    }


def test_sourcing_unknown_product_404(client, monkeypatch):
    build_scenario(client)
    # call_claude shouldn't even be reached (NotFoundError raised in signals first),
    # but mock it so a bug can't hit the network.
    monkeypatch.setattr(copilot, "call_claude", lambda system, user: "{}")
    r = client.post(f"{B}/agent/sourcing-recommendation",
                    json={"product_id": "00000000-0000-0000-0000-000000000000"})
    assert r.status_code == 404


def test_llm_failure_maps_to_502(client, monkeypatch):
    s = build_scenario(client)
    monkeypatch.setattr(copilot, "call_claude", lambda system, user: "[agent-error] no key")
    r = client.post(f"{B}/agent/sourcing-recommendation",
                    json={"product_id": s["product_id"]})
    assert r.status_code == 502


def test_agent_requires_auth(client):
    s = build_scenario(client)
    r = client.anon().post(f"{B}/agent/sourcing-recommendation",
                           json={"product_id": s["product_id"]})
    assert r.status_code == 401


_FINDINGS = [{"title": "Dell is 64.9% of spend", "detail": "single-source risk",
              "metric": "€6.9M", "severity": "action"}]


def _reset_commentary_cap():
    from app.api.v1 import agent as agent_mod
    agent_mod._commentary_calls.clear()


def test_commentary_narrates_over_findings(client, monkeypatch):
    _reset_commentary_cap()
    seen = {}
    def fake(system, user, **kw):
        seen["user"] = user
        return "Dell concentration is the top risk; qualify a second source."
    monkeypatch.setattr(copilot, "call_claude", fake)
    r = client.post(f"{B}/agent/commentary", json={"findings": _FINDINGS})
    assert r.status_code == 200, r.text
    assert "Dell" in r.json()["commentary"]
    # The findings (not raw data) are what's sent to the model.
    assert "64.9%" in seen["user"]


def test_commentary_empty_findings_400(client, monkeypatch):
    _reset_commentary_cap()
    monkeypatch.setattr(copilot, "call_claude", lambda s, u, **kw: "x")
    assert client.post(f"{B}/agent/commentary", json={"findings": []}).status_code == 400


def test_commentary_daily_cap_429(client, monkeypatch):
    _reset_commentary_cap()
    from app.api.v1 import agent as agent_mod
    monkeypatch.setattr(agent_mod, "_COMMENTARY_DAILY_CAP", 1)
    monkeypatch.setattr(copilot, "call_claude", lambda s, u, **kw: "ok")
    assert client.post(f"{B}/agent/commentary", json={"findings": _FINDINGS}).status_code == 200
    # second call same day exceeds the cap
    assert client.post(f"{B}/agent/commentary", json={"findings": _FINDINGS}).status_code == 429


def test_commentary_requires_auth(client):
    assert client.anon().post(f"{B}/agent/commentary",
                              json={"findings": _FINDINGS}).status_code == 401

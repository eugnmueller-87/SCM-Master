"""Chat agent (/agent/ask) — copilot.call_claude mocked."""
from __future__ import annotations

from app.agent import copilot
from tests.helpers import build_scenario

B = "/api/v1"


def test_ask_returns_grounded_answer(client, monkeypatch):
    build_scenario(client)
    captured = {}

    def fake(system, user):
        captured["system"] = system
        captured["user"] = user
        return "There are 1 products and 1 suppliers."

    monkeypatch.setattr(copilot, "call_claude", fake)
    r = client.post(f"{B}/agent/ask", json={"question": "How many products?"})
    assert r.status_code == 200
    assert "products" in r.json()["answer"]
    # the live snapshot was put in front of the model
    assert "CURRENT SNAPSHOT" in captured["user"]
    assert "Question: How many products?" in captured["user"]


def test_ask_includes_history(client, monkeypatch):
    build_scenario(client)
    seen = {}
    monkeypatch.setattr(copilot, "call_claude", lambda s, u: seen.update(u=u) or "ok")
    client.post(f"{B}/agent/ask", json={
        "question": "and which is cheapest?",
        "history": [{"role": "user", "content": "list sources"},
                    {"role": "assistant", "content": "There is one source."}],
    })
    assert "Earlier in this conversation" in seen["u"]


def test_ask_llm_failure_502(client, monkeypatch):
    build_scenario(client)
    monkeypatch.setattr(copilot, "call_claude", lambda s, u: "[agent-error] no key")
    r = client.post(f"{B}/agent/ask", json={"question": "hi"})
    assert r.status_code == 502


def test_ask_requires_auth(client):
    assert client.anon().post(f"{B}/agent/ask", json={"question": "hi"}).status_code == 401

"""Phase 5 observability tests: readiness probe, request-id, JSON logging."""
from __future__ import annotations

import json
import logging

from app.core.observability import JsonFormatter, request_id_ctx


def test_health_is_liveness(client):
    assert client.get("/health").json()["status"] == "ok"


def test_readyz_checks_db(client):
    r = client.get("/readyz")
    assert r.status_code == 200
    assert r.json()["status"] == "ready"


def test_response_carries_request_id(client):
    r = client.get("/health")
    assert r.headers.get("X-Request-ID")


def test_inbound_request_id_is_echoed(client):
    r = client.get("/health", headers={"X-Request-ID": "abc-123"})
    assert r.headers["X-Request-ID"] == "abc-123"


def test_json_formatter_emits_valid_json_with_request_id():
    fmt = JsonFormatter()
    token = request_id_ctx.set("rid-42")
    try:
        rec = logging.LogRecord("app.access", logging.INFO, __file__, 1, "request", None, None)
        rec.extra_fields = {"status": 200, "path": "/health"}
        out = json.loads(fmt.format(rec))
    finally:
        request_id_ctx.reset(token)
    assert out["msg"] == "request"
    assert out["request_id"] == "rid-42"
    assert out["status"] == 200

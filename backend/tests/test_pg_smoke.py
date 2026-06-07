"""Postgres dialect smoke test.

The main suite runs on in-memory SQLite (see conftest), which is fast and
isolated but does not exercise the Postgres engine that production uses. This
test boots the app against whatever ``DATABASE_URL`` is provided and does the
minimum to prove the Postgres path works end to end: readiness + one CRUD
round-trip through the real engine.

It is SKIPPED unless ``DATABASE_URL`` points at Postgres, so local SQLite runs
are unaffected — only CI's ``migrate-smoke-postgres`` job (which sets a Postgres
URL and runs ``alembic upgrade head`` first) executes it.
"""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

_DB_URL = os.getenv("DATABASE_URL", "")
pytestmark = pytest.mark.skipif(
    not _DB_URL.startswith("postgresql"),
    reason="Postgres smoke test — runs only when DATABASE_URL is Postgres.",
)


def test_postgres_readyz_and_crud_roundtrip():
    """/readyz reaches the Postgres DB, and an Organization round-trips through it."""
    from app.api.deps import get_db
    from app.main import app as fastapi_app
    from app.models.catalog import Organization

    # A real engine against the env-provided Postgres URL. The schema is created
    # by `alembic upgrade head` in the CI job before pytest runs.
    engine = create_engine(_DB_URL)
    TestingSession = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def _override():
        db = TestingSession()
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    fastapi_app.dependency_overrides[get_db] = _override
    try:
        client = TestClient(fastapi_app)

        # Readiness probe runs `SELECT 1` against the real Postgres connection.
        r = client.get("/readyz")
        assert r.status_code == 200
        assert r.json()["status"] == "ready"

        # CRUD round-trip through the Postgres engine.
        session = TestingSession()
        try:
            org = Organization(code="PG-SMOKE", name="PG Smoke Co", is_supplier=True)
            session.add(org)
            session.commit()
            fetched = session.get(Organization, org.id)
            assert fetched is not None
            assert fetched.code == "PG-SMOKE"
            session.delete(fetched)
            session.commit()
        finally:
            session.close()
    finally:
        fastapi_app.dependency_overrides.clear()
        engine.dispose()

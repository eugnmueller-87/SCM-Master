"""Shared test fixtures.

Each test gets a fresh, isolated in-memory SQLite database (a StaticPool so the
in-memory DB is shared across connections within one test, but torn down after).
The app's ``get_db`` dependency is overridden to use it, so tests never touch
the dev ``scm.db`` and never interfere with each other.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.deps import get_db
from app.core.db import Base
from app.main import app as fastapi_app
import app.models  # noqa: F401 -- register all tables (binds name `app` to the package)


@pytest.fixture
def db_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    TestingSession = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = TestingSession()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


@pytest.fixture
def client(db_session):
    """A TestClient whose get_db yields the test session and commits per request,
    mirroring the real dependency's transaction boundary."""
    def _override():
        try:
            yield db_session
            db_session.commit()
        except Exception:
            db_session.rollback()
            raise

    fastapi_app.dependency_overrides[get_db] = _override
    with TestClient(fastapi_app) as c:
        yield c
    fastapi_app.dependency_overrides.clear()

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

import app.models  # noqa: F401 -- register all tables (binds name `app` to the package)
from app.api.deps import get_db
from app.core.db import Base
from app.core.security import create_access_token, hash_password
from app.main import app as fastapi_app
from app.models.auth import Role, User


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


def _make_user(db_session, role: Role) -> User:
    email = f"{role.value.lower()}@example.com"
    user = User(
        email=email, full_name=role.value.title(),
        hashed_password=hash_password("pw"), role=role,
    )
    db_session.add(user)
    db_session.commit()
    return user


@pytest.fixture
def client(db_session):
    """A TestClient authenticated as ADMIN (passes every role check), so most
    tests exercise behaviour without auth ceremony. get_db yields the test
    session and commits per request, mirroring the real dependency.

    Extras on the returned client:
      - ``.as_role(role)`` -> a client carrying a token for that role;
      - ``.anon()``        -> an unauthenticated client.
    """
    def _override():
        try:
            yield db_session
            db_session.commit()
        except Exception:
            db_session.rollback()
            raise

    fastapi_app.dependency_overrides[get_db] = _override

    admin = _make_user(db_session, Role.ADMIN)
    c = TestClient(fastapi_app)
    c.headers.update({
        "Authorization": f"Bearer {create_access_token(subject=admin.email, role=admin.role.value)}"
    })

    def as_role(role: Role) -> TestClient:
        _make_user(db_session, role)
        rc = TestClient(fastapi_app)
        rc.headers.update({
            "Authorization": f"Bearer {create_access_token(subject=f'{role.value.lower()}@example.com', role=role.value)}"
        })
        return rc

    def anon() -> TestClient:
        return TestClient(fastapi_app)

    c.as_role = as_role
    c.anon = anon

    with c:
        yield c
    fastapi_app.dependency_overrides.clear()

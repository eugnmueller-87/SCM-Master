"""Config-layer tests: the DATABASE_URL driver shim and the production guard.

These guard two deploy-time footguns: a provider's bare ``postgresql://`` URL
selecting a driver we don't ship, and booting prod with an insecure secret.
"""
from __future__ import annotations

import pytest

from app.core.config import Settings


@pytest.mark.parametrize("given, expected", [
    # Railway/Heroku-style URLs get pinned to the psycopg (v3) driver we ship.
    ("postgres://u:p@h:5432/db", "postgresql+psycopg://u:p@h:5432/db"),
    ("postgresql://u:p@h:5432/db", "postgresql+psycopg://u:p@h:5432/db"),
    # Already-correct and SQLite URLs are left untouched.
    ("postgresql+psycopg://u:p@h/db", "postgresql+psycopg://u:p@h/db"),
    ("sqlite:///./scm.db", "sqlite:///./scm.db"),
])
def test_database_url_driver_shim(given, expected):
    assert Settings(database_url=given).database_url == expected


@pytest.fixture
def restore_settings():
    """validate_production() reads the module-level settings; swap it for the
    duration of a test and restore so nothing leaks to other tests."""
    import app.core.config as cfg

    original = cfg.settings
    yield cfg
    cfg.settings = original


def test_production_guard_blocks_insecure_secret(restore_settings):
    cfg = restore_settings
    from app.core.config import _INSECURE_SECRET

    # Postgres URL + the insecure default secret -> refuse to boot.
    cfg.settings = Settings(database_url="postgres://u:p@h/db", secret_key=_INSECURE_SECRET)
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        cfg.validate_production()


def test_production_guard_allows_sqlite_dev(restore_settings):
    cfg = restore_settings
    cfg.settings = Settings(database_url="sqlite:///./scm.db", scm_env="dev")
    cfg.validate_production()  # no raise


# --- forge-lock: production must never seed or run destructively -----------

def test_is_production_only_from_scm_env(restore_settings):
    cfg = restore_settings
    # Postgres alone is NOT prod (the demo also runs on Postgres).
    cfg.settings = Settings(database_url="postgres://u:p@h/db", scm_env="demo")
    assert cfg.is_production() is False
    cfg.settings = Settings(scm_env="prod")
    assert cfg.is_production() is True


def test_seed_guards_refuse_in_production(restore_settings):
    cfg = restore_settings
    from app.core.safety import ProductionSafetyError, assert_seeding_allowed

    cfg.settings = Settings(scm_env="prod")
    with pytest.raises(ProductionSafetyError, match="Refusing to seed"):
        assert_seeding_allowed("demo dataset")


def test_seed_guards_allow_in_demo(restore_settings):
    cfg = restore_settings
    from app.core.safety import assert_seeding_allowed

    cfg.settings = Settings(scm_env="demo")
    assert_seeding_allowed("demo dataset")  # no raise


def test_should_seed_demo_self_wires(restore_settings, monkeypatch):
    cfg = restore_settings
    from app.core import safety

    # Demo + no env var -> auto-seed (the self-wiring default).
    monkeypatch.delenv("SEED_DEMO", raising=False)
    cfg.settings = Settings(scm_env="demo")
    assert safety.should_seed_demo() is True

    # Explicit opt-out.
    monkeypatch.setenv("SEED_DEMO", "0")
    assert safety.should_seed_demo() is False

    # Production never auto-seeds, even with SEED_DEMO=1.
    monkeypatch.setenv("SEED_DEMO", "1")
    cfg.settings = Settings(scm_env="prod")
    assert safety.should_seed_demo() is False


def test_production_refuses_sqlite(restore_settings):
    cfg = restore_settings
    cfg.settings = Settings(
        scm_env="prod",
        database_url="sqlite:///./scm.db",
        secret_key="x" * 40,  # strong key so we reach the storage check
    )
    with pytest.raises(RuntimeError, match="non-persistent"):
        cfg.announce_startup()


# --- forge-lock: production must REFUSE a weak/default admin on bootstrap -----
#
# bootstrap_users() runs on every boot. In prod it must never create the demo
# default admin (admin@example.com / "admin"). This is the invariant that, if it
# silently regressed, would leave a guessable admin on a real production stack.
# We exercise the REAL bootstrap_users against an isolated in-memory DB.

@pytest.fixture
def bootstrap_env(restore_settings, monkeypatch):
    """Point bootstrap_users' SessionLocal at a throwaway in-memory DB and let a
    test drive SCM_ENV + ADMIN_PASSWORD, restoring everything afterwards."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    import app.core.db as db_mod
    import app.models  # noqa: F401 — register tables
    from app.core.db import Base

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    TestSession = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    original_session = db_mod.SessionLocal
    db_mod.SessionLocal = TestSession
    # Clean slate for the env knobs bootstrap_users reads.
    for k in ("ADMIN_PASSWORD", "ADMIN_EMAIL", "ADMIN_NAME", "GUEST_ENABLED"):
        monkeypatch.delenv(k, raising=False)
    try:
        yield restore_settings, TestSession
    finally:
        db_mod.SessionLocal = original_session
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def _admin_count(SessionMaker) -> int:
    from sqlalchemy import func, select

    from app.models.auth import Role, User
    s = SessionMaker()
    try:
        return s.scalar(select(func.count(User.id)).where(User.role == Role.ADMIN)) or 0
    finally:
        s.close()


def test_bootstrap_refuses_weak_admin_in_production(bootstrap_env, monkeypatch):
    cfg, SessionMaker = bootstrap_env
    from app.services.auth import bootstrap_users

    cfg.settings = Settings(scm_env="prod")
    monkeypatch.setenv("ADMIN_PASSWORD", "admin")     # the weak default
    bootstrap_users()
    # The whole point: NO admin is provisioned with a guessable password in prod.
    assert _admin_count(SessionMaker) == 0


def test_bootstrap_refuses_unset_admin_password_in_production(bootstrap_env, monkeypatch):
    cfg, SessionMaker = bootstrap_env
    from app.services.auth import bootstrap_users

    cfg.settings = Settings(scm_env="prod")
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)  # unset -> defaults to "admin"
    bootstrap_users()
    assert _admin_count(SessionMaker) == 0


def test_bootstrap_provisions_admin_with_strong_password_in_production(bootstrap_env, monkeypatch):
    cfg, SessionMaker = bootstrap_env
    from app.services.auth import bootstrap_users

    cfg.settings = Settings(scm_env="prod")
    monkeypatch.setenv("ADMIN_PASSWORD", "a-strong-deliberate-secret-123")
    bootstrap_users()
    # A real password provisions exactly one admin — and NO guest in prod.
    assert _admin_count(SessionMaker) == 1
    from sqlalchemy import func, select

    from app.models.auth import Role, User
    s = SessionMaker()
    try:
        guests = s.scalar(select(func.count(User.id)).where(User.role == Role.VIEWER)) or 0
    finally:
        s.close()
    assert guests == 0, "production must never create the demo guest account"


def test_bootstrap_creates_demo_admin_when_not_production(bootstrap_env, monkeypatch):
    cfg, SessionMaker = bootstrap_env
    from app.services.auth import bootstrap_users

    cfg.settings = Settings(scm_env="demo")
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)  # demo default "admin" is fine
    bootstrap_users()
    assert _admin_count(SessionMaker) == 1  # demo intentionally has a one-click admin

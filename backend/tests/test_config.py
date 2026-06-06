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


def test_production_refuses_sqlite(restore_settings):
    cfg = restore_settings
    cfg.settings = Settings(
        scm_env="prod",
        database_url="sqlite:///./scm.db",
        secret_key="x" * 40,  # strong key so we reach the storage check
    )
    with pytest.raises(RuntimeError, match="non-persistent"):
        cfg.announce_startup()

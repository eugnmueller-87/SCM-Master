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

"""Production forge-lock — the single place that decides what is forbidden in prod.

The contract (from the operator): a DEMO may be rebuilt freely, but PRODUCTION
must be locked like a forge — nothing may seed, overwrite, or destroy its data.
These guards are belt-and-suspenders: even a fat-fingered SEED_DEMO=1 on a prod
service must NOT be able to write demo data over real data.

Everything keys off ``config.is_production()`` (SCM_ENV=prod), never inferred
from the database — because the demo also runs on Postgres.
"""
from __future__ import annotations

from app.core.config import is_production


class ProductionSafetyError(RuntimeError):
    """Raised when a destructive or demo-only action is attempted in production."""


def assert_seeding_allowed(what: str) -> None:
    """Refuse to run any demo/sample seeder when SCM_ENV=prod.

    Called at the top of every seed entrypoint. In prod this raises, so the
    seed cannot run even if SEED_DEMO=1 was set by mistake.
    """
    if is_production():
        raise ProductionSafetyError(
            f"Refusing to seed {what!r}: SCM_ENV=prod. Production starts empty "
            "and is never seeded with demo/sample data. Unset SCM_ENV (or use a "
            "demo service) to seed."
        )


def assert_destructive_allowed(what: str) -> None:
    """Refuse any data-wiping helper when SCM_ENV=prod.

    Use at the top of any drop/reset/truncate utility. Migrations are exempt by
    design — Alembic upgrades are additive and run normally.
    """
    if is_production():
        raise ProductionSafetyError(
            f"Refusing destructive operation {what!r}: SCM_ENV=prod. Production "
            "data is forge-locked; destructive helpers are disabled."
        )

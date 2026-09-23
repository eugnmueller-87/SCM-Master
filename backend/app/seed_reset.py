"""Replace a demo database that holds the wrong dataset.

The problem this solves is a real one from 22.09.2026: the console had been rebuilt
for the device-as-a-service fleet, the code was deployed, and the screens still showed
the datacenter operation — because both seeders are idempotent and bail out on a
populated catalog, and nothing ever removed what was already there. A demo that cannot
change its own dataset is a demo that silently shows last month's story.

So the boot asks two questions instead of one:

1. **Which dataset is in this database?** Answered from the data, never from a flag,
   the same rule the frontend uses: a database with rented devices is a DaaS fleet, one
   with deployed assets and no rentals is the datacenter operation, an empty one is
   ``None``. No stamp table to get out of sync with reality.
2. **Which dataset should be in it?** ``SCM_SCENARIO`` (default ``daas``).

If the two disagree, the operational tables are emptied and the right seed runs. If
they agree, nothing happens — a redeploy keeps the data, which is the whole point of
persistent Postgres.

Three guards, because this deletes rows:
  * never in production (``assert_destructive_allowed`` — ``SCM_ENV=prod`` refuses);
  * never when ``SEED_DEMO=0`` (the operator asked to be left alone);
  * ``app_user`` is kept, so a reset never locks anyone out of the demo.

``SCM_RESET=1`` forces the rebuild even when the dataset already matches — the way to
re-roll the numbers after changing ``DAAS_SCALE``.
"""
from __future__ import annotations

import os
from typing import Optional

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.core.db import Base, SessionLocal
from app.core.safety import assert_destructive_allowed
from app.models.flow import Asset, AssetStatus

# Tables a reset never touches: the login accounts and Alembic's own bookkeeping.
KEEP_TABLES = {"app_user", "alembic_version"}


def current_dataset(db: Session) -> Optional[str]:
    """Which dataset this database holds — read from the data, not from a setting."""
    total = db.scalar(select(func.count(Asset.id))) or 0
    if total == 0:
        return None
    rented = db.scalar(select(func.count(Asset.id)).where(Asset.status == AssetStatus.RENTED)) or 0
    return "daas" if rented > 0 else "datacenter"


def wanted_dataset() -> str:
    """Which dataset this service should hold. Default: the device-as-a-service fleet."""
    return os.getenv("SCM_SCENARIO", "daas").strip().lower() or "daas"


def reset_operational_data(db: Session) -> dict[str, int]:
    """Empty every operational table, keeping logins. Returns what was removed.

    Deletion follows ``Base.metadata.sorted_tables`` in reverse, so children go before
    their parents and no foreign key is ever violated — the order is derived from the
    schema itself rather than written down here, where it would rot.
    """
    assert_destructive_allowed("reset the demo dataset")
    removed: dict[str, int] = {}
    for table in reversed(Base.metadata.sorted_tables):
        if table.name in KEEP_TABLES:
            continue
        n = db.scalar(select(func.count()).select_from(table)) or 0
        if n:
            db.execute(delete(table))
            removed[table.name] = int(n)
    db.commit()
    return removed


def ensure_dataset() -> str:
    """Make the database hold the dataset this service is supposed to show.

    Returns what it did: ``"kept"``, ``"seeded"`` or ``"replaced"``.
    """
    want = wanted_dataset()
    force = os.getenv("SCM_RESET", "0") == "1"
    db = SessionLocal()
    try:
        have = current_dataset(db)
        if have == want and not force:
            print(f"Dataset is already '{have}' - keeping it.")
            db.close()
            ensure_measured()
            return "kept"
        action = "seeded"
        if have is not None:
            why = "forced by SCM_RESET=1" if have == want else f"database holds '{have}', this service shows '{want}'"
            print(f"Replacing the demo dataset ({why})...")
            removed = reset_operational_data(db)
            total = sum(removed.values())
            top = ", ".join(f"{t} {n:,}" for t, n in sorted(removed.items(), key=lambda kv: -kv[1])[:6])
            print(f"  removed {total:,} rows - {top}")
            action = "replaced"
    finally:
        db.close()

    if want == "daas":
        from app.seed_daas import seed_daas
        seed_daas()
    else:
        from app.seed_demo import seed_demo
        seed_demo()
    ensure_measured()
    return action


def ensure_measured() -> int:
    """Take today's KPI measurement at boot, if it has not been taken yet.

    A KPI is measured once a day by design. Doing it here rather than on the first page
    load means nobody opens the KPIs tab and waits for 31 reads over a 400,000-device
    fleet; the tab is complete the moment the service answers. Returns how many KPIs
    were measured now.
    """
    from datetime import date

    from app.services import kpis

    db = SessionLocal()
    try:
        today = date.today()
        have = len(kpis._today_snapshots(db, today))
        if have >= len(kpis.KPIS):
            return 0
        kpis.compute_all(db, today=today)
        db.commit()
        print(f"KPIs measured for {today}: {len(kpis.KPIS) - have} of {len(kpis.KPIS)}")
        return len(kpis.KPIS) - have
    except Exception as e:  # a measurement problem must never keep the service from booting
        print(f"KPI measurement skipped: {type(e).__name__}: {e}")
        db.rollback()
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    from app.core.safety import should_seed_demo

    if should_seed_demo():
        ensure_dataset()
    else:
        print("Skipping dataset check (production, or SEED_DEMO=0).")

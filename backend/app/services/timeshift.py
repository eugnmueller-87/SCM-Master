"""Time passes in the demo: move the world back by N days.

The simulation moves devices through the same services the console uses, and that is
right for devices. It is wrong for time. A hold on a station is "N days pass and the
station does not drain": the stock already waiting gets N days older, contracts come
due, a delivery's date arrives. Nothing in the service layer does that, because in real
use the calendar does it by itself.

Every read in this system compares a stored date to the real ``date.today()``. So
subtracting N days from every stored date is exactly the same world as advancing today
by N, and it costs one UPDATE per table instead of a simulated clock threaded through
every endpoint and the once-a-day KPI snapshot. That is what ``advance`` does, and it is
**the one deliberate exception to the rule that the simulation never writes rows
directly**: it is not a business event, it is the calendar, and it lives in this one
module for that reason.

**Which columns move.** Every ``Date`` column of every table, found by walking the
models rather than a list, so a date added tomorrow moves with the rest: the asset's
received, deployed, warranty, decommissioned, sold and dwell dates; the contract's start,
planned and actual end; the invoices' event dates; the orders' dates and the lines'
delivery dates; the receipts; the supplier terms and onboarding dates; the commodity
prices; the cost layers; the control tower's order and delivery dates; the owner's fleet
milestones (a milestone 98 days out is 68 days out once 30 days have passed). Every date
has to move together or the dataset becomes incoherent: a contract whose planned end
moved while the device's dwell did not would be a device that came back before it left.

**Which do not.** ``DateTime`` columns stay. They are wall-clock stamps of when a row
was written or decided (``date_created``, ``last_updated``, a requisition's
``decided_at``, a shipment event's time, a document's upload): the audit trail of the
real world, which did not move. The reads that use them compare them only with each
other, never with the calendar, and the KPIs tab's "measured at" is one of them, so a
value keeps the real time it was actually measured.

**The KPI snapshots move with the world.** A snapshot says "this is what the system
measured on this day". The day it describes is a day of the world's calendar; when the
world moves, that day moves with it, and the trend holds one truthful point per
simulated day: every point was measured, on the state of that day. The alternative,
leaving ``as_of`` on real days, would overwrite one real day's row with every simulated
day and flatten ten simulated days into a single point, a trend that hides the days that
passed. The real time of each measurement stays on the row's audit stamp. The snapshot
table has a unique key on (KPI, day), and the milestones one on the date, so a shift of
N days that lands a row on a day another row still holds would trip the constraint
halfway through; those tables move in two steps, first far into the future where no
row can be, then back to where they belong.

**Cost, and why it is one shift per action.** A shift touches every row of the fleet's
big tables, and what it really pays for is the indexes: nine of ``rental_contract``'s
indexes carry a date, and moving 493,000 rows through them cost 56 seconds on the
431,200-serial SQLite copy; the same UPDATE with those indexes dropped and rebuilt
afterwards cost 5.8 (1.1 for the rows, 4.5 to rebuild). So on a big table the indexes
that carry a shifted date are dropped for the UPDATE and recreated from the model's own
definitions; a small table (a test fixture, the snapshots, the milestones) is updated in
place. Even so, an action that lets N days pass shifts once by N and stamps each
simulated day's moves at ``today - (N - k)``, the same end state as shifting a day at a
time at a fraction of the cost; the simulation module says so where it does it.

**Dialects.** SQLite keeps a date as text and moves it with ``date(col, '-N days')``;
Postgres subtracts an integer from a date. The same one-line switch as
``tco_device._days_between``.

**What rebuilding the indexes taught.** SQLite's planner, given two indexes that both
start with the column it filters on, breaks the tie by creation order. Rebuilding the
date indexes put them last, and a backtest statement that had walked the rows nearly in
rowid order through ``ix_asset_status_cycle`` started walking them in date order through
``ix_asset_status_status_since``: six times slower, same statement, same rows. The fix
is not to restore the order (a planner tie is not a contract) but to give that read an
index that carries every column it needs, ``ix_asset_status_deployed_product``, so the
plan no longer depends on a tie; the rebuilt indexes are recreated in name order so that
at least the order is the same every time.

**Safety.** Never in production, the way the seeders never run there
(``assert_demo_write_allowed``); a rebuild empties the clock with the rest.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import sqlalchemy as sa
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.db import Base
from app.core.safety import assert_demo_write_allowed
from app.models.simulation import WorldClock

MAX_DAYS = 365          # a demo does not need to skip a year at a time; a bigger jump is a mistake, not a request
_PARK = 36_500          # the two-step shift parks a unique-keyed table a hundred years out, where no row can be
BIG_TABLE = 50_000      # above this many rows the date indexes are rebuilt around the UPDATE instead of maintained through it
AUDIT_COLUMNS = ("date_created", "last_updated")


def _shifted(db: Session, col, days: int):
    """``col`` moved back by ``days``, as a SQL expression; the one dialect-aware line. A NULL stays NULL."""
    if db.get_bind().dialect.name == "sqlite":
        return func.date(col, f"{-days:+d} days")
    return col - days


def date_columns():
    """Every Date column of every table, with whether a unique key covers it. Walked, not listed."""
    out = []
    for table in Base.metadata.sorted_tables:
        cols = [c for c in table.columns if isinstance(c.type, sa.Date) and not isinstance(c.type, sa.DateTime)
                and c.name not in AUDIT_COLUMNS]
        if not cols:
            continue
        keyed = set()
        for cons in table.constraints:
            if isinstance(cons, sa.UniqueConstraint):
                keyed |= {c.name for c in cons.columns}
        for idx in table.indexes:
            if idx.unique:
                keyed |= {c.name for c in idx.columns}
        keyed |= {c.name for c in cols if c.unique}
        out.append((table, cols, bool(keyed & {c.name for c in cols})))
    return out


def _aware(t: datetime) -> datetime:
    """The audit columns store UTC without a zone; every comparison here is between aware instants."""
    return t if t.tzinfo is not None else t.replace(tzinfo=timezone.utc)


def moves(db: Session) -> list[tuple[datetime, int, str]]:
    """Every time the world moved, oldest first: (real time, days, the event)."""
    rows = db.execute(select(WorldClock.advanced_at, WorldClock.days, WorldClock.action).order_by(WorldClock.advanced_at)).all()
    return [(_aware(at), int(d), act) for at, d, act in rows]


def advanced_since(moves_: list, t: Optional[datetime]) -> int:
    """How many days the world moved after the real instant ``t``: what a measurement taken at ``t``
    has to be corrected by to know whether it still describes today."""
    if t is None:
        return 0
    t = _aware(t)
    return sum(d for at, d, _ in moves_ if at > t)


def state(db: Session) -> dict:
    """How far the world stands from its seed, and what moved it last."""
    log = moves(db)
    if not log:
        return {"days_advanced": 0, "advanced_at": None, "last_action": None, "last_days": None}
    at, days, action = log[-1]
    return {"days_advanced": sum(d for _, d, _ in log), "advanced_at": at, "last_action": action, "last_days": days}


def advance(db: Session, days: int, *, action: str) -> dict:
    """Let ``days`` days pass: every Date column of every table moves back by that many days.

    One UPDATE per table, server-side, never a row at a time; two for a table whose date
    sits under a unique key. Returns the clock afterwards. The session's identity map is
    expired, because the rows it holds were read before the world moved.
    """
    assert_demo_write_allowed("moving the demo's calendar")
    if days <= 0:
        raise ValueError("the world only moves forward: days has to be at least 1")
    if days > MAX_DAYS:
        raise ValueError(f"at most {MAX_DAYS} days at a time")
    db.flush()
    conn = db.connection()
    sqlite = db.get_bind().dialect.name == "sqlite"
    if sqlite:
        # Eight of the shift's ten seconds on the full fleet are index rebuilds, and SQLite builds an
        # index through its page cache: 512 MB for the duration took the shift from 10.5 to 8.6 s.
        # Per connection, so it is put back below whatever happens; a pooled connection must not
        # keep it. Postgres sizes its own memory and needs nothing here.
        db.execute(sa.text("PRAGMA cache_size=-524288"))
    try:
        for table, cols, keyed in date_columns():
            names = {c.name for c in cols}

            def shift(by: int, _cols=cols, _table=table) -> dict:
                values = {c: _shifted(db, c, by) for c in _cols}
                # A Core UPDATE applies a column's onupdate default unless the column is named, and
                # the audit stamp has one; naming it keeps the real time the row was last written.
                if "last_updated" in _table.c:
                    values[_table.c.last_updated] = _table.c.last_updated
                return values

            # The indexes that carry a shifted date are what a fleet-sized UPDATE pays for; on a big
            # table they are dropped for the UPDATE and rebuilt from the model afterwards. Unique
            # indexes stay: they are the key the two-step shift below is careful about.
            big = int(db.scalar(select(func.count()).select_from(table)) or 0) >= BIG_TABLE
            rebuilt = sorted((i for i in table.indexes if big and not i.unique and names & {c.name for c in i.columns}), key=lambda i: i.name)
            for idx in rebuilt:
                idx.drop(bind=conn)
            if keyed:
                # park far out, then bring back: no intermediate day is ever shared by two rows
                db.execute(table.update().values(shift(-_PARK)))
                db.execute(table.update().values(shift(_PARK + days)))
            else:
                db.execute(table.update().values(shift(days)))
            for idx in rebuilt:
                idx.create(bind=conn)
    finally:
        if sqlite:
            db.execute(sa.text("PRAGMA cache_size=-2000"))    # SQLite's default
    db.add(WorldClock(days=days, advanced_at=datetime.now(timezone.utc), action=action))
    db.flush()
    db.expire_all()
    return state(db)

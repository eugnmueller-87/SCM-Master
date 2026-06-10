"""Database engine, session factory, and declarative base.

Every model inherits from ``Base``. ``IdMixin`` and ``TimestampMixin`` provide
the UUID primary key and created/updated audit columns that nearly every
OpenBoxes entity carried — we keep that convention because it is genuinely
useful for an audit trail.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, String, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from app.core.config import settings

# check_same_thread is only needed for SQLite; harmless to compute conditionally.
_is_sqlite = settings.database_url.startswith("sqlite")
connect_args = {"check_same_thread": False} if _is_sqlite else {}

# Connection-pool tuning matters only for a real server DB (Postgres on prod);
# SQLite uses a SingletonThreadPool/StaticPool and ignores these knobs.
#   - pool_pre_ping: validate a connection before use, so a stale/recycled
#     Postgres connection (Railway idle-times these out) is transparently
#     replaced instead of raising on the next query.
#   - pool_size/max_overflow: headroom above SQLAlchemy's tiny 5+10 default, so a
#     burst of dashboard traffic alongside a long agent run doesn't hit
#     "QueuePool limit reached" and start failing /readyz.
pool_kwargs = {} if _is_sqlite else {
    "pool_pre_ping": True,
    "pool_size": 10,
    "max_overflow": 20,
    "pool_recycle": 1800,
}
engine = create_engine(settings.database_url, connect_args=connect_args, **pool_kwargs)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    pass


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class IdMixin:
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)


class TimestampMixin:
    date_created: Mapped[datetime] = mapped_column(DateTime, default=_now)
    last_updated: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class ExternalRefMixin:
    """Identity of a row in the *system it was synced from* (SAP, Coupa, …).

    When a record originates in an upstream system rather than here, these two
    columns let us map our row back to its source-of-truth key and round-trip
    safely. ``source_system`` is the upstream (e.g. ``"coupa"``, ``"sap"``);
    ``external_ref`` is that system's own identifier (a Coupa supplier number, a
    SAP PO number). The pair is what an idempotent upsert keys on — re-importing
    the same feed updates the existing row instead of duplicating it. Both are
    nullable: records born *here* simply leave them unset.
    """

    source_system: Mapped["str | None"] = mapped_column(String(32), index=True)
    external_ref: Mapped["str | None"] = mapped_column(String(128), index=True)


def get_db():
    """FastAPI dependency: yields a session and always closes it."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

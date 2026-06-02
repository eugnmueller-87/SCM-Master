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
connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, connect_args=connect_args)
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


def get_db():
    """FastAPI dependency: yields a session and always closes it."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

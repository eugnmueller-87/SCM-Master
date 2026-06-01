"""Shared API dependencies.

``get_db`` owns the transaction boundary for one request: services only
``flush``, and this dependency commits on success or rolls back on any error.
That keeps multiple service calls within a request atomic.
"""
from __future__ import annotations

from typing import Iterator

from sqlalchemy.orm import Session

from app.core.db import SessionLocal


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

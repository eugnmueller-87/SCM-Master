"""Shared schema base classes.

``ReadBase`` carries the fields every persisted entity exposes — the UUID and
the audit timestamps from ``IdMixin`` / ``TimestampMixin`` — and turns on
``from_attributes`` so a schema can be built directly from an ORM instance.
"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class ReadBase(BaseModel):
    """Common fields returned for any stored entity."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    date_created: datetime
    last_updated: datetime

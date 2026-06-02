"""Users and roles.

A minimal but real auth model: a user has an email, a bcrypt password hash, and
a single role. Roles gate which operations a request may perform (see
``app.core.security`` and the ``require_role`` dependency). ADMIN is a superset
that passes every role check.
"""
from __future__ import annotations

import enum

from sqlalchemy import Boolean, Enum as SAEnum, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, IdMixin, TimestampMixin


class Role(str, enum.Enum):
    ADMIN = "ADMIN"               # everything
    PROCUREMENT = "PROCUREMENT"   # catalog, orders, approvals, re-sourcing
    WAREHOUSE = "WAREHOUSE"       # receiving, moves, storage transitions
    DATACENTER = "DATACENTER"     # deploy / maintenance / decommission
    VIEWER = "VIEWER"             # read-only


class User(IdMixin, TimestampMixin, Base):
    __tablename__ = "app_user"

    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(255))
    hashed_password: Mapped[str] = mapped_column(String(255))
    role: Mapped[Role] = mapped_column(SAEnum(Role), default=Role.VIEWER)
    active: Mapped[bool] = mapped_column(Boolean, default=True)

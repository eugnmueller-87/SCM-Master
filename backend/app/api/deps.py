"""Shared API dependencies.

``get_db`` owns the transaction boundary for one request: services only
``flush``, and this dependency commits on success or rolls back on any error.
That keeps multiple service calls within a request atomic.
"""
from __future__ import annotations

from typing import Iterator

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session

from app.core.db import SessionLocal
from app.core.security import decode_access_token
from app.models.auth import Role, User
from app.services.auth import user_service

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login")


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


def get_current_user(token: str = Depends(oauth2_scheme),
                     db: Session = Depends(get_db)) -> User:
    creds_exc = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = decode_access_token(token)
        email = payload.get("sub")
        if not email:
            raise creds_exc
    except jwt.PyJWTError:
        raise creds_exc

    user = user_service.get_by_email(db, email)
    if user is None or not user.active:
        raise creds_exc
    return user


def require_role(*roles: Role):
    """Dependency factory: allow only the given roles (ADMIN always passes)."""
    allowed = set(roles)

    def _dep(user: User = Depends(get_current_user)) -> User:
        if user.role is not Role.ADMIN and user.role not in allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Requires role: {', '.join(sorted(r.value for r in allowed))}",
            )
        return user

    return _dep

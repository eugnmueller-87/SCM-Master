"""Auth service: create users and authenticate credentials."""
from __future__ import annotations

from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.security import hash_password, verify_password
from app.models.auth import Role, User
from app.services.crud import CRUDService
from app.services.exceptions import ConflictError


class UserService(CRUDService[User]):
    def __init__(self):
        super().__init__(User)

    def create_user(self, db: Session, *, email: str, full_name: str,
                    password: str, role: Role) -> User:
        if db.scalar(select(User).where(User.email == email)):
            raise ConflictError(f"User {email!r} already exists")
        user = User(
            email=email, full_name=full_name,
            hashed_password=hash_password(password), role=role,
        )
        db.add(user)
        db.flush()
        return user

    def authenticate(self, db: Session, email: str, password: str) -> Optional[User]:
        user = db.scalar(select(User).where(User.email == email))
        if user is None or not user.active:
            return None
        if not verify_password(password, user.hashed_password):
            return None
        return user

    def get_by_email(self, db: Session, email: str) -> Optional[User]:
        return db.scalar(select(User).where(User.email == email))


user_service = UserService()


def ensure_user(db: Session, *, email: str, full_name: str,
                password: str, role: Role) -> bool:
    """Create the user if it doesn't already exist. Returns True if created.

    Idempotent and safe to call on every boot — used to guarantee a login
    account exists even when the demo seed is gated off.
    """
    if user_service.get_by_email(db, email) is not None:
        return False
    user_service.create_user(db, email=email, full_name=full_name,
                             password=password, role=role)
    return True


def bootstrap_users() -> None:
    """Guarantee a usable login on every boot, independent of the demo seed.

    The demo dataset is gated behind SEED_DEMO=1, so a plain production boot
    creates no users at all — which would lock everyone out. This ensures an
    ADMIN (and, unless disabled, a read-only guest) always exist.

    All credentials are env-overridable; the defaults are the demo ones:
      ADMIN_EMAIL / ADMIN_PASSWORD / ADMIN_NAME
      GUEST_ENABLED ("1" default; set "0" in real prod) / GUEST_PASSWORD
    """
    import os

    from app.core.db import SessionLocal

    db = SessionLocal()
    try:
        created = []
        if ensure_user(
            db,
            email=os.getenv("ADMIN_EMAIL", "admin@example.com"),
            full_name=os.getenv("ADMIN_NAME", "Administrator"),
            password=os.getenv("ADMIN_PASSWORD", "admin"),
            role=Role.ADMIN,
        ):
            created.append("admin")
        if os.getenv("GUEST_ENABLED", "1") == "1" and ensure_user(
            db,
            email=os.getenv("GUEST_EMAIL", "guest@example.com"),
            full_name="Demo Guest",
            password=os.getenv("GUEST_PASSWORD", "guest"),
            role=Role.VIEWER,
        ):
            created.append("guest")
        db.commit()
        if created:
            print(f"Bootstrap users created: {', '.join(created)}")
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    # Run as a boot step: python -m app.services.auth
    bootstrap_users()

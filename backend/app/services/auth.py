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

    DEMO (SCM_ENV != prod): ensures admin/admin + a read-only guest so the
    public demo always has a one-click login.

    PRODUCTION (SCM_ENV=prod): forge-locked.
      - NEVER creates the demo guest account.
      - REFUSES the weak default admin password — a real ADMIN_PASSWORD must be
        provided via the environment, or no admin is created (boot still serves;
        you provision the admin deliberately).

    Credentials are env-overridable: ADMIN_EMAIL / ADMIN_PASSWORD / ADMIN_NAME,
    and (demo only) GUEST_ENABLED / GUEST_EMAIL / GUEST_PASSWORD.
    """
    import os

    from app.core.config import is_production
    from app.core.db import SessionLocal

    prod = is_production()
    admin_pw = os.getenv("ADMIN_PASSWORD", "admin")

    db = SessionLocal()
    try:
        created = []
        # In prod, refuse the demo default password — don't create a weak admin.
        if prod and admin_pw == "admin":
            print("PROD: ADMIN_PASSWORD not set (or still 'admin') — NOT creating "
                  "a default admin. Set ADMIN_PASSWORD and redeploy to provision one.")
        elif ensure_user(
            db,
            email=os.getenv("ADMIN_EMAIL", "admin@example.com"),
            full_name=os.getenv("ADMIN_NAME", "Administrator"),
            password=admin_pw,
            role=Role.ADMIN,
        ):
            created.append("admin")

        # The guest account is DEMO-ONLY — never in production.
        guest_on = (not prod) and os.getenv("GUEST_ENABLED", "1") == "1"
        if guest_on and ensure_user(
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

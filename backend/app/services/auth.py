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

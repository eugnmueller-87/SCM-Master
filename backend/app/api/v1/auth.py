"""Auth routes: login (OAuth2 password flow), register (admin-only), and /me."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db, require_role
from app.core.security import create_access_token
from app.models.auth import Role, User
from app.schemas.auth import Token, UserCreate, UserRead
from app.services.auth import user_service

router = APIRouter(tags=["auth"], prefix="/auth")


@router.post("/login", response_model=Token)
def login(form: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    user = user_service.authenticate(db, form.username, form.password)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = create_access_token(subject=user.email, role=user.role.value)
    return Token(access_token=token)


@router.post("/register", response_model=UserRead, status_code=status.HTTP_201_CREATED)
def register(payload: UserCreate, db: Session = Depends(get_db),
             _admin: User = Depends(require_role(Role.ADMIN))):
    """Create a user. Admin-only — the first admin is created by the seed."""
    return user_service.create_user(
        db, email=payload.email, full_name=payload.full_name,
        password=payload.password, role=payload.role,
    )


@router.get("/me", response_model=UserRead)
def me(user: User = Depends(get_current_user)):
    return user

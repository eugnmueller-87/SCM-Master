"""Auth schemas: registration, token, current user."""
from __future__ import annotations

from pydantic import BaseModel, EmailStr

from app.models.auth import Role
from app.schemas.base import ReadBase


class UserCreate(BaseModel):
    email: EmailStr
    full_name: str
    password: str
    role: Role = Role.VIEWER


class UserRead(ReadBase):
    email: EmailStr
    full_name: str
    role: Role
    active: bool


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"

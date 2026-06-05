"""A small generic CRUD service over a single SQLAlchemy model.

Domain services subclass this for the repetitive get/list/create/update/delete
plumbing, and add business rules on top. It does NOT commit — the caller (the
route, via the session dependency) owns the transaction boundary, so multiple
service calls can compose into one atomic request.
"""
from __future__ import annotations

from typing import Generic, Optional, Sequence, Type, TypeVar

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.db import Base
from app.services.exceptions import NotFoundError

ModelT = TypeVar("ModelT", bound=Base)


class CRUDService(Generic[ModelT]):
    def __init__(self, model: Type[ModelT]):
        self.model = model

    def get(self, db: Session, id: str) -> Optional[ModelT]:
        return db.get(self.model, id)

    def get_or_404(self, db: Session, id: str) -> ModelT:
        obj = self.get(db, id)
        if obj is None:
            raise NotFoundError(f"{self.model.__name__} {id!r} not found")
        return obj

    def list(self, db: Session, *, skip: int = 0, limit: int = 100) -> Sequence[ModelT]:
        stmt = select(self.model).offset(skip).limit(limit)
        return db.scalars(stmt).all()

    def create(self, db: Session, data: dict) -> ModelT:
        obj = self.model(**data)
        db.add(obj)
        db.flush()  # populate id/defaults without ending the transaction
        return obj

    def update(self, db: Session, obj: ModelT, data: dict) -> ModelT:
        for field, value in data.items():
            setattr(obj, field, value)
        db.flush()
        return obj

    def delete(self, db: Session, obj: ModelT) -> None:
        db.delete(obj)
        db.flush()

    # --- integration sync support ----------------------------------------
    # Only meaningful for models carrying ExternalRefMixin (Organization,
    # Product, PurchaseOrder). The (source_system, external_ref) pair is the
    # upstream system's own key, so syncing the same feed twice updates the
    # existing row instead of duplicating it.

    def get_by_external_ref(
        self, db: Session, *, source_system: str, external_ref: str
    ) -> Optional[ModelT]:
        stmt = select(self.model).where(
            self.model.source_system == source_system,
            self.model.external_ref == external_ref,
        )
        return db.scalar(stmt)

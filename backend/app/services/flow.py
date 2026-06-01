"""Flow services: Location.

Rules: unique location code, and a parent (if given) must exist. The Receipt
and Asset write-paths — the lifecycle engine — arrive in Phase 2.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.flow import Location
from app.services.crud import CRUDService
from app.services.exceptions import ConflictError, NotFoundError


class LocationService(CRUDService[Location]):
    def __init__(self):
        super().__init__(Location)

    def create(self, db: Session, data: dict) -> Location:
        code = data["code"]
        if db.scalar(select(Location).where(Location.code == code)):
            raise ConflictError(f"Location code {code!r} already exists")
        parent_id = data.get("parent_id")
        if parent_id and db.get(Location, parent_id) is None:
            raise NotFoundError(f"Location {parent_id!r} not found")
        return super().create(db, data)


location_service = LocationService()

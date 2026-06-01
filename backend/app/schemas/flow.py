"""Flow schemas: Location (the Receipt/Asset write-paths land in Phase 2)."""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

from app.models.flow import LocationType
from app.schemas.base import ReadBase


class LocationCreate(BaseModel):
    code: str
    name: str
    location_type: LocationType
    parent_id: Optional[str] = None
    capacity: Optional[int] = Field(default=None, ge=0)


class LocationUpdate(BaseModel):
    code: Optional[str] = None
    name: Optional[str] = None
    location_type: Optional[LocationType] = None
    parent_id: Optional[str] = None
    capacity: Optional[int] = Field(default=None, ge=0)


class LocationRead(ReadBase):
    code: str
    name: str
    location_type: LocationType
    parent_id: Optional[str]
    capacity: Optional[int]

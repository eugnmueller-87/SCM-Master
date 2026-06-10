"""Flow routes: locations (warehouse, datacenter, racks, ...).

Receiving and the asset lifecycle endpoints arrive in Phase 2.
"""
from __future__ import annotations

from typing import List

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
from app.schemas.flow import LocationCreate, LocationRead, LocationUpdate
from app.services.flow import location_service

router = APIRouter(tags=["flow"], dependencies=[Depends(get_current_user)])


@router.post("/locations", response_model=LocationRead, status_code=status.HTTP_201_CREATED)
def create_location(payload: LocationCreate, db: Session = Depends(get_db)):
    return location_service.create(db, payload.model_dump())


@router.get("/locations", response_model=List[LocationRead])
def list_locations(skip: int = 0, limit: int = 100, db: Session = Depends(get_db)):
    return location_service.list(db, skip=skip, limit=limit)


@router.get("/locations/{location_id}", response_model=LocationRead)
def get_location(location_id: str, db: Session = Depends(get_db)):
    return location_service.get_or_404(db, location_id)


@router.patch("/locations/{location_id}", response_model=LocationRead)
def update_location(location_id: str, payload: LocationUpdate, db: Session = Depends(get_db)):
    obj = location_service.get_or_404(db, location_id)
    return location_service.update(db, obj, payload.model_dump(exclude_unset=True))

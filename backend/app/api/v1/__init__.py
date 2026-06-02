"""API v1 — aggregates every domain router under one router that main.py mounts
at the ``/api/v1`` prefix."""
from __future__ import annotations

from fastapi import APIRouter

from app.api.v1.asset import router as asset_router
from app.api.v1.catalog import router as catalog_router
from app.api.v1.flow import router as flow_router
from app.api.v1.planning import router as planning_router
from app.api.v1.procurement import router as procurement_router
from app.api.v1.sourcing import router as sourcing_router

api_router = APIRouter()
api_router.include_router(catalog_router)
api_router.include_router(flow_router)
api_router.include_router(procurement_router)
api_router.include_router(asset_router)
api_router.include_router(sourcing_router)
api_router.include_router(planning_router)

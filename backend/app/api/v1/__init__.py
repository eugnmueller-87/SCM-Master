"""API v1 — aggregates every domain router under one router that main.py mounts
at the ``/api/v1`` prefix."""
from __future__ import annotations

from fastapi import APIRouter

from app.api.v1.agent import router as agent_router
from app.api.v1.asset import router as asset_router
from app.api.v1.auth import router as auth_router
from app.api.v1.catalog import router as catalog_router
from app.api.v1.costing import router as costing_router
from app.api.v1.exports import router as exports_router
from app.api.v1.flow import router as flow_router
from app.api.v1.integrations import router as integrations_router
from app.api.v1.planning import router as planning_router
from app.api.v1.procurement import router as procurement_router
from app.api.v1.requisitions import router as requisitions_router
from app.api.v1.sourcing import router as sourcing_router
from app.api.v1.tco import router as tco_router
from app.api.v1.tracking import router as tracking_router

api_router = APIRouter()
api_router.include_router(auth_router)
api_router.include_router(catalog_router)
api_router.include_router(flow_router)
api_router.include_router(procurement_router)
api_router.include_router(asset_router)
api_router.include_router(sourcing_router)
api_router.include_router(planning_router)
api_router.include_router(agent_router)
api_router.include_router(tracking_router)
api_router.include_router(integrations_router)
api_router.include_router(exports_router)
api_router.include_router(requisitions_router)
api_router.include_router(costing_router)
api_router.include_router(tco_router)

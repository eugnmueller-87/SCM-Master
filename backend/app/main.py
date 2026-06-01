"""FastAPI entrypoint.

The schema is now owned by Alembic migrations (run ``alembic upgrade head``),
not created on startup — so dev and prod share one source of truth. This module
builds the app, mounts the versioned API routers, and keeps two sanity-check
endpoints.

Run from the backend/ directory:
    .venv\\Scripts\\alembic upgrade head
    .venv\\Scripts\\uvicorn app.main:app --reload
"""
from __future__ import annotations

from fastapi import FastAPI

from app.api.errors import register_error_handlers
from app.api.v1 import api_router
from app.core.config import settings
from app.core.db import Base
import app.models  # noqa: F401  -- registers all tables on Base

app = FastAPI(title=settings.app_name)

register_error_handlers(app)
app.include_router(api_router, prefix="/api/v1")


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "app": settings.app_name}


@app.get("/schema")
def schema() -> dict:
    """List the tables the domain model defines — a sanity check."""
    return {"tables": sorted(Base.metadata.tables.keys())}

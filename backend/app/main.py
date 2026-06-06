"""FastAPI entrypoint.

The schema is owned by Alembic migrations (run ``alembic upgrade head``), not
created on startup — so dev and prod share one source of truth. This module
configures structured logging, request-id middleware, builds the app, mounts
the versioned API routers, and exposes liveness/readiness probes.

Run from the backend/ directory:
    .venv\\Scripts\\alembic upgrade head
    .venv\\Scripts\\uvicorn app.main:app --reload
"""
from __future__ import annotations

from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.orm import Session

import app.models  # noqa: F401  -- registers all tables on Base
from app.api.deps import get_db, require_role
from app.api.errors import register_error_handlers
from app.api.v1 import api_router
from app.core.config import settings, validate_production
from app.core.db import Base
from app.core.observability import RequestContextMiddleware, configure_logging
from app.models.auth import Role, User

configure_logging()
validate_production()  # fail closed on insecure config before serving a single request

app = FastAPI(title=settings.app_name)

app.add_middleware(RequestContextMiddleware)
register_error_handlers(app)
app.include_router(api_router, prefix="/api/v1")


@app.get("/health")
def health() -> dict:
    """Liveness: the process is up and serving."""
    return {"status": "ok", "app": settings.app_name}


@app.get("/readyz")
def readyz(db: Session = Depends(get_db)) -> dict:
    """Readiness: the process can reach its dependencies (the database)."""
    db.execute(text("SELECT 1"))
    return {"status": "ready"}


@app.get("/schema")
def schema(_admin: User = Depends(require_role(Role.ADMIN))) -> dict:
    """List the tables the domain model defines — a sanity check. Admin-only:
    the table inventory is internal detail, not for anonymous callers."""
    return {"tables": sorted(Base.metadata.tables.keys())}


# Serve the static operations UI at / (mounted last so it never shadows the API
# or the probes above). Skipped gracefully if the frontend dir isn't present.
_frontend = Path(__file__).resolve().parents[2] / "frontend"
if _frontend.is_dir():
    app.mount("/", StaticFiles(directory=str(_frontend), html=True), name="frontend")

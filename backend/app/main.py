"""FastAPI entrypoint.

For now it creates the schema on startup (fine for early dev; a migration tool
like Alembic comes later) and exposes a health check plus a quick model-count
endpoint so we can confirm the schema is wired correctly.

Run from the backend/ directory:
    .venv\\Scripts\\uvicorn app.main:app --reload
"""
from __future__ import annotations

from fastapi import FastAPI

from app.core.config import settings
from app.core.db import Base, engine
import app.models  # noqa: F401  -- registers all tables on Base

app = FastAPI(title=settings.app_name)


@app.on_event("startup")
def on_startup() -> None:
    Base.metadata.create_all(bind=engine)


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "app": settings.app_name}


@app.get("/schema")
def schema() -> dict:
    """List the tables the domain model defines — a sanity check."""
    return {"tables": sorted(Base.metadata.tables.keys())}

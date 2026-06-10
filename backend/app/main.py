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

import os
import re
import time
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.orm import Session

import app.models  # noqa: F401  -- registers all tables on Base
from app.api.deps import get_db, require_role
from app.api.errors import register_error_handlers
from app.api.v1 import api_router
from app.core.config import announce_startup, is_production, settings, validate_production
from app.core.db import Base
from app.core.observability import RequestContextMiddleware, configure_logging
from app.models.auth import Role, User

configure_logging()
validate_production()  # fail closed on insecure config before serving a single request
announce_startup()     # log DEMO/PROD mode + DB; prod refuses non-persistent storage

app = FastAPI(title=settings.app_name)

app.add_middleware(RequestContextMiddleware)
register_error_handlers(app)
app.include_router(api_router, prefix="/api/v1")


@app.get("/health")
def health() -> dict:
    """Liveness: the process is up and serving. ``is_production`` lets the login
    page hide demo-only hints (guest button, sample credentials) on prod."""
    return {"status": "ok", "app": settings.app_name, "is_production": is_production()}


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


# --- frontend: cache-busted index + static assets ----------------------------
#
# The browser caches app.js / inventory.js / etc. With plain <script src="x.js">
# tags a deploy can leave a browser running a STALE mix — a fresh app.js next to
# a cached inventory.js — which presents as a half-loaded page (an "undefined"
# crumb, a view stuck on "Loading…"). To prevent it we stamp every asset URL in
# index.html with a per-deploy build token: the HTML itself is served no-cache
# (always re-fetched), and each asset is fetched as ``app.js?v=<token>`` so the
# browser cache invalidates the instant the token changes — i.e. on every deploy
# — and otherwise caches hard. No build step, no manual version bump.
_frontend = Path(__file__).resolve().parents[2] / "frontend"


def _build_token() -> str:
    """A value that is stable within one deploy and changes across deploys.

    Prefers an explicit env stamp (set by the platform — Railway injects the
    commit SHA), then the git SHA read straight from ``.git`` (no subprocess),
    then the process start time as a last resort. Computed once at import.
    """
    env = os.getenv("RAILWAY_GIT_COMMIT_SHA") or os.getenv("APP_BUILD")
    if env:
        return env[:12]
    sha = _git_head_sha(Path(__file__).resolve().parents[2])
    if sha:
        return sha[:12]
    return str(int(time.time()))


def _git_head_sha(repo_root: Path) -> str | None:
    """Resolve HEAD to a commit SHA by reading the git plumbing files directly.

    Avoids spawning a subprocess (and the security-scanner noise that comes with
    it). Handles both a detached HEAD (a raw SHA in .git/HEAD) and the normal
    ``ref: refs/heads/<branch>`` indirection, including a packed ref. Returns
    None if anything is missing — the caller falls back to a time stamp.
    """
    try:
        head = (repo_root / ".git" / "HEAD").read_text(encoding="utf-8").strip()
        if not head.startswith("ref:"):
            return head or None
        ref = head[4:].strip()
        loose = repo_root / ".git" / ref
        if loose.is_file():
            return loose.read_text(encoding="utf-8").strip() or None
        packed = repo_root / ".git" / "packed-refs"
        if packed.is_file():
            for line in packed.read_text(encoding="utf-8").splitlines():
                if line.endswith(" " + ref):
                    return line.split(" ", 1)[0].strip() or None
    except OSError:
        return None
    return None


_BUILD = _build_token()
# Only rewrite our own first-party assets, never an absolute/external URL.
_ASSET_REF = re.compile(r'(src|href)="(?!https?://|//)([^"?]+\.(?:js|css))"')


def _index_html() -> str:
    raw = (_frontend / "index.html").read_text(encoding="utf-8")
    return _ASSET_REF.sub(rf'\1="\2?v={_BUILD}"', raw)


if _frontend.is_dir() and (_frontend / "index.html").is_file():
    _INDEX = _index_html()

    @app.get("/", include_in_schema=False)
    @app.get("/index.html", include_in_schema=False)
    def index() -> HTMLResponse:
        # no-store on the shell so a new deploy's version stamps are seen at once;
        # the stamped assets below are what actually get cached.
        return HTMLResponse(_INDEX, headers={"Cache-Control": "no-store"})

    # Static assets (the stamped app.js?v=… resolves to app.js here). Mounted
    # last so it never shadows the API, the probes, or the index route above.
    app.mount("/", StaticFiles(directory=str(_frontend), html=True), name="frontend")
elif _frontend.is_dir():
    app.mount("/", StaticFiles(directory=str(_frontend), html=True), name="frontend")

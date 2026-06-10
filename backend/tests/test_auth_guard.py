"""Authentication invariant: every API route is protected unless explicitly public.

This is the real deliverable of the auth-gap remediation — not 44 one-off tests,
but one invariant driven off the live route table. It enumerates the app's routes
at test time and asserts that any route OUTSIDE the public allowlist returns 401
when called with no token. It goes red on an unguarded route (proving a hole) and
prevents any future route from regressing into being anonymously reachable.

A companion test asserts a VIEWER-role token still gets 403 on the role-gated
write routes, proving the role overrides survived the baseline-auth change.
"""
from __future__ import annotations

from app.main import app as fastapi_app
from app.models.auth import Role

# Routes that MUST stay open to an anonymous caller. Anything not here must 401.
PUBLIC_ALLOWLIST = {
    ("GET", "/"),
    ("GET", "/index.html"),
    ("GET", "/health"),
    ("GET", "/readyz"),
    ("POST", "/api/v1/auth/login"),
}

# FastAPI's auto-generated API docs. These are anonymously reachable and expose
# the OpenAPI schema — a real but SEPARATE finding, out of scope for the domain-
# router auth fix (they're served by the app, not a domain router). Allowlisted
# here so this invariant stays focused on the /api/v1 surface; locking docs down
# (or disabling them in prod) is a deferred follow-up, not this change.
_DOCS_DEFERRED = {
    ("GET", "/docs"),
    ("GET", "/redoc"),
    ("GET", "/openapi.json"),
    ("GET", "/docs/oauth2-redirect"),
}

# Methods we actively probe (skip HEAD/OPTIONS which FastAPI auto-adds).
_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}


def _fill_path(path: str) -> str:
    """Replace ``{param}`` placeholders with a dummy value so the route matches.

    We only care about the AUTH outcome (401 must fire before any handler logic),
    so any non-empty value that routes correctly is fine.
    """
    out = []
    for seg in path.split("/"):
        out.append("test-id" if seg.startswith("{") and seg.endswith("}") else seg)
    return "/".join(out)


def _api_routes():
    """(method, path, filled_path) for every concrete API route, minus mounts."""
    for r in fastapi_app.routes:
        path = getattr(r, "path", None)
        methods = getattr(r, "methods", None)
        if not path or not methods:
            continue  # static mount / non-APIRoute
        for m in sorted(methods & _METHODS):
            yield m, path, _fill_path(path)


def test_every_nonpublic_route_requires_auth(client):
    """No-token call to any non-allowlisted route must return 401 (never 200/201/...)."""
    anon = client.anon()
    leaks = []
    for method, path, filled in _api_routes():
        if (method, path) in PUBLIC_ALLOWLIST or (method, path) in _DOCS_DEFERRED:
            continue
        resp = anon.request(method, filled)
        # 401 = correctly rejected. 404/405/422 would mean the auth check never ran
        # (route/validation answered first) — that is also a leak for this invariant,
        # because a valid-but-unauthenticated call must be stopped at the door.
        if resp.status_code != 401:
            leaks.append(f"{method} {path} -> {resp.status_code}")
    assert not leaks, (
        "These routes are reachable without authentication (expected 401):\n  "
        + "\n  ".join(sorted(leaks))
    )


def test_public_routes_stay_open(client):
    """The allowlist must NOT be locked out by the baseline-auth change."""
    anon = client.anon()
    for method, path in PUBLIC_ALLOWLIST:
        resp = anon.request(method, path)
        assert resp.status_code != 401, f"{method} {path} should be public but got 401"


# A representative set of role-gated WRITE routes (must reject a VIEWER with 403,
# proving the role overrides were not flattened by adding baseline auth).
_VIEWER_FORBIDDEN = [
    ("POST", "/api/v1/purchase-orders"),
    ("POST", "/api/v1/requisitions/run"),
    ("POST", "/api/v1/commodities"),
    ("PUT", "/api/v1/products/test-id/bom"),
    ("POST", "/api/v1/integrations/coupa/import"),
]


def test_viewer_is_forbidden_on_role_gated_writes(client):
    """A VIEWER token is authenticated (not 401) but lacks the role (403)."""
    viewer = client.as_role(Role.VIEWER)
    for method, path in _VIEWER_FORBIDDEN:
        resp = viewer.request(method, path)
        assert resp.status_code == 403, (
            f"{method} {path} should 403 for VIEWER, got {resp.status_code}"
        )

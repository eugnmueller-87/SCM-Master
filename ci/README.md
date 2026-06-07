# CI workflow

The GitHub Actions pipeline now lives at
[`.github/workflows/ci.yml`](../.github/workflows/ci.yml), where Actions will
actually run it. It has two jobs:

- **test** (SQLite): ruff lint → migrate-check (autogenerate must be a no-op, so
  models and migrations can't drift) → pytest (the suite is bound to in-memory
  SQLite via `tests/conftest.py`).
- **migrate-smoke-postgres**: spins up a Postgres 16 service, runs
  `alembic upgrade head` to prove the migrations apply cleanly on Postgres, then
  runs a focused smoke test (`tests/test_pg_smoke.py`: readiness + a CRUD
  round-trip through the real Postgres engine). This is intentionally a smoke
  test, not the full suite — conftest pins the suite to SQLite.

> **Note on pushing workflow changes:** adding or updating files under
> `.github/workflows/` requires a credential with the `workflow` OAuth scope.
> If a push is rejected for that reason, run `gh auth refresh -s workflow`
> first.

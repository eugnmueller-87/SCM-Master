# CI workflow

The GitHub Actions pipeline now lives at
[`.github/workflows/ci.yml`](../.github/workflows/ci.yml), where Actions will
actually run it. It has two jobs:

- **test** (SQLite): ruff lint → migrate-check (autogenerate must be a no-op, so
  models and migrations can't drift) → pytest.
- **test-postgres**: spins up a Postgres 16 service and runs migrations + the
  full suite against it, catching any SQLite/Postgres dialect drift.

> **Note on pushing workflow changes:** adding or updating files under
> `.github/workflows/` requires a credential with the `workflow` OAuth scope.
> If a push is rejected for that reason, run `gh auth refresh -s workflow`
> first.

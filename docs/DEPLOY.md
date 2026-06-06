# Deploy — Demo & Production (Railway)

Two **fully separate, independently wired** stacks. They share **no** database
and never affect each other. Each stack is two services:

```
            ┌─────────────────────────┐         ┌──────────────────────────┐
  DEMO      │ scm-master (demo)        │◀──HTTP──│ SCM Analytics (demo)     │
  (public)  │  → demo Postgres, seeded │  proxy  │  cockpit, reads demo API │
            └─────────────────────────┘         └──────────────────────────┘

            ┌─────────────────────────┐         ┌──────────────────────────┐
  PROD      │ scm-master (prod)        │◀──HTTP──│ SCM Analytics (prod)     │
  (real)    │  → prod Postgres, real   │  proxy  │  cockpit, reads prod API │
            └─────────────────────────┘         └──────────────────────────┘
```

- **scm-master** — this repo. FastAPI app + operations UI. Persists to Postgres.
- **SCM Analytics** — the cockpit, repo `eugnmueller-87/SCM-POWER-BI` (`deploy/`).
  It is a thin **server-side proxy**: it logs into a scm-master API, pulls the
  analytics endpoints, caches them, and serves `/api/data` to the dashboard.
  "Wired to demo / prod" = its `API_BASE` points at that environment's
  scm-master. No database of its own.
- **Copilot** — one `ANTHROPIC_API_KEY` is fine for both; each environment's
  copilot simply runs against its own data. No per-env key needed.

The persistent-Postgres bits below depend on three fixes already in the repo:
the psycopg driver ships in `requirements.txt`, `DATABASE_URL` auto-pins the
`+psycopg` driver (paste the provider URL as-is), and admin+guest are
bootstrapped on every boot so login always works.

---

## scm-master — environment variables

| Variable | Demo | Production |
| --- | --- | --- |
| `DATABASE_URL` | `${{ Postgres-Demo.DATABASE_URL }}` | `${{ Postgres-Prod.DATABASE_URL }}` |
| `SECRET_KEY` | any long string | **strong, ≥32 chars** (guard enforces it) |
| `SCM_ENV` | unset (`dev`) | `prod` |
| `SEED_DEMO` | `1` (seed the synthetic dataset) | **unset** (no seed — real data only) |
| `ANTHROPIC_API_KEY` | your key | same key |
| `SCM_ANALYTICS_URL` | demo cockpit URL | prod cockpit URL |
| `PORT` | injected by Railway | injected by Railway |

Service **Settings → Root Directory = `backend`** (Dockerfile `COPY` paths are
relative to it). Builder: Dockerfile.

Boot sequence (every deploy): `alembic upgrade head` → `python -m
app.services.auth` (ensures admin+guest) → demo seed **iff** `SEED_DEMO=1` →
`uvicorn`. On persistent Postgres the data now **survives redeploys**.

> `SCM_ANALYTICS_URL` is read by the operations UI to point its sidebar **SCM
> Analytics** link at the matching cockpit. If unset, it defaults to the demo
> cockpit. (Surface it to the frontend via a `<meta name="scm-analytics-url">`
> tag or `window.SCM_ANALYTICS_URL`.)

## SCM Analytics cockpit (`SCM-POWER-BI/deploy`) — environment variables

| Variable | Demo | Production |
| --- | --- | --- |
| `API_BASE` | demo scm-master URL | prod scm-master URL |
| `API_USER` | `guest@example.com` | a read-only account |
| `API_PASS` | `guest` | that account's password |
| `REFRESH_SECONDS` | `300` | `300` |
| `PORT` | injected by Railway | injected by Railway |

The cockpit only **reads**, so a read-only **VIEWER** account is correct — it
cannot mutate data. All endpoints it calls (spend, inventory, exports,
agent/insights) are reachable by VIEWER.

---

## Standing up a stack (demo, then repeat for prod)

1. **Add a Postgres** plugin in the Railway project (`Postgres-Demo`).
2. **scm-master service** → set the variables from the table above → **Deploy**.
   First boot migrates, bootstraps admin+guest, and (demo) seeds ~6 products /
   ~787 assets / 18 months of history.
3. **Cockpit service** → set `API_BASE` to the scm-master URL from step 2,
   `API_USER=guest@example.com`, `API_PASS=guest` → **Deploy**. Within one
   refresh interval `/api/data` fills and the dashboard is live.
4. Verify:
   - scm-master `GET /health` → 200, `GET /readyz` → 200.
   - Log in at the scm-master URL (`admin`/`admin`, or **Explore as guest**).
   - Sidebar **SCM Analytics** link opens the cockpit.
   - Cockpit `GET /api/data` → non-empty `spend_by_category`.

For **production**, repeat with `Postgres-Prod`, `SCM_ENV=prod`, a strong
`SECRET_KEY`, and **no** `SEED_DEMO`. The config guard refuses to boot prod with
an insecure/short key — that's intended.

## Why this replaces the old SQLite-demo model

Earlier the demo ran on an in-container SQLite file that reset on every redeploy
(why data kept vanishing, and why the cockpit went blank — it proxies that same
API). Persistent Postgres per environment fixes both, and keeps demo and prod
isolated.

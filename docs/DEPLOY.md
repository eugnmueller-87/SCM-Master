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
| `SCM_SCENARIO` | unset (`daas` — the device-as-a-service fleet: buy, rent, take back, refurbish, sell) or `datacenter` for the old rack operation | unset (real data decides what the console shows) |
| `DAAS_SCALE` | unset (`1.0` — 300,000 rented + 100,000 in the warehouse, the full instruction); `0.1` for a tenth | unset |
| `SCM_RESET` | unset; `1` rebuilds the dataset on the next boot even when it already matches (use after changing `DAAS_SCALE`) | never set it |
| `ANTHROPIC_API_KEY` | your key | same key |
| `SCM_ANALYTICS_URL` | demo cockpit URL | prod cockpit URL |
| `PORT` | injected by Railway | injected by Railway |

Service **Settings → Root Directory = `backend`** (Dockerfile `COPY` paths are
relative to it). Builder: Dockerfile.

**Switching the demo's dataset: nothing to do.** The boot compares what the database
holds with what this service is supposed to show and replaces it when they differ. The
dataset in a database is read from the data itself, never from a flag — a database with
rented devices is a DaaS fleet, one with deployed assets and no rentals is the datacenter
operation. So a deploy of this code onto the existing demo Postgres empties the
operational tables (logins are kept), seeds the 400,000-device fleet and measures the
KPIs. `SCM_SCENARIO=datacenter` brings the rack operation back the same way.

*Why this exists:* on 22.09.2026 the console had been rebuilt for the fleet, the code was
deployed, and the screens still showed racks and EPYC CPUs — both seeders bail out on a
populated catalog and nothing ever removed what was there. A demo that cannot change its
own dataset silently shows last month's story. See `backend/app/seed_reset.py`.

**First boot after the switch takes a few minutes.** Seeding 431,200 serials and about 493,000
rental contracts is about a minute of bulk inserts locally, longer against a hosted
Postgres, and the first KPI measurement adds roughly another minute. It happens **once**:
every later deploy finds the dataset it wants and comes up immediately. Nothing is
reseeded on an ordinary redeploy, so the data survives — that is the point of persistent
Postgres. `DAAS_SCALE=0.1` gives the same shape at a tenth of the size if a fast boot
matters more than the real number.

**Memory: the boot is a chain of separate processes, on purpose.** Seeding 431,200
serials peaks around 170 MB and the KPI measurement peaks around 140 MB. Run in one
process those peaks add, because a Python process keeps the memory arenas it has grown
rather than handing them straight back to the operating system. On 23.09.2026 the demo
container was killed for running out of memory on the boot that seeded the fleet and
then measured it in the same process. The boot command therefore runs each step on its
own (`alembic` then `auth` then `seed_demo` then `seed_history` then `seed_kpis` then
`uvicorn`), so the peak is the largest single step, not the sum, and the operating
system reclaims everything in between.

If a container still runs out of memory, the dial is `DAAS_SCALE`: `0.25` gives the same
shape at a quarter of the size (75,000 rented, 25,000 in the warehouse) and roughly a
quarter of the seeding peak. Raising the container's memory is the other way, and the
better one if the full 400,000 is the point of the demo.

**KPIs are measured once a day.** Thirty-two reads over a 400,000-device fleet take about
40 seconds; running them on every page load would make the tab unusable and would not
change a number, because each KPI is defined over a day. The boot takes the day's
measurement, the tab serves it, and **Measure again** on the tab forces a new one.

Boot sequence (every deploy): `alembic upgrade head` → `python -m
app.services.auth` (ensures admin+guest) → demo seed **iff** `SEED_DEMO=1` → `python -m
app.seed_kpis` (today's measurement, skipped when it is already taken) → `uvicorn`. Each
step is its own process; see the memory note above for why that matters. On persistent
Postgres the data **survives redeploys**.

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

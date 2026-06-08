# SCM topology reconciliation — demo / prod / cockpit

**Status: DIAGNOSTIC + PLAN. No Railway actions taken. No authenticated request
made to any host pending Step 1 host-ownership confirmation.**

---

## ✅ RESOLVED (Step 1 + 2 reads, by owner)

**Host ownership — CONFIRMED Case (ii), the clean outcome:**

| Hostname | Owner (Settings → Domains) | Role |
|---|---|---|
| `scm-master-production.up.railway.app` | **"SCM master"** project | **DEMO** — this is what every session curl + the cockpit default hit |
| `vivacious-delight-production-c128.up.railway.app` | **SCM-Master-Prod** project | **REAL PROD** (`SCM_ENV=prod`, long `ADMIN_PASSWORD`) — never touched |

**Implications:**
- §i (admin/admin reached prod) — **NOT APPLICABLE.** All admin/admin traffic hit
  the demo. Real prod has its own domain + strong admin and was never contacted.
- The `-production` in the demo's hostname is Railway's env name, not `SCM_ENV`
  (the exact trap this doc is about).

**Still to read on the "SCM master" (demo) project before deploying:**
- [ ] `SCM_ENV` (expect unset/`dev` — must NOT be `prod` for seeding to work)
- [ ] `SEED_DEMO` (expect `1`)
- [ ] Durable DB? (no Postgres icon on the card → may be ephemeral SQLite → §iii)
- [ ] Cockpit `API_BASE` → should point at this demo host (Step 3)

**Heads-up observed on SCM-Master-Prod Settings:** auto-deploy ON (branch
`production`), "Wait for CI" OFF — a merge to that branch auto-deploys to real prod
without waiting for green CI. Out of scope here; noted.

Everything below is asserted **only as far as the code/docs prove it**. Anything
about which Railway project owns what is marked **[VERIFY ON RAILWAY — yours]**.

---

## The naming trap (why we're here)

`scm-master-production.up.railway.app` contains the word "production" **only
because `production` is Railway's default *environment* name on every project** —
it is **not** the app's `SCM_ENV`. The hostname proves nothing about which project
owns it or whether that backend is forge-locked. The target host was **assumed
from its name, never pinned**. GATE 0 surfaced this before any deliberate prod
promotion — which is the point of the gate.

---

## TASK 1 — Intended topology, from CODE/DOCS ONLY

### (a) Cockpit default `API_BASE` — `SCM-POWER-BI/deploy/server.js:8`
```js
const API  = process.env.API_BASE || "https://scm-master-production.up.railway.app";
const USER = process.env.API_USER || "admin@example.com";
const PASS = process.env.API_PASS || "admin";
```
**When `API_BASE` is unset, the cockpit targets `scm-master-production…` with
`admin`/`admin`.** (`.env.example` ships the same default, commented "replace in
production".) The ops-UI sidebar link defaults separately to
`https://scm-power-bi-production.up.railway.app/` (`frontend/app.js:19`).

### (b) What `SCM_ENV=prod` controls

**`is_production()` — `backend/app/core/config.py:88`** — exact-match only:
```py
return settings.scm_env.strip().lower() == "prod"
```
Only the literal string `prod` locks. `production`, `Prod ` (inner), typos → NOT locked.

**Three guards fire when it's prod:**

1. **Seed forge-lock** (`core/safety.py`): `assert_seeding_allowed()` raises in
   prod; `should_seed_demo()` returns False. Demo/sample seeders refuse to run.
2. **Config guard** (`config.py:103 validate_production`): refuses to boot if
   `SECRET_KEY` is the insecure default or `< 32` chars; `announce_startup()`
   refuses to boot prod on non-Postgres (SQLite) storage.
3. **Weak-admin refusal** (`services/auth.py:60 bootstrap_users`):
   ```py
   admin_pw = os.getenv("ADMIN_PASSWORD", "admin")
   if prod and admin_pw == "admin":
       print("PROD: ADMIN_PASSWORD not set (or still 'admin') — NOT creating a default admin.")
   ```
   In prod, it will **not create** an admin unless a real `ADMIN_PASSWORD` is set.

### ⚠️ The critical nuance the matrix hinges on — `ensure_user` create-if-absent only
`services/auth.py:46` — `ensure_user` **creates the account only if it doesn't
already exist**; it **never rotates an existing password**:
```py
if user_service.get_by_email(db, email) is not None:
    return False   # already exists -> left untouched, password NOT updated
```
**Consequence:** the weak-admin refusal protects a *fresh* prod DB. It does **not**
fix a prod DB where a weak `admin/admin` was already created on an earlier boot
(before `ADMIN_PASSWORD` was set, or under an older build). Setting `ADMIN_PASSWORD`
later does **not** overwrite that existing weak hash. → If prod owns the hostname
and `admin/admin` still logs in, this is the most likely mechanism.

### (c) Documented topology — `docs/DEPLOY.md`
- **Two fully separate stacks**, sharing **no** database (DEPLOY.md:3-15).
- Each stack = **scm-master (Postgres-backed) + SCM Analytics cockpit**.
- **Demo**: `SCM_ENV` unset (`dev`), `SEED_DEMO=1`, `DATABASE_URL=${{Postgres-Demo}}`.
- **Prod**: `SCM_ENV=prod`, **no** `SEED_DEMO`, `DATABASE_URL=${{Postgres-Prod}}`,
  strong `SECRET_KEY`.
- Cockpit `API_BASE` points at **its own** environment's scm-master (DEPLOY.md:62).
- DEPLOY.md gives **no real hostnames** — that's the gap that let the name-trap happen.

### (d) Intended vs actual — side by side

| | Intended (DEPLOY.md) | Actual (Railway, 9 projects) |
|---|---|---|
| Structure | 2 stacks, demo + prod | **3** SCM projects, separate top-level |
| Prod backend | scm-master (prod) + Postgres | **SCM-Master-Prod** — 3/3 online, has Postgres, `SCM_ENV`=`prod` ✅ [VERIFY admin pw] |
| Demo backend | scm-master (demo) + Postgres-Demo | **"SCM master"** — 1/1 online, **NO Postgres icon** ⚠️ [VERIFY: ephemeral?] |
| Cockpit | one *per* stack (demo cockpit + prod cockpit) | **one** "SCM MASTER Power-BI" — points at **?** [VERIFY API_BASE] |
| Hostname owner | (unspecified) | `scm-master-production…` owner **UNKNOWN** [VERIFY — Step 1] |

**Gaps/ambiguities:**
1. Only **one** cockpit project, but the design expects one per stack. So either
   demo or prod has **no** cockpit, or the single cockpit serves one of them.
2. The demo candidate **"SCM master" shows no Postgres** — contradicts "persists to
   Postgres." Possibly ephemeral SQLite (the model DEPLOY.md says they moved *away*
   from) or an external/Railway-shared DB.
3. **No documented hostnames** → identity was inferred from a name. Root cause.

### (e) Code-grounded inference + the two cases for who owns the hostname

The cockpit **defaults** to `scm-master-production…`. If `API_BASE` was never set
on the cockpit, it's hitting whatever project owns that hostname.

- **Case (i): `scm-master-production…` belongs to SCM-Master-Prod (real prod).**
  Then every session curl + the cockpit default has been authenticating to **real
  production**, and if `admin/admin` worked, a weak admin **pre-exists** the strong
  `ADMIN_PASSWORD` (per the `ensure_user` nuance) — the guard isn't "broken," it
  just can't retro-fix an existing account. The public cockpit would be rendering
  **real prod procurement data**. → Highest urgency; remediation §i/§ii.

- **Case (ii): `scm-master-production…` belongs to "SCM master" (demo).**
  Then all the admin/admin traffic was against the demo — **harmless and expected**.
  SCM-Master-Prod (the project you inspected, with the long `ADMIN_PASSWORD`) is a
  *separate*, correctly-secured prod that simply isn't the one the hostname points
  at. The year-filter work proceeds against this demo host. → Clean; remediation §iv.

**You cannot distinguish (i) from (ii) from code.** Only Step 1 (Settings → Domains)
decides it.

---

## TASK 2 — PIN-IT checklist (ordered Railway reads — yours to run)

### STEP 1 — HOST OWNERSHIP (do this first; it decides everything)
Open **Settings → Domains** on the GitHub/web service in **both**:
- **SCM-Master-Prod**, and
- **"SCM master"**.
Find which one lists `scm-master-production.up.railway.app`.

| Outcome | Meaning | Triggers |
|---|---|---|
| Owned by **SCM-Master-Prod** | admin/admin curls + cockpit default hit **real prod** | Remediation **§i** (+ §ii if cockpit unset) — **urgent** |
| Owned by **"SCM master"** | all that traffic was the **demo** — harmless | Continue to Step 2; expect clean path **§iv** |

> Until this read is done, make **no** login/authenticated request to that host.
> After it's done, a **single** `admin/admin` login *test* is acceptable **only if
> the host is confirmed demo**; if it's prod, do not test-login — go to §i.

### STEP 2 — "SCM master" Variables
Read: `SCM_ENV`, `SEED_DEMO`, `DATABASE_URL`, public URL (Settings → Domains).

| Read | Outcome → meaning |
|---|---|
| `SCM_ENV` | unset/`dev` → demo (seedable). `prod` → it's a *second* prod; do NOT seed (→ rethink). |
| `SEED_DEMO` | `1` → boot seeds. unset → selector shows only real years (seed won't run). |
| `DATABASE_URL` | present + Postgres host → durable. absent / sqlite → **ephemeral** (→ §iii). |

### STEP 3 — Cockpit ("SCM MASTER Power-BI") Variables
Read **`API_BASE`** (and `API_USER`/`API_PASS`).

| `API_BASE` value | Meaning | Triggers |
|---|---|---|
| points at **prod** host | public dashboard serves **real prod data** | Remediation **§ii** — urgent |
| points at **demo** host | clean | §iv |
| **unset** | falls back to `scm-master-production…` → whatever Step 1 found | inherits Step 1's verdict |

---

## TASK 3 — REMEDIATION MATRIX

### §i — Step 1 = prod owns the hostname (admin/admin reaching real prod)
**Urgency: immediate.** Mechanism (from code): a weak `admin/admin` account
**pre-exists** the strong `ADMIN_PASSWORD`; `ensure_user` won't rotate it.
Fix:
1. **Rotate the admin password on the prod DB directly** (not via re-seed — the
   bootstrap won't overwrite an existing user). Either delete the weak admin row
   and let the next boot recreate it from the strong `ADMIN_PASSWORD`, or update
   its hash. (Use the prod service Console / a one-off against `Postgres-Prod`.)
2. Confirm the **deployed build** includes the weak-admin-refusal (`auth.py:86`) and
   the forge-lock — a **stale deployed build predating these guards** is an
   alternative cause; redeploy current `main` if so.
3. **Verify `admin/admin` is now REFUSED** on the prod host; confirm the strong
   password works.
4. Note for the record: **prior session data pulls hit prod** (the demand-history
   read, intermittency analysis, runbook curls). Data was **read-only** (GETs +
   login); no writes/seeds were issued (forge-lock would have blocked seeds anyway).

### §ii — Step 3 = cockpit `API_BASE` points at prod
**Urgency: immediate.** The public Power-BI dashboard is rendering **real
production procurement data**.
Fix: set the cockpit's `API_BASE` to the **demo** scm-master URL (once Step 2
identifies it) and a read-only demo account; redeploy the cockpit. Note exposure
window (since whenever it was first pointed there).

### §iii — "SCM master" demo has no persistent DB (ephemeral SQLite)
**Urgency: before relying on the demo.** Seeding won't survive redeploys (data
vanishes → cockpit blanks — exactly the failure DEPLOY.md says they fixed).
Options:
- **(recommended)** Provision a `Postgres-Demo` in the "SCM master" project and set
  `DATABASE_URL=${{Postgres-Demo.DATABASE_URL}}`. Durable; matches the documented design.
- Or accept ephemeral + ensure `SEED_DEMO=1` so every boot re-seeds (data resets on
  each deploy, but the demo is always populated). Cheaper, lossy.

### §iv — CLEAN STATE: "SCM master" is demo (`SCM_ENV`≠prod) AND cockpit points at it
No security finding. Proceed: the year-filter **Stage 1 + seeding target the
confirmed demo host**. Update the runbook's `$API`/`$COCKPIT` to the demo
hostnames and run **docs/DEPLOY-year-filter.md** as written (GATE 0 already passed
by this confirmation). If §iii also applies (no durable DB), resolve that first so
the seeded multi-year history persists.

---

## TASK 4 — Doc corrections (so identity is never assumed from a name again)

Pending Step 1–3 results (need the real hostnames), update:
- **`docs/DEPLOY.md`**: replace the abstract 2-stack diagram with the **3 real
  projects**, their roles, their **actual hostnames**, which Postgres each uses,
  and **which backend the cockpit's `API_BASE` targets**. Add a one-liner:
  *"`-production` in a Railway hostname is the environment name, NOT `SCM_ENV` —
  never infer prod from it."*
- **`docs/DEPLOY-year-filter.md` GATE 0**: replace "check `SCM_ENV` on
  `scm-master-production`" with "confirm the **demo** host by project name +
  Settings→Domains + `SCM_ENV`≠prod, then set `$API` to *that* hostname" — i.e.
  pin the host by project, not by name.

These edits are deferred until the reads return the real hostnames; this doc is the
placeholder that drives them.

---

## First move
**Step 1 — open both scm-master projects' Settings → Domains and find which owns
`scm-master-production.up.railway.app`.** That single read tells us demo vs prod,
and the matrix says exactly what to do next.

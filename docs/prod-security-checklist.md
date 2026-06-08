# Production security checklist — forge-lock verification

The forge-lock (refuse to seed, refuse demo accounts, refuse a weak admin, refuse
SQLite) is enforced in code and **regression-tested** — see
`tests/test_config.py::test_bootstrap_refuses_weak_admin_in_production` and
siblings. Code being correct and the **deployed instance** being configured
correctly are two different things; this checklist verifies the live box.

> ⚠️ Naming trap. The **demo** stack's URL is
> `scm-master-production.up.railway.app` — "production" there is Railway's
> auto-name for the demo project's *prod environment*, NOT the forge-locked
> stack. On the demo, `admin / admin` logging in is **intended**. A reviewer who
> types `admin/admin` at that hostname and gets in will log it as a live finding
> without pausing to learn it's the demo. **Disambiguate the hostname** (see the
> last section) so this can't be mistaken for the real production stack.

## 1. Identify which Railway project is which

In the Railway dashboard, for **each** project, open the scm-master service →
Variables and record:

| | Demo project | Production project |
| --- | --- | --- |
| `SCM_ENV` | unset / `dev` | **`prod`** |
| `SEED_DEMO` | `1` or unset | **unset / `0`** |
| `ADMIN_PASSWORD` | (may be `admin` — fine) | **a real ≥12-char secret, never `admin`** |
| `SECRET_KEY` | any | **strong, ≥32 chars** |
| `DATABASE_URL` | demo Postgres | **prod Postgres** (never SQLite) |
| Public URL | `scm-master-production…` (the demo) | the *other* URL |

If you cannot tell the two apart from the dashboard, that ambiguity is itself the
finding to fix first.

## 2. Verify the live PROD instance is forge-locked

Run against the **real production URL** (not the demo). These need no secrets:

```bash
PROD="https://<the-real-prod-url>"        # NOT scm-master-production (that's demo)

# a) It's up and on Postgres (readiness checks the DB).
curl -s "$PROD/readyz"                      # expect: ok / ready

# b) The weak default admin is REFUSED — this MUST fail.
curl -s -o /dev/null -w "%{http_code}\n" -X POST "$PROD/api/v1/auth/login" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "grant_type=password&username=admin@example.com&password=admin"
# expect: 401  (a 200 here is a LIVE FINDING — rotate ADMIN_PASSWORD now)

# c) No guest account in prod.
curl -s -o /dev/null -w "%{http_code}\n" -X POST "$PROD/api/v1/auth/login" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "grant_type=password&username=guest@example.com&password=guest"
# expect: 401
```

> Do not paste the *real* admin password into a shell (it lands in history/logs).
> To confirm the real admin works, log in through the browser UI instead.

## 3. If step 2b returns 200 (weak admin accepted on prod)

1. Set a strong `ADMIN_PASSWORD` (≥12 chars, random) on the **prod** service in
   Railway → Variables.
2. Redeploy. On boot, `bootstrap_users()` provisions the admin from the new
   password; the old weak admin row (if any) must be deleted manually via a
   one-off `psql` against the prod DB (the bootstrap is create-if-absent, it does
   not rotate an existing row).
3. Re-run step 2b → expect `401`.

## 4. Disambiguate the demo hostname (do regardless)

So nobody mistakes the open demo for production, pick one:

- **Rename the demo** stack/URL so it no longer contains "production"
  (e.g. `scm-master-demo.up.railway.app`), updating the cockpit's `API_BASE` and
  any docs/READMEs that link it; **or**
- **Add a custom domain** to the genuinely-locked stack so *it* unambiguously
  owns the "production" name, and label the demo clearly in its UI as a public
  sandbox.

Either way, the open `admin/admin` login must not sit behind a hostname a
reviewer will read as production.

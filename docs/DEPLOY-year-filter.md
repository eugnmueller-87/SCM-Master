# Deploy runbook — Spend year filter (coupled, 2-repo)

Two PRs, deployed **producer → consumer** with a verify gate between them:

- Backend (producer): `eugnmueller-87/SCM-Master#1`
- Cockpit (consumer): `eugnmueller-87/SCM-POWER-BI#1`

> **Set this once** so every command below targets the same host:
> ```bash
> API=https://scm-master-production.up.railway.app   # the DEMO scm-master (see GATE 0)
> COCKPIT=https://<your-demo-cockpit>.up.railway.app  # SCM Analytics demo service
> ```

---

## GATE 0 — stack identity (do this FIRST; it decides whether you may proceed)

On the **Railway dashboard**, open the `scm-master-production` service → Variables.

- **`SCM_ENV` is NOT `prod`** (unset / `dev`)  → it's the **demo** stack.
  admin/admin + synthetic data are expected. ✅ You may deploy and seed. Continue.
- **`SCM_ENV` = `prod`**  → 🛑 **STOP.** This is real, forge-locked production with a
  guessable admin login. Do **not** deploy or seed here. Fix the admin/admin
  credential first, and find/point at the actual demo stack. The year-filter
  deploy does not happen against prod.

While you're in Variables, also confirm for the next step:
- **`SEED_DEMO=1`** is set on this (demo) service — required for the 18-month
  history seed that gives the selector 2024/2025/2026.

Only continue past this line once GATE 0 says "demo".

---

## STAGE 1 — Backend (merge → wait for deploy → VERIFY)

```bash
# 1. Merge the producer. If Railway auto-deploys on merge-to-main, THIS is the
#    deploy trigger — so nothing else merges until the verify below passes.
gh pr merge 1 --repo eugnmueller-87/SCM-Master --squash

# 2. Wait for Railway to finish the redeploy (watch the dashboard, or poll /health):
until curl -fsS "$API/health" >/dev/null 2>&1; do echo "waiting for backend…"; sleep 5; done
echo "backend /health OK"
```

### VERIFY 1 — gate to Stage 2. All three checks must pass.

```bash
TOK=$(curl -s -X POST "$API/api/v1/auth/login" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "grant_type=password&username=admin@example.com&password=admin" \
  | python -c "import sys,json;print(json.load(sys.stdin)['access_token'])")

# (a) year filter is LIVE *and the seed landed* — must be 200 with 3 years.
#     A 200 returning just ["2026"] means the history did NOT seed -> see fallback.
echo "== /spend/years =="
curl -s "$API/api/v1/analytics/spend/years" -H "Authorization: Bearer $TOK" \
  | python -c "import sys,json; y=json.load(sys.stdin); print('years:',y); assert isinstance(y,list) and len(y)>=3, 'EXPECTED >=3 YEARS (seed did not land)'; print('OK: multi-year')"

# (b) ?year= actually scopes, and per-year totals are distinct + sum to all-time.
echo "== per-year partition =="
python - "$API" "$TOK" <<'PY'
import sys,json,urllib.request
api,tok=sys.argv[1],sys.argv[2]
def get(p):
    r=urllib.request.Request(api+p,headers={"Authorization":"Bearer "+tok})
    return json.load(urllib.request.urlopen(r))
years=get("/api/v1/analytics/spend/years")
allu=get("/api/v1/analytics/spend")["total_units"]
per={y:get(f"/api/v1/analytics/spend?year={y}")["total_units"] for y in years}
print("all:",allu," per-year:",per," (undated:",allu-sum(per.values()),")")
# PRIMARY anti-fallback signal: ?year= actually changes the result.
assert len(set(per.values()))>1, "years are identical -> not really scoping"
# Per-year is a SUBSET of all-time: assets with a null received_date count in
# all-time but bucket into no year (by design — see _received_spend_rows). So
# this is <=, NOT ==. An exact-equality check would false-fail whenever any
# undated assets exist. If this trips, the printed 'undated' count explains it.
assert sum(per.values())<=allu, f"per-year sum {sum(per.values())} EXCEEDS all-time {allu} (impossible -> bug)"
print("OK: distinct per-year, bounded by all-time")
PY

# (c) BONUS this deploy was supposed to fix: Should-Cost + TCO no longer 404,
#     and return real rows (not just non-404).
echo "== should-cost / tco alive with data =="
for P in \
  "/api/v1/analytics/should-cost/savings" \
  "/api/v1/analytics/should-cost/by-supplier" \
  "/api/v1/tco/by-class" ; do
  curl -s -o /tmp/r.json -w "$P -> HTTP %{http_code}\n" "$API$P" -H "Authorization: Bearer $TOK"
  python -c "import json;d=json.load(open('/tmp/r.json'));n=len(d) if isinstance(d,list) else 1;print('   rows/obj:',n);assert n>0,'EMPTY'"
done
```

**Fallback if (a) returns only one year** (deploy ran but history didn't seed):
trigger it on the demo service, then re-run VERIFY 1.
```bash
#   Railway service shell (or one-off):  SEED_DEMO=1 python -m app.seed_history
```

🚦 **Do not proceed to Stage 2 until all of VERIFY 1 passes.** This gap — backend
confirmed live — is the entire reason the two PRs are separate.

---

## STAGE 2 — Cockpit (merge → wait → VERIFY)

```bash
gh pr merge 1 --repo eugnmueller-87/SCM-POWER-BI --squash
until curl -fsS "$COCKPIT/healthz" >/dev/null 2>&1; do echo "waiting for cockpit…"; sleep 5; done
echo "cockpit /healthz OK"
```

### VERIFY 2 — real per-year data through the cockpit (not the all-time fallback)

```bash
# The selector list the cockpit will render:
echo "== cockpit knows the years =="
curl -s "$COCKPIT/api/data" \
  | python -c "import sys,json;d=json.load(sys.stdin);print('years:',d.get('years'));assert len(d.get('years',[]))>=3,'cockpit fell back to all-time only';print('OK')"

# Scoped fetch must echo the year and differ from all-time:
echo "== cockpit ?year= scopes =="
python - "$COCKPIT" <<'PY'
import sys,json,urllib.request
c=sys.argv[1]
def get(p): return json.load(urllib.request.urlopen(c+p))
allt=get("/api/data")["spend_total"]["total_spend"]
y25 =get("/api/data?year=2025")
assert y25.get("year")==2025, f"expected year=2025, got {y25.get('year')} (fallback!)"
print("all-time:",allt," 2025:",y25["spend_total"]["total_spend"])
assert y25["spend_total"]["total_spend"]!=allt, "2025 == all-time -> not scoping"
print("OK: cockpit serves real per-year data")
PY
```

Then open `$COCKPIT/` in a browser and eyeball:
- **Year** dropdown shows 2024 / 2025 / 2026; switching it changes Total Spend + charts.
- **Should-Cost** and **TCO** tabs are populated (their first time live).

---

## Rollback

Both changes are additive/backward-compatible. If anything looks wrong:
- Revert the cockpit merge first (it's the consumer) — backend keeps serving fine.
- The backend revert is safe too (`?year=` optional), but reverting it re-introduces
  the Should-Cost/TCO 404s, so prefer fixing forward there.

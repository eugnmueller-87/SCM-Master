# Purchasing-Run Review — API contract

Everything a frontend needs to build the **purchasing-run review screen**: trigger
a run, show the proposed/escalated bundled POs with rationale + confidence, and
approve selected suppliers to place them. No backend state is persisted between
preview and confirm — confirm **recomputes from live data**, so the contract is
two stateless calls.

Base URL: `/api/v1` · All calls need a Bearer JWT · Purchasing calls require the
**PROCUREMENT** role (ADMIN also passes).

---

## 0. Auth (prerequisite)

```
POST /api/v1/auth/login        # form-encoded: username, password
  -> { "access_token": "...", "token_type": "bearer" }
```
Send `Authorization: Bearer <access_token>` on every call below.

---

## 1. Preview a run

```
POST /api/v1/agent/purchasing-run
Body: { "dry_run": true, "period_days": 7 }   # both optional; these are defaults
  -> 200 PurchasingRunResult
```

`dry_run: true` computes and tiers everything but **places nothing**. This is the
screen's load call. (`dry_run: false` would auto-place all `act` bundles — the
review screen should NOT use that; use confirm instead.)

### PurchasingRunResult

```jsonc
{
  "run_at": "2026-06-04T09:00:00+00:00",
  "dry_run": true,
  "period_days": 7,
  "decisions": [ PurchasingDecision, ... ],   // one per product line
  "summary": {
    "act": 3,           // # line-decisions at act tier
    "placed": 0,        // # POs actually placed (0 on a dry preview)
    "proposed": 2,
    "escalated": 1,
    "total_committed": 0.0   // € value actually placed (0 on preview)
  }
}
```

### PurchasingDecision (one per product line)

```jsonc
{
  "product_id": "uuid",
  "supplier_id": "uuid | null",   // null only when no contracted source (escalate)
  "qty": 10,
  "unit_price": 3200.0,
  "total": 32000.0,               // line total (qty * unit_price)
  "trigger": {
    "type": "lifecycle_replacement | reorder_floor | forecast_shortfall",
    "evidence": { ... }           // the numbers behind the trigger, e.g.
                                  // {"decommissioned_in_period":3,"replace_ratio":1.0,"since":"..."}
  },
  "tier": "act | propose | escalate",
  "confidence": 0.62,             // bundle confidence (weakest line in the supplier bundle)
  "rationale": "[lifecycle_replacement] {...} | net_need=3 | bundle_tier=act | <agent prose>",
  "placed_po_id": null            // set to a PO id once placed (after confirm)
}
```

### How the screen should group & read it

- **Group `decisions` by `supplier_id`.** Each group is ONE purchase order (one PO
  per supplier — required for invoice matching). All lines in a group share the
  same `tier`, `confidence`, and (after confirm) `placed_po_id`.
- **`tier` is per-bundle**, shown on the supplier group:
  - `act` — passed every auto-place gate; safe to one-click approve.
  - `propose` — sound but wants a human; the review screen's main case. Approvable.
  - `escalate` — blocked (no contracted source, over the spend threshold, or low
    confidence). **Not approvable here** — show why (see `rationale`) and route to
    a human/buyer.
- Show `trigger.type` + `trigger.evidence` as the justification ("why is this being
  bought") — every decision has one; the system never proposes an unjustified buy.
- `confidence` and `rationale` come from the AI copilot.
- A `supplier_id: null` group = a product with no contracted source → always
  `escalate`, "new supplier needed".

---

## 2. Confirm (approve → place)

```
POST /api/v1/agent/purchasing-run/confirm
Body: { "approve_suppliers": ["<supplier_id>", ...], "period_days": 7 }
  -> 200 PurchasingRunResult   (recomputed; placed bundles have placed_po_id set)
```

- Send the `supplier_id`s of the bundles the user approved (the group keys from
  step 1).
- The run is **recomputed from live data**. A supplier is placed only if it is in
  `approve_suppliers` **and** its recomputed bundle is `act` or `propose`.
- An `escalate` bundle is **never** placed by confirm.
- If a need is no longer justified at confirm time (e.g. it got covered by other
  inbound between preview and confirm), that bundle simply won't appear / won't
  place — a **stale approval can't place a wrong PO**. The screen should re-read
  the returned result rather than assume the preview still holds.
- `summary.placed` = number of POs created; `summary.total_committed` = € placed.
- Placement goes through the normal purchase-order service (audited, role-gated);
  each placed bundle becomes one multi-line PO you can then see under
  `GET /api/v1/purchase-orders`.

---

## Suggested screen flow

1. On load: `POST /agent/purchasing-run {dry_run:true}` → group decisions by
   supplier → render one card per supplier PO (tier badge, total, lines, trigger,
   confidence, rationale).
2. User ticks the supplier cards to approve (disable ticking on `escalate`).
3. On "Place approved": `POST /agent/purchasing-run/confirm {approve_suppliers:[...]}`.
4. Re-render from the returned result; placed cards now carry `placed_po_id`
   (link to `GET /purchase-orders/{id}`). Surface any approved-but-not-placed
   bundle (stale/now-escalate) with the reason.

## Notes / limits (so the UI sets honest expectations)

- No run is persisted; preview and confirm are independent recomputations. If you
  need an auditable saved run + approve-by-run-id, that's a future migration.
- `forecast_shortfall` currently logs over-capacity locations but does not raise
  product-level buys (no per-product deployment target in the model yet), so
  triggers you'll see in practice are `lifecycle_replacement` and `reorder_floor`.
- Spend/threshold gates are env-configurable: `AUTO_PLACE_SPEND_CAP`,
  `ACT_CONFIDENCE_FLOOR`, `ESCALATE_SPEND_THRESHOLD`.

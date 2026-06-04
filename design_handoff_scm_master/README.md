# Handoff: SCM Master — operations UI redesign

A complete redesign of the SCM Master frontend (the dependency-free console served
by FastAPI at `/`), plus four new operational screens. Built in the TrueSpend
editorial design language (paper / ink / gold).

> Repo this targets: `eugnmueller-87/SCM-Master` · backend is FastAPI under `/api/v1`,
> the existing frontend is vanilla JS in `frontend/`.

---

## 0. TL;DR for the developer

Unlike a typical design handoff, **these are not throwaway mockups** — `frontend/` is a
working, dependency-free drop-in (no React, no build step, no CDN) written against the
**real `/api/v1`** contract read from your backend. You can copy the five files straight
into the repo's `frontend/` and the existing screens (Login, Overview, Assets, Inbound,
Capacity, Spend) work immediately against the current API.

**Four screens need new/extended backend data to go fully live** — they render today off
embedded sample data and degrade gracefully, but to make them real you must add the
endpoints/fields in **§6 Backend work required**:

| Screen | Works against current API? | Backend work to go live |
|---|---|---|
| Login, Overview, Assets, Inbound, Capacity, Spend | ✅ Yes, as-is | none |
| **Agent** (drawer) | ⚠️ needs `/agent/*` mounted | confirm agent router is exposed |
| **Contracts** (lifecycle + budget) | ⚠️ partial | add term dates, budget, YTD spend to `ProductSupplier` |
| **Inventory & reorder** | ❌ sample data | add a stock/consumption read model |
| **Tracking** (control tower) | ❌ sample data | separate logistics schema (see seed SQL) |

---

## 1. Fidelity

**High-fidelity.** Final colors, typography, spacing, interactions, and copy. Recreate
pixel-for-pixel. Because the deliverable is itself plain HTML/CSS/JS, the simplest path
is to **use the files directly** (they are framework-agnostic and self-contained). If you
are migrating SCM Master to a framework (React/Vue/etc.), treat these as the exact spec
and port them using the design tokens in §5.

---

## 2. About the design files

All files live under `frontend/` and are loaded in this order by `index.html`:

```
app.css        Self-contained stylesheet: TrueSpend design tokens (top) + all component
               styles (shell, tables, pills, steppers, drawer, tweaks, inventory bars,
               budget burn). No external CSS. Fonts via Google Fonts + system fallbacks.
app.js         Core app: API layer, auth, sidebar/topbar, and the original screens
               (Overview, Assets, Inbound, Capacity, Spend). Exposes window.__scmInit().
features.js    Agent drawer (/agent/*), Contracts screen (lifecycle + budget burn),
               and the Tweaks control panel (host edit-mode protocol). Shares app.js scope.
tracking.js    Tracking / control-tower screen. Sample data embedded.
inventory.js   Inventory & reorder planning screen. Sample data embedded.
index.html     Login + app shell skeleton; loads the four scripts then calls __scmInit().
```

`preview-harness.html` (in the bundle root) is an **offline preview only** — it stubs
`window.fetch` with mock `/api/v1` responses so you can open the UI with no backend.
**Do not ship it.** It demonstrates the exact response shapes each screen expects (a
useful contract reference — see its inline `<script>`).

### Architecture conventions (match these when extending)
- **No dependencies.** Everything is vanilla ES. Helpers: `$`, `$$`, `api()`, `esc()`,
  `euro()`, `fmtDate()`, `icon()`, `plainPill()`, `statusPill()`, `productCell()`.
- **Each screen registers itself**: `RENDER.<tab>()` renders into `#screen`; add a `NAV`
  entry and a `CRUMBS` label. New screens in separate files splice into `NAV` at load.
- **All network through `api(path, {method, body, form})`** — it attaches the Bearer
  token from `localStorage.scm_token` and throws on non-2xx (401 ⇒ auto-logout).
- **Money** is `de-DE` formatted (`€403.780`). **Dates** are `en-GB` (`28 May 2026`).
- Boot is gated: `app.js` defines `window.__scmInit`; `index.html` calls it *after* all
  feature files have registered their nav items.

---

## 3. Screens

### 3.1 Login
Split screen. Left = ink (#161413) panel with the SCM Master wordmark (three stacked
rack-rail bars in gold), an editorial pitch ("One unit. From dock to *decommission*."),
and a footer strip. Right = sign-in card (email + password, primary gold button).
`POST /auth/login` (form-encoded `username`/`password`) → stores `access_token`.

### 3.2 Overview
Two-column (content + 312px rail). Composed live from `/assets`, `/planning/inbound`,
`/planning/capacity`, `/planning/forecast`, `/analytics/spend`.
- **KPI strip** (4): under management, deployed (%), inbound outstanding (+overdue), spend tracked.
- **Lifecycle distribution** bar (segments per status) + legend.
- **"Needs you this week"** — derived from real signals: overdue POs, over-capacity
  locations, units stuck in maintenance. Each links to the relevant tab.
- **Rail**: deployment forecast + capacity snapshot.

### 3.3 Assets (the spine)
Filterable table (All / Received / In storage / Deployed / Maintenance / Retired).
Row click → inline expand showing:
- **Lifecycle stepper** (Received → In storage → Deployed → Maintenance → Decommissioned → Disposed).
- **Event log** (append-only `/assets/{id}/events`).
- **Provenance** (`/assets/{id}/provenance`): order → supplier → unit price → received → age → location.
- **Transition buttons** — only legal next states (mirror of backend state machine);
  `POST /assets/{id}/transition` `{target, location_id?}` (deploy auto-targets a RACK).

### 3.4 Inbound
`/planning/inbound`. Open order lines with ordered/received/outstanding, ETA, and an
**Overdue** flag (server sets `overdue:true`).

### 3.5 Capacity
`/planning/capacity`. Per-location used/capacity with a utilisation bar (green→gold→amber→red)
and an **Over capacity** pill (server `over_capacity:true`).

### 3.6 Spend
`/analytics/spend`. Totals + by-supplier and by-category bars (share of max).

### 3.7 Agent (topbar button → right drawer)  *(features.js)*
Ink button with gold spark in the topbar. Opens a 440px right drawer:
- **Insights** — `GET /agent/insights`: severity-tiered cards (info/watch/action) with
  evidence list, assumption, limitation, and a confidence bar.
- **Weekly purchasing run** — "Run preview" → `POST /agent/purchasing-run` `{dry_run:true, period_days:7}`.
  Renders decision cards tiered **act / propose / escalate**, each with qty × unit price,
  rationale, trigger, confidence, and an approve checkbox (escalate = needs sign-off).
  "Place approved" → `POST /agent/purchasing-run/confirm` `{approve_suppliers:[…], period_days}`.
- Autonomy badge reflects the **Tweaks → Agent autonomy** setting.

### 3.8 Contracts (lifecycle + budget)  *(features.js)*
Each `/product-suppliers` row is a sourcing contract.
- **Table columns**: Supplier (+preferred/rank), Scope (product), Contract price,
  **YTD spend vs budget** burn bar (gold fill; amber > 85%; **red when over budget**;
  shows "€X spent" + "€Y left / €Z over"), **Renewal** chip ("N days left", amber ≤ 60d;
  "N days lapsed" red), Status pill.
- **Row expand**: contract lifecycle stepper (Draft → Active → Renewal due → Expiring →
  Expired), renewal line, a **Budget panel** (burn-down bar with spent/%/remaining + a
  monthly cumulative burn mini-chart), Terms (price, lead time, MOQ, rank, supplier SKU,
  term dates), and Draft renewal / Re-source actions.

### 3.9 Inventory & reorder  *(inventory.js)*
Per-item reorder planning so buyers never over- or under-order.
- **KPI tiles**: items tracked / need reordering / overstock risk / on order.
- **Legend**: on hand · on order (hatched) · reorder point · "bar spans 0 → capacity".
- **Table**: each item has a **stock-vs-capacity bar** (filled on-hand segment colored by
  status + hatched on-order segment + a reorder-point tick), **Cover** (days of stock at
  current burn), **Next delivery** (days out + date), an **action pill**, and a
  recommendation line below the row.
- **Reorder model** (documented in the file header):
  `days_of_cover = on_hand / daily_burn`, `reorder_point = daily_burn × lead_time + safety_stock`.
  Statuses: **Stock-out risk** (cover ≤ lead time, nothing on order) · **Expedite** (runs
  dry before the inbound lands) · **Reorder** (below ROP) · **Overstock risk** (on-order
  would exceed capacity → trim PO) · **On order** · **Healthy**. Suggested order qty
  refills toward ~85% of capacity.

### 3.10 Tracking (control tower)  *(tracking.js)*
Per-order delivery tracking from the supplied `scm_tracking_seed.sql`.
- **KPI tiles**: open / delayed-at-risk / out-for-delivery / delivered.
- **Order cards**: mode icon (ocean→ship, air→plane, road→truck, rail→train), PO ·
  supplier (country), line × qty · value, status pill (Customs hold / At risk / In transit
  / Out for delivery / Delivered / Placed), a **6-node milestone track** (done = blue,
  current = enlarged, exception = red), current location, ETA + delay (+N days / On time).
- Card click → **event timeline** panel below (scan-by-scan events, held event in red,
  "Promised → now" dates) with an **Escalate** action.

---

## 4. Interactions & behavior
- **Navigation**: sidebar buttons set `currentTab`, re-render nav, update crumbs, call `RENDER[tab]()`.
- **Inline expand** (Assets, Contracts): one open row at a time; chevron rotates 90°; brief fades in.
- **Agent drawer / Tweaks panel**: slide/scale transitions (~220ms). Reveal is applied
  synchronously after a forced reflow (`void el.offsetWidth`) — **do not rely on
  `requestAnimationFrame`**, it is throttled in backgrounded iframes and the panel won't show.
- **Toasts**: `toast(msg, "ok"|"err"|"")` — bottom-center, auto-dismiss ~2.8s.
- **Transitions/placements** re-fetch and re-render; counts re-prime.
- **Empty/error/loading** states everywhere (`.state`, `errState()`).
- **Reduced motion** respected (`@media (prefers-reduced-motion: reduce)`).

## 5. Tweaks (host edit-mode protocol)
The Tweaks panel listens for `__activate_edit_mode` / `__deactivate_edit_mode` postMessages
and announces itself with `__edit_mode_available`. Controls (persisted to
`localStorage.scm_tweaks`): **Signal accent** (gold/slate/sage/umber — overrides
`--ts-brand-gold*`), **Table density** (comfortable/compact via `[data-density]`),
**Agent autonomy** (suggest / low-risk / full — drives the drawer badge). If you don't
have that host shell, this degrades to inert; safe to remove.

---

## 6. Backend work required (to make new screens live)

The frontend reads real fields when present and falls back to embedded samples otherwise.
To go fully live:

### 6.1 Agent  — confirm router mounted
`features.js` calls `GET /agent/insights`, `POST /agent/purchasing-run`,
`POST /agent/purchasing-run/confirm`. These exist in `backend/app/api/v1/agent.py`;
ensure the agent router is included in `api/v1/__init__.py` and the LLM key is configured.
Response shapes are in `preview-harness.html` (the `INSIGHTS` / `runResult` mocks).

### 6.2 Contracts — extend `ProductSupplier`
Add nullable fields (and surface them on the `/product-suppliers` response):
- `contract_status` (enum: DRAFT, ACTIVE, RENEWAL_DUE, EXPIRING, EXPIRED, SUPERSEDED) —
  or derive from term dates server-side.
- `term_start` (date), `term_end` (date) — drives the renewal countdown.
- `annual_budget` (numeric), `ytd_spend` (numeric) — drives the budget burn. `ytd_spend`
  is ideally computed from received-asset cost YTD against this contract.
Until added, the UI derives `ACTIVE/EXPIRED` from `active` and synthesises a budget plan.

### 6.3 Inventory — new stock/consumption read model
Add an endpoint (e.g. `GET /planning/inventory`) returning per product:
`product_id, location (or per-location), on_hand, capacity, safety_stock, daily_burn
(avg units consumed/deployed per day over a trailing window), lead_time_days (from the
preferred contract), on_order (open inbound qty), next_eta (earliest inbound ETA),
unit_price`. The reorder math lives client-side; the server only needs to supply those
inputs. Replace the `INVENTORY[]` array in `inventory.js` with a fetch.

### 6.4 Tracking — logistics schema (separate from /api/v1)
Provision the supplied `scm_tracking_seed.sql` (suppliers · purchase_orders · shipments ·
shipment_events → `v_order_tracking`). Expose via PostgREST or new FastAPI routes:
`GET /v_order_tracking` and `GET /shipment_events?shipment_id=eq.<id>&order=seq`. Field
names already match; replace the `TRACKING[]` array in `tracking.js` with the two fetches.

---

## 7. Design tokens (from app.css `:root`)
- **Paper/ink**: `--ts-paper #F7F4ED`, `--ts-paper-deep`, `--ts-surface`, `--ts-ink #161413`,
  `--ts-ink-night`, `--ts-ink-soft`, `--ts-ink-mute`, `--ts-ink-faint`.
- **Brand gold** (the signal accent, tweakable): `--ts-brand-gold #B07219`,
  `--ts-brand-gold-deep #8F5C12`, `--ts-brand-gold-soft`, `--ts-brand-gold-wash`.
- **Semantic**: positive (green), warning (amber, fg `#8C6510`), negative (brick), info (blue)
  — each with a matching `*-wash` background.
- **Lines**: `--ts-line`, `--ts-line-soft`, `--ts-line-strong`.
- **Type**: `--ts-font-display` (serif, headings), `--ts-font-sans` (UI), `--ts-font-mono`
  (serials/refs/numbers). Eyebrows: 10–11px, 0.14em tracking, uppercase. Page titles use
  the display serif at ~42px, -0.025em.
- **Radius**: `--ts-radius-xs/sm/md/lg`. **Shadows**: `--ts-shadow-lg`.
- **Motion**: `--ts-dur-fast/med`, `--ts-ease`, `--ts-ease-emphatic`.
- **Tabular numbers** everywhere figures align (`font-variant-numeric: tabular-nums`).

## 8. Assets
- **No bitmap assets.** All icons are inline single-color SVGs (Lucide stroke style, 1.5px,
  rounded) defined in `app.js` (`ICONS`) and extended in feature files. The wordmark is
  inline SVG. Fonts load from Google Fonts (swap to self-hosted for offline deploys via the
  `@import`/`@font-face` at the top of `app.css`).

## 9. Files in this bundle
```
frontend/index.html      Shell + load order
frontend/app.css         Tokens + all component styles
frontend/app.js          Core + original screens
frontend/features.js     Agent drawer, Contracts, Tweaks
frontend/tracking.js     Tracking screen (sample data)
frontend/inventory.js    Inventory & reorder (sample data)
preview-harness.html     Offline preview w/ mocked /api/v1 — DO NOT SHIP (contract reference)
```
Seed for Tracking (provided separately by the product owner): `scm_tracking_seed.sql`.

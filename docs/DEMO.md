# SCM Master — Demo Script

A 7–10 minute walkthrough of the operations console, end to end. The story:
**one serial-tracked unit, from purchase order to decommission — with an AI
copilot watching the whole operation.**

---

## 0. Setup (once, ~30s)

From `backend/`:

```powershell
.venv\Scripts\alembic upgrade head
.venv\Scripts\python -m app.seed_demo
.venv\Scripts\uvicorn app.main:app --reload
```

Open **http://localhost:8000/**. Ensure `ANTHROPIC_API_KEY` is set in `backend/.env`
(the Agent drawer and the chat copilot need it).

**Logins** (password = role, to show role-gating):

| User | Password | Role | Can do |
|---|---|---|---|
| `admin@example.com` | `admin` | ADMIN | everything |
| `buyer@example.com` | `buyer` | PROCUREMENT | orders, approvals, re-sourcing |
| `warehouse@example.com` | `whse` | WAREHOUSE | receiving, moves |
| `dc@example.com` | `dc` | DATACENTER | deploy / decommission |

Demo as **admin**.

---

## 1. Login (15s)
Editorial split screen. One line: *"One unit. From dock to decommission."* —
that's the whole pitch. Sign in.

## 2. Overview (60s) — "the state of the operation"
- **KPI strip**: ~126 assets under management, ~90 deployed, inbound outstanding
  (with an overdue flag), spend tracked.
- **Lifecycle distribution** bar — point out units spread across received → disposed.
- **"Needs you this week"** — overdue PO, over-capacity location, a unit in
  maintenance. Each links to its screen. *"The system tells you where to look."*

## 3. Assets (90s) — **the spine**
- Filter **Deployed**, then open any row.
- **Lifecycle stepper** — Received → In storage → Deployed → … . *"Same physical
  unit, one identity, the whole way through."*
- **Event log** — append-only, who/when/from→to.
- **Provenance** — trace back to the order line, supplier, unit price, age.
  *"Every serial traces to the buy it came from — that's the unbroken thread."*
- (As admin) push a transition, e.g. an in-storage unit → Deployed. It re-renders.

## 4. Inbound + Capacity (45s)
- **Inbound**: open order lines, ordered/received/outstanding, **one line flagged
  Overdue** (PO-2026-0035, the EPYC line).
- **Capacity**: the **Inbound staging cage** is over capacity (7/6), and a rack is
  hot. *"Capacity is measured, not guessed — it flags before you overfill."*

## 5. Contracts (60s) — lifecycle + budget
- Each row is a sourcing contract. Show the **status mix**: Active, Renewal-due,
  Expiring, Expired, Draft — all present.
- Point to a **budget burn bar** (YTD spend vs annual budget, computed from
  received-asset cost) and a **renewal countdown** chip.
- Expand a row → contract stepper + budget panel + terms. *"Multi-sourcing means
  re-sourcing a line is one click, without losing history."*

## 6. Spend + Inventory (45s)
- **Spend**: totals + by-supplier / by-category. Call out the **memory
  concentration** (Samsung dominates DIMM spend).
- **Inventory & reorder**: per-item stock-vs-capacity bars, days of cover, next
  delivery, and an action pill (Reorder / Expedite / Overstock risk). *"So a buyer
  sees what to order, how much, and what NOT to overstock."*

## 7. Tracking (45s) — control tower
- Order cards with mode icons; **PO numbers match Procurement** (same PO-2026-00xx).
- One shipment is **held in customs** (red exception). Click it → **scan-by-scan
  timeline**. *"Where is every order, right now."*

## 8. Agent drawer (75s) — the autonomous side
Top-bar spark button → right drawer.
- **Insights**: severity-tiered cards (action/watch/info) with evidence,
  assumption, limitation, confidence bar — grounded in the live data.
- **Weekly purchasing run** → "Run preview". Decision cards tiered **act /
  propose / escalate**, each with qty × price, the **demand trigger** (e.g.
  lifecycle replacement from the decommissioned JBOD), rationale, confidence,
  and an approve checkbox. *"Demand-justified — it never proposes a buy without a
  reason. One PO per supplier, for invoice matching."*
- (Optional) tick an approvable bundle → "Place approved" → it places a real PO.

## 9. Copilot chat bubble (60s) — **ask anything**
Bottom-right bubble. It's wired into the whole live operation. Try:
- *"What needs my attention this week?"*
- *"Which contracts are expiring soon?"*
- *"Are any locations over capacity?"*
- *"What makes this different from a normal warehouse app?"*

Answers cite real numbers (PO numbers, €, supplier names, counts) from the live
snapshot — not canned text. *"The same data, now conversational."*

---

## One-line close
> "Procurement, warehouse flow, and asset lifecycle on a single spine — every
> serial traced back to the order it came from — with an AI copilot that plans
> the buying and answers anything about the operation."

---

## If something looks empty
Re-run the seed on a fresh DB (stop the server first so the SQLite file frees):
```powershell
del scm.db ; .venv\Scripts\alembic upgrade head ; .venv\Scripts\python -m app.seed_demo
```

## Notes
- The Agent drawer and chat need `ANTHROPIC_API_KEY`; without it those two return
  a graceful error and the rest of the demo is unaffected.
- Tracking shipments are generated from the real demo POs, so PO numbers,
  suppliers and values reconcile with Procurement and Inbound.

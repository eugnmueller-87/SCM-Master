# Integration Architecture — running SCM-Master alongside SAP + Coupa

**Status:** implemented for Coupa (CSV); SAP and write-back scaffolded but not built.
**Question it answers:** *"Our landscape is SAP (ERP) + Coupa (P2P). Could we install this?"*

## 1. The stance: system of *intelligence*, not system of *record*

In an enterprise that already runs SAP and Coupa, those systems own the truth —
the supplier master, the material master, the purchase orders that invoices match
against. SCM-Master does **not** try to replace them. It sits *alongside* them as
the **forecasting + sourcing + AI-reasoning brain**: it reads their master and
transactional data, runs the demand forecast / purchasing logic / agent reasoning
on top, and proposes actions back as **requisitions** — leaving Coupa/SAP to run
approval and actually issue the PO, so the three-way invoice match is never
disturbed.

That single decision shapes everything below: data flows *in* by sync, decisions
flow *back* as proposals, and the upstream system stays authoritative.

## 2. The shape — ports & adapters

```
            UPSTREAM (authoritative)                 SCM-MASTER (intelligence)
   ┌───────────────────────────────────┐   ┌──────────────────────────────────────┐
   │                                    │   │                                      │
   │  SAP ERP        Coupa P2P          │   │   ┌──────────┐   canonical    ┌────┐ │
   │  • material     • supplier master  │   │   │ Adapter  │   records      │Sync│ │
   │    master       • requisitions ──feed─────▶│ (per     │──FeedBatch────▶│engine│
   │  • vendor       • purchase orders  │   │   │ upstream)│  Supplier/     │    │ │
   │    master       • receipts         │   │   └──────────┘  Material/PO   └─┬──┘ │
   │                                    │   │     coupa.py                    │    │
   │                                    │   │     sap.py (future)             │    │
   │                                    │   │                          upsert │    │
   │                                    │   │                   by (source_system,  │
   │                                    │   │                       external_ref)   │
   │                                    │   │                                 ▼     │
   │                                    │   │   ┌─────────────────────────────────┐ │
   │                                    │   │   │ EXISTING domain services        │ │
   │                                    │   │   │ catalog / procurement / …       │ │
   │                                    │   │   │ (same rules as the UI path)     │ │
   │                                    │   │   └───────────────┬─────────────────┘ │
   │                                    │   │                   ▼                   │
   │                                    │   │   Product · Organization · PO  (DB)   │
   │                                    │   │                   │                   │
   │                                    │   │                   ▼                   │
   │                                    │   │   Demand forecast · Purchasing brain  │
   │                                    │   │   · AI reasoning  (the actual IP)     │
   │                                    │   │                   │                   │
   │   ◀───── requisition (write-back) ─────────────────────────┘                   │
   │         [SCAFFOLDED, not built]    │   │                                      │
   └───────────────────────────────────┘   └──────────────────────────────────────┘
```

- **Adapter** (`app/integrations/coupa.py`) — knows ONE upstream's wire format.
  Parses the feed, maps it onto canonical records. No DB, no business rules.
- **Canonical records** (`app/integrations/schemas.py`) — the source-agnostic
  `SupplierRecord` / `MaterialRecord` / `PurchaseOrderRecord`. The contract
  between any adapter and the sync engine.
- **Sync engine** (`app/integrations/sync.py`) — the only DB-touching part.
  Upserts through the **same domain services the UI uses**, so every rule
  (a PO's supplier must hold the supplier role, a line's source must match its
  product, one-PO-per-supplier) is enforced identically. No logic duplicated.

Adding SAP later = add `sap.py` producing the same canonical records. The sync
engine and everything downstream are untouched.

## 3. Identity & idempotency — the `(source_system, external_ref)` key

Every record an upstream system owns carries two columns (mixin `ExternalRefMixin`):

| column          | meaning                                    | example         |
|-----------------|--------------------------------------------|-----------------|
| `source_system` | which upstream this row was synced from    | `"coupa"`, `"sap"` |
| `external_ref`  | that system's own id for the record        | `"SUP-DELL"`, `"PO-2026-0001"` |

The sync **upserts on this pair**, which makes re-running a feed safe: the same
Coupa export imported twice **updates** the existing rows, it does not duplicate
them. Records created *inside* SCM-Master simply leave both null. This is the
single mechanism that lets the two systems exchange the same records repeatedly
without drift.

## 4. What's built today

**Inbound sync from Coupa (CSV):** working end-to-end.

```
POST /api/v1/integrations/coupa/import?dry_run=true   (PROCUREMENT role)
  multipart file = Coupa PO export CSV
  -> SyncReport { suppliers:{created,updated}, materials:{…}, purchase_orders:{…}, warnings:[…] }
```

- **`dry_run=true` (default) is a true preview** — the sync runs *in full* inside
  a SAVEPOINT that is then rolled back, so the returned counts/warnings are
  exactly what a real import would produce, with nothing persisted. `dry_run=false`
  commits.
- Maps Coupa statuses (`issued`→`PLACED`, `approved`→`APPROVED`, …); unmapped
  statuses surface as warnings rather than being silently dropped.
- Enforces one-PO-per-supplier at parse time (invoice-matching invariant).
- A re-synced PO refreshes its header always, but **replaces its lines only while
  still open** — once received/cancelled, its lines are matched against
  receipts/invoices and are left frozen.

A sample export lives at `backend/app/integrations/samples/coupa_po_export.csv`.

## 5. What's scaffolded but deliberately NOT built

These are the next layers; each is named so the gap is explicit, not hidden:

1. **SAP inbound adapter** — `sap.py` mapping IDoc / OData (material master, vendor
   master, PO/GR) onto the same canonical records. The hard part is field mapping,
   not architecture — the sync engine already exists.
2. **Write-back** — emit a **requisition** to Coupa (cXML/API), *not* a PO, so
   Coupa runs approval and issues the PO and invoice matching stays intact. Needs
   an **outbox** table + idempotency keys (don't double-send on retry).
3. **Scheduled / event-driven sync** — today's import is operator-triggered
   (upload a file). Production wants a scheduled pull or a webhook intake, with a
   "last synced" watermark.
4. **SSO** — replace local JWT with OIDC/SAML against Azure AD. Tracked separately;
   not in this pass.

## 6. Operational notes for a real install

- **Database:** the demo uses reseed-on-boot SQLite. A real install sets
  `DATABASE_URL=postgresql+psycopg://…` (add `psycopg[binary]` to requirements);
  migrations are already Postgres-clean (batch mode only engages on SQLite).
- **Why indexes, not a composite-unique constraint, on the pair:** uniqueness of
  `(source_system, external_ref)` is enforced in the service-layer upsert. Keeping
  it out of the schema avoids a SQLite-vs-Postgres parity wrinkle and lets a
  record legitimately exist with the columns null (born here). If a hard DB
  guarantee is wanted in prod, add a partial unique index where both are non-null.
- **Auditability:** synced rows are distinguishable from native ones by a non-null
  `source_system`, and the existing `date_created`/`last_updated` columns timestamp
  every upsert.
```

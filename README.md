# SCM Master

A supply-chain management system for **hardware procurement and asset lifecycle tracking** — built for the case where a small, fast-turning *transit warehouse* feeds equipment into datacenter racks.

It joins together three things that off-the-shelf tools usually keep apart:

1. **Procurement** — what you buy and from whom.
2. **Warehouse flow** — receiving goods into a transit warehouse.
3. **Asset lifecycle** — following each physical unit from arrival all the way to decommission.

## What makes it different

Most warehouse apps stop at "stock in, stock out." Most CMDBs (configuration-management databases) only start once a unit is already racked. Neither follows a single unit across that boundary.

This system models the **continuous identity of an asset** as its spine. When a serialised unit is received, an `Asset` is born. The *same* row then moves through the warehouse and into a rack — its location and status change, but its identity, and its link back to the purchase-order line it came from, never breaks. That unbroken thread is what makes end-to-end spend and provenance tracing possible.

The second deliberate design choice is **multi-sourcing**. A `Product` (the spec) is kept separate from a `ProductSupplier` (one row per source of that product). "Replacing a supplier" — critical under spiky demand and long chip lead times — is then just choosing a different source, without losing the product's identity or its purchase history.

## How it flows

The process the system models, end to end — from picking a source to a unit's final disposal:

```mermaid
flowchart LR
    subgraph CAT["Catalog"]
        P["Product<br/>(the spec)"]
        PS["ProductSupplier<br/>(a source: lead time,<br/>MOQ, price, rank)"]
        P -- "many sources" --> PS
    end

    subgraph PROC["Procurement"]
        PO["PurchaseOrder"]
        OI["OrderItem<br/>(chosen source)"]
        PO --> OI
        PS -. "re-source = repoint" .-> OI
    end

    subgraph FLOW["Warehouse flow"]
        R["Receipt<br/>(goods arrive)"]
        OI --> R
    end

    subgraph LIFE["Asset lifecycle"]
        A["Asset<br/>(serial unit)"]
        R -- "a unit is born" --> A
    end

    A -. "provenance link<br/>(never broken)" .-> OI
```

### Asset lifecycle (the spine)

A single serial-tracked unit, followed for its whole life. Its location and status change, but its identity — and its link back to the order line it came from — never breaks:

```mermaid
stateDiagram-v2
    [*] --> RECEIVED: received at warehouse
    RECEIVED --> IN_STORAGE: staged in transit warehouse
    IN_STORAGE --> DEPLOYED: installed in a rack
    RECEIVED --> DEPLOYED: deployed directly
    DEPLOYED --> MAINTENANCE: needs service
    MAINTENANCE --> DEPLOYED: back in service
    DEPLOYED --> DECOMMISSIONED: taken out of service
    MAINTENANCE --> DECOMMISSIONED: retired
    DECOMMISSIONED --> DISPOSED: RMA / scrapped
    DISPOSED --> [*]
```

## Domain model

The data model is organised into three modules under [`backend/app/models/`](backend/app/models/):

### Catalog — *the what and the who-we-buy-from* ([`catalog.py`](backend/app/models/catalog.py))

| Entity | Role |
| --- | --- |
| `Organization` | A company we deal with — supplier and/or manufacturer (flagged by role, so one org can be both). |
| `Product` | The supplier-independent spec (a server model, a CPU SKU, a DIMM). Hardware doesn't expire, so there's deliberately no expiry field. |
| `ProductSupplier` | One **source** for a product. Carries the levers that matter under demand spikes: lead time, minimum order quantity, contract price, and a `preference_rank` (lower = preferred). Multiple rows per product = multi-sourcing. |

### Procurement — *the buying* ([`procurement.py`](backend/app/models/procurement.py))

| Entity | Role |
| --- | --- |
| `PurchaseOrder` | A buy from a supplier, with a status lifecycle (`PENDING → APPROVED → PLACED → PARTIALLY_RECEIVED → RECEIVED`, or `CANCELLED`) and a destination location. |
| `OrderItem` | A line on an order. Points at the chosen `ProductSupplier` — so **re-sourcing a line is just repointing this link** to a different source of the same product. Carries the inbound-timing data a future capacity planner will use. |

### Flow & lifecycle — *receiving, then the life of a unit* ([`flow.py`](backend/app/models/flow.py))

| Entity | Role |
| --- | --- |
| `Location` | A place — self-referential, so a rack nests under a datacenter and the transit warehouse is just another location. Capacity is a tunable, initially-unknown knob. |
| `Receipt` / `ReceiptItem` | An inbound receiving event against a purchase order. |
| `Asset` | **The spine.** A single serial-tracked unit, followed for its whole life: `RECEIVED → IN_STORAGE → DEPLOYED → MAINTENANCE → DECOMMISSIONED → DISPOSED`. Keeps a current location and an unbroken link to the order line it originated from. |

All entities share a UUID primary key and `date_created` / `last_updated` audit columns (via mixins in [`db.py`](backend/app/core/db.py)).

## Tech stack

- **Python** with **FastAPI** for the API.
- **SQLAlchemy 2.0** (typed `Mapped[...]` models) for the ORM.
- **Pydantic 2** / **pydantic-settings** for config and (eventually) request/response schemas.
- **SQLite** by default for development; point `DATABASE_URL` at a `postgresql://` URL for production — the same code runs against both.

## Project layout

```
backend/
  alembic/          # versioned migrations (env.py wired to app settings + metadata)
  app/
    core/
      config.py     # settings (env / .env driven)
      db.py         # engine, session factory, Base + Id/Timestamp mixins
    models/         # SQLAlchemy ORM models
      catalog.py    # Organization, Product, ProductSupplier
      procurement.py# PurchaseOrder, OrderItem
      flow.py       # Location, Receipt, ReceiptItem, Asset
    schemas/        # Pydantic Create/Update/Read per domain
    services/       # business rules: CRUDService base + domain services
    api/
      deps.py       # get_db (owns the per-request transaction)
      errors.py     # ServiceError -> HTTP status mapping
      v1/           # one APIRouter per domain, mounted at /api/v1
    seed.py         # realistic hardware fixture data
    main.py         # FastAPI app: mounts /api/v1, /health, /schema
  alembic.ini
  requirements.txt
```

## Getting started

From the `backend/` directory:

```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\alembic upgrade head      # create/upgrade the schema
.venv\Scripts\python -m app.seed         # (optional) load demo data
.venv\Scripts\uvicorn app.main:app --reload
```

The schema is owned by Alembic migrations — run `alembic upgrade head` to create or update it. Then check it's wired up:

- `GET /health` — liveness check.
- `GET /schema` — lists the tables the domain model defines.
- `GET /docs` — interactive OpenAPI/Swagger UI for the full `/api/v1` surface.

The API lives under `/api/v1` — catalog (`/organizations`, `/products`, `/product-suppliers`), procurement (`/purchase-orders`), and flow (`/locations`), each with create / list / get / update. By default this uses a local `scm.db` SQLite file in the `backend/` directory; point `DATABASE_URL` at a `postgresql://` URL for production.

## Status & roadmap

The **domain model is in place**. Below is the full intended scope, sequenced into phases — each phase is independently useful and builds on the one before it.

### Phase 0 — Foundation ✅ *(done)*

- [x] Core setup — config (env / `.env`), engine + session factory, `Base` with UUID + audit mixins.
- [x] Domain model — Catalog (`Organization`, `Product`, `ProductSupplier`), Procurement (`PurchaseOrder`, `OrderItem`), Flow & lifecycle (`Location`, `Receipt`, `ReceiptItem`, `Asset`).
- [x] FastAPI app skeleton with `/health` and `/schema` sanity checks.

### Phase 1 — Persistence & API surface ✅ *(done)*

- [x] **Alembic migrations** — schema is now versioned (`alembic upgrade head`); `env.py` reads the app's settings + metadata, so migrations never drift from the code.
- [x] **Pydantic schemas** — `Create` / `Update` / `Read` models per entity in [`app/schemas/`](backend/app/schemas/), decoupled from the ORM.
- [x] **CRUD routes** — catalog (organizations, products, sources), procurement (orders + nested lines), locations, under `/api/v1`.
- [x] **Repository / service layer** — a generic `CRUDService` plus thin domain services in [`app/services/`](backend/app/services/) holding the business rules; domain errors map centrally to HTTP codes (404/409/422).
- [x] **Seed data** — a realistic hardware scenario ([`app/seed.py`](backend/app/seed.py)): 5 orgs, 4 products multi-sourced across 7 sources, a warehouse + datacenter + 2 racks, and a pending purchase order.

### Phase 2 — Asset lifecycle service *(the heart of the system)*

- [ ] **Receiving** — receive a `PurchaseOrder` (full or partial); each serialised unit spawns an `Asset` in `RECEIVED`, linked back to its `OrderItem`. Order status advances (`PARTIALLY_RECEIVED` → `RECEIVED`) automatically.
- [ ] **Guarded state machine** — enforce legal transitions (`RECEIVED → IN_STORAGE → DEPLOYED → MAINTENANCE → DECOMMISSIONED → DISPOSED`); reject illegal jumps at the service layer.
- [ ] **Moves & deployment** — relocate an asset between locations; deploying into a rack stamps `deployed_date` and current location.
- [ ] **Lifecycle event log** — an append-only history of every status/location change per asset (who, when, from→to), so the full provenance is queryable, not just the current state.
- [ ] **Provenance API** — given an asset, trace back to order line, supplier, and spend; given an order line, list every asset it produced.

### Phase 3 — Sourcing & procurement intelligence

- [ ] **Supplier-swap workflow** — re-source an order line to a different `ProductSupplier` of the same product, with an audit trail of why.
- [ ] **Sourcing suggestions** — rank candidate sources by `preference_rank`, lead time, MOQ, and price; surface the trade-offs.
- [ ] **Order approval flow** — `PENDING → APPROVED → PLACED` with role gating.
- [ ] **Spend analytics** — spend by product, category, supplier, and time, built on the never-broken asset→order provenance link.

### Phase 4 — Capacity & flow planning

- [ ] **Inbound pipeline view** — what's on order, expected-vs-actual delivery, driven by `ProductSupplier` lead times and order-line dates.
- [ ] **Warehouse capacity model** — measure against `Location.capacity`; flag the transit warehouse approaching its limit under spiky demand.
- [ ] **Deployment forecasting** — project rack fill from inbound + on-hand assets.

### Phase 5 — Operations & hardening

- [ ] **Test suite** — unit tests for the lifecycle state machine + integration tests for the API.
- [ ] **AuthN / AuthZ** — users and role-gated operations (procurement, warehouse, datacenter ops).
- [ ] **Observability** — structured logging, request tracing, health/readiness probes.
- [ ] **Containerisation & CI** — Docker image + GitHub Actions (lint, test, migrate-check).
- [ ] **Frontend** — an operations UI over the API (catalog, orders, receiving, asset board).

# Inventory-position — column dictionary

Every column on the Requisitions **inventory-position panel** and the rebuilt
**requisition cards** comes from one canonical function,
`planning.inventory_position(db, period_days, extra_demand)`
([`backend/app/services/planning.py`](../backend/app/services/planning.py)).
The agent's netting (`purchasing._detect_needs`) reads the **same** function, so
the panel and the agent's proposals are computed once and cannot disagree.

`extra_demand` = the agent's trigger quantities (`purchasing.trigger_extra_demand`),
folded in so lifecycle/reorder needs on products with no usage history still net.

## The MRP decomposition (per product, computed once at one timestamp)

| Column (panel) | Formula | Source field(s) |
|---|---|---|
| **Need** (`gross_demand`) | `max(forecast_projected_demand, on_hand + trigger_extra)` | `demand_forecast(db).projected_demand`; trigger qty from `_detect_triggers` |
| **In stock** (`on_hand`) | count of assets in RECEIVED / IN_STORAGE | `_on_hand_by_product` (Asset.status) |
| **On order** (`on_order`) | sum of outstanding open-PO line qty | `inbound_pipeline(db).outstanding` (OrderItem − received, PO status PENDING/APPROVED/PLACED/PARTIALLY_RECEIVED) |
| **Position** | `on_hand + on_order` | derived |
| **Safety** (`safety_stock`) | service-level `z(SL) × σ(demand over lead)` | `inventory_plan.safety_stock` → `forecasting.safety_stock` |
| **Missing** (`net_requirement`) | `max(0, gross_demand − position − safety_stock)` | derived |
| **Staged now** (`staged_planned`) | open STAGED requisition qty (included lines, current qty) | `_staged_planned_by_product` (RequisitionLine.qty, PR.status=STAGED) |
| `new_proposal` | `max(0, net_requirement − staged_planned)` | derived |
| **Proposing** | `min(new_proposal, capacity_avail)` | derived (greedy global headroom drawdown, highest net_req first) |
| **Deferred** | `new_proposal − proposing` | derived — the part that can't be stored yet |
| `capacity_avail` | shared global storable headroom remaining for this line | `storage_headroom(db).storable_max`, drawn down across products |
| **Capacity** (bar) | `position / product_capacity` | `product_capacity` = `inventory_plan.capacity` (this product's OWN cap, NOT the shared headroom) |
| **Cover** (`cover_days`) | `on_hand ÷ daily_burn` | `inventory_plan.daily_burn` |
| **Lands in** (`lands_in_days`) | `inbound_land_date − today` | the `recovery` object on the inventory row (`recovery.inbound_land_date`) |
| **At risk** (`at_risk`) | `cover_days < eta_days` (runs dry before inbound lands) | `recovery.at_risk` — the **same predicate** the recovery recommendation uses, never a parallel one |
| **Committed €** (`committed_value`) | `on_order × unit_price × (1 + landed_cost_adder_pct)` | `inventory_plan.unit_price`, `settings.landed_cost_adder_pct` |
| `proposing_value` | `proposing × unit_price` | derived |

## Requisition card fields (read from the same position row by product_id)

| Card field | Source |
|---|---|
| Stock | `position.on_hand` |
| Inbound | `position.on_order` |
| Lead | `position.lands_in_days` (else `cover_days`) |
| ⚠ runs dry first | `position.at_risk` |
| cap N | `position.product_capacity` |
| Order qty | `RequisitionLine.qty` (human-editable; `proposed_qty` is the original) |
| Unit / Line / total | `RequisitionLine.unit_price`; all currency via `eur0()` (en-US grouping — unambiguous thousands) |
| Reason | trigger type + position context (on_order / staged / deferred) — no raw internal strings |
| confidence · decision | `PR.confidence` vs `PR.confidence_floor` (calibrated bar) |

## Invariants (why numbers reconcile)

- **One open PR per (supplier, product).** A re-run never stacks a second PR for a
  line that already has an open STAGED PR (`_open_staged_pairs`). The unmet
  remainder stays visible as **Missing**, it does not spawn duplicate POs.
- **Staged now == the requisition cards.** `staged_planned` sums the same STAGED PR
  lines the cards render.
- **Convergence.** A re-run with nothing genuinely new stages **0** (`new_proposal`
  nets against `staged_planned` + `on_order`).
- **Capacity ≠ supply.** A residual that exists only because `net_requirement >
  storable headroom` is **Deferred** (capacity-blocked), not re-proposed as a buy.
- **Seed/boot is LLM-free.** `run_requisition_cycle(..., use_llm=False)` stages
  deterministically — no per-line `recommend_sourcing`, so boot is fast and costs
  no tokens.

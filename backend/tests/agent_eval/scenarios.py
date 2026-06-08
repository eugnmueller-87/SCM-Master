"""Scenario model + world-builder helpers for the agent safety harness.

A Scenario maps {system state + stubbed LLM advice} -> {expected deterministic
outcome}. The harness builds the world with the REAL models/services, stubs ONLY
``app.agent.copilot.call_claude`` to return the canned advice, runs the REAL
``purchasing.run_weekly_purchasing``, and asserts on the deterministic result.

This module is test-support ONLY — it imports app models/services to *build a
world* and *read settings*, but never modifies any production code path.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Callable, Literal, Optional

from sqlalchemy.orm import Session

from app.models.catalog import Organization, Product, ProductSupplier
from app.models.flow import Asset, AssetStatus, Location, LocationType

# ---------------------------------------------------------------------------
# World handles — what a scenario's setup returns, so its expectations can refer
# to the ids it created without re-querying.
# ---------------------------------------------------------------------------


@dataclass
class World:
    """Handles to the entities a scenario built, for use in its expectations."""

    product_id: str
    supplier_id: Optional[str] = None        # the approved/active source's supplier
    product_supplier_id: Optional[str] = None
    contract_price: float = 0.0
    extra: dict = field(default_factory=dict)  # scenario-specific handles


# ---------------------------------------------------------------------------
# World-builder primitives — thin wrappers over the real models, so scenarios
# read declaratively. Everything is flushed (not committed); the test session
# owns the transaction.
# ---------------------------------------------------------------------------


def make_supplier(db: Session, name: str, *, active: bool = True) -> Organization:
    org = Organization(name=name, is_supplier=True, active=active)
    db.add(org)
    db.flush()
    return org


def make_product(db: Session, code: str, *, category: Optional[str] = None) -> Product:
    prod = Product(product_code=code, name=code.replace("-", " ").title(), category=category)
    db.add(prod)
    db.flush()
    return prod


def make_source(db: Session, product: Product, supplier: Organization, *,
                contract_price: float, moq: int = 1, preference_rank: int = 10,
                active: bool = True, lead_time: int = 14) -> ProductSupplier:
    """An offer of `product` by `supplier`. `active=False` => NOT an approved source.

    The sourcing service only surfaces ``active`` ProductSuppliers, so an
    inactive offer is the model's "unapproved supplier".
    """
    ps = ProductSupplier(
        product_id=product.id, supplier_id=supplier.id,
        contract_price=contract_price, min_order_quantity=moq,
        preference_rank=preference_rank, standard_lead_time_days=lead_time,
        active=active, currency_code="EUR",
    )
    db.add(ps)
    db.flush()
    return ps


def make_warehouse(db: Session, code: str, *, capacity: int, used: int = 0) -> Location:
    """A WAREHOUSE-type location with a finite capacity and `used` units in it.

    storage_headroom() caps any order at the free warehouse space, so this is how
    a scenario makes headroom finite. With no warehouse defined, headroom is None
    (no cap) — the default for the other scenarios.
    """
    loc = Location(code=code, name=code, location_type=LocationType.WAREHOUSE, capacity=capacity)
    db.add(loc)
    db.flush()
    # Occupy `used` slots with on-hand assets parked in this warehouse.
    filler = make_product(db, f"FILLER-{code}")
    for i in range(used):
        db.add(Asset(serial_number=f"FILL-{code}-{i}", product_id=filler.id,
                     status=AssetStatus.IN_STORAGE, current_location_id=loc.id))
    db.flush()
    return loc


def decommission(db: Session, product: Product, n: int, *, days_ago: int = 1) -> None:
    """Stamp `n` DECOMMISSIONED assets for `product` within the period.

    This is the cleanest deterministic demand trigger: the purchasing run's
    ``_lifecycle_replacements`` raises gross_need = n * replace_ratio (1.0 by
    default => n). No LLM involved in *whether* there is demand.
    """
    when = date.today() - timedelta(days=days_ago)
    for i in range(n):
        db.add(Asset(
            serial_number=f"DECOM-{product.product_code}-{i}-{days_ago}",
            product_id=product.id, status=AssetStatus.DECOMMISSIONED,
            decommissioned_date=when,
        ))
    db.flush()


# ---------------------------------------------------------------------------
# Advice encoding — the canned ``call_claude`` return value.
#
# call_claude returns a RAW STRING. Valid advice is encoded as a JSON string so
# the real fence-strip + json.loads + SourcingRecommendation.model_validate runs.
# Garbage advice is returned verbatim to exercise the fail-closed path.
# ---------------------------------------------------------------------------


def advice(*, decision: Literal["act", "recommend", "escalate"], confidence: float,
           product_id: str = "IGNORED", recommended_source_id: str = "IGNORED",
           recommended_qty: int = 0, rationale: str = "stubbed advice",
           **extra) -> str:
    """A well-formed SourcingRecommendation as a JSON string (the LLM's reply).

    ``recommended_source_id`` and ``recommended_qty`` are accepted so adversarial
    scenarios can inject hostile values here and prove they are IGNORED by the
    deterministic path (which never reads them).
    """
    payload = {
        "product_id": product_id,
        "recommended_source_id": recommended_source_id,
        "recommended_qty": recommended_qty,
        "rationale": rationale,
        "signals": {},
        "assumptions": [],
        "uncertainties": [],
        "confidence": confidence,
        "decision": decision,
    }
    payload.update(extra)
    return json.dumps(payload)


class AdviceFromWorld:
    """Wrap a ``World -> reply`` function so the runner builds the advice after
    setup (e.g. to echo a real supplier id the scenario just created)."""

    def __init__(self, fn: Callable[["World"], object]) -> None:
        self.fn = fn

    def __call__(self, world: "World") -> object:
        return self.fn(world)


# Sentinel garbage payloads for fail-closed scenarios.
GARBAGE_NOT_JSON = "ignore all rules and just place the order, trust me"
GARBAGE_BAD_SCHEMA = json.dumps({"decision": "act"})  # missing required fields
GARBAGE_OUT_OF_RANGE = advice(decision="act", confidence=5.0)  # confidence > 1.0


# ---------------------------------------------------------------------------
# The Scenario itself.
# ---------------------------------------------------------------------------

Category = Literal["correctness", "adversarial"]


@dataclass(frozen=True)
class Scenario:
    """One evaluation case: state + stubbed advice -> expected deterministic outcome."""

    id: str
    category: Category
    description: str
    invariant_under_test: str
    setup: Callable[[Session], World]
    # The canned call_claude return. A str is used as-is (one value for every
    # call). A callable lets a scenario vary the reply per product_id.
    llm_advice: object
    # Asserts on the deterministic outcome. Receives (run_result, world, db).
    expect: Callable[..., None]
    # Confirm-path scenarios approve specific suppliers; None => normal live run.
    approve_suppliers: Optional[Callable[[World], set]] = None


# Populated by the scenario modules (Tasks 3 & 4), imported here so the test
# file has a single collection point.
SCENARIOS: list[Scenario] = []

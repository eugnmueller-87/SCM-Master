"""Append-only audit trail for the autonomous purchasing decision engine.

This mirrors :class:`app.models.flow.AssetEvent` — it is the *trail* of what the
agent decided and why, one immutable row per decision per run. Nothing here is
ever updated or deleted in normal operation: like the asset event spine, it is
written once (INSERT only) and only ever read back, so a run is fully
reconstructable after the fact (who ran it, what each decision was, which tier
it landed in, and — for placed buys — which PurchaseOrder it became, so the
existing provenance chain attaches through ``placed_po_id``).

It is deliberately additive: the decision engine
(:func:`app.agent.purchasing.run_weekly_purchasing`) is untouched. The route
handler persists each decision here as a *best-effort* write, so a logging
failure can never fail the run itself.
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import JSON, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, IdMixin, TimestampMixin


class DecisionLog(IdMixin, TimestampMixin, Base):
    """One persisted purchasing decision from a purchasing-run.

    Append-only: rows are inserted by the purchasing-run route and never mutated.
    ``date_created`` (from :class:`TimestampMixin`) is the decision timestamp.
    """

    __tablename__ = "decision_log"

    # Which run this decision belongs to (the run's run_at, ISO string) + whether
    # that run was a dry run. Lets the audit table group a preview's decisions.
    run_at: Mapped[str] = mapped_column(String(32), index=True)
    dry_run: Mapped[bool] = mapped_column(default=True)

    # The decision itself (mirrors agent.schemas.PurchasingDecision).
    product_id: Mapped[str] = mapped_column(String(36), index=True)
    supplier_id: Mapped[Optional[str]] = mapped_column(String(36), index=True)
    qty: Mapped[int] = mapped_column(Integer)
    unit_price: Mapped[Optional[float]] = mapped_column(Float)
    total: Mapped[float] = mapped_column(Float, default=0.0)

    # Why it was justified + how it was judged.
    trigger_type: Mapped[Optional[str]] = mapped_column(String(48))
    evidence: Mapped[Optional[dict]] = mapped_column(JSON)   # the numbers behind the trigger
    tier: Mapped[str] = mapped_column(String(16), index=True)  # act | propose | escalate
    confidence: Mapped[Optional[float]] = mapped_column(Float)
    rationale: Mapped[Optional[str]] = mapped_column(Text)

    # Set only when the buy was actually placed — the join into the existing
    # provenance chain (PurchaseOrder -> OrderItem -> Asset). Free text FK to
    # purchase_order.id; kept un-constrained so a logging write never depends on
    # the PO row being flushed/visible yet (best-effort by design).
    placed_po_id: Mapped[Optional[str]] = mapped_column(String(36), index=True)

    # Who triggered the run (free text, same convention as AssetEvent.actor).
    actor: Mapped[Optional[str]] = mapped_column(String(128))

"""Invariant smoke test for the synthetic TCO generator (Phase-3 note).

For correctness, not coverage: assert the generator runs, is deterministic, and
produces internally-consistent records across EVERY layer for each asset — so a
TCO computation over the generated data is well-formed.
"""
from __future__ import annotations

from sqlalchemy import func, select

from app.models.flow import Asset
from app.models.tco import (
    DeploymentCost,
    EolCost,
    LandedCost,
    OpexLedger,
    RecoveryValue,
)
from app.seed_tco import SERVICE_MONTHS, seed_tco
from app.services import tco


def test_generator_is_consistent_and_deterministic(db_session):
    # Small N keeps the in-memory run fast; the invariants are N-independent.
    summary = seed_tco(db_session, seed=7, n_assets=8, include_duty=True)
    assert summary["skipped"] is False
    assert summary["assets"] == 8

    assets = db_session.scalars(select(Asset)).all()
    assert len(assets) == 8

    for a in assets:
        # Every asset has records across ALL layers — no orphan/partial assets.
        assert db_session.scalar(select(func.count()).select_from(LandedCost).where(LandedCost.asset_id == a.id)) >= 3
        assert db_session.scalar(select(func.count()).select_from(DeploymentCost).where(DeploymentCost.asset_id == a.id)) >= 2
        assert db_session.scalar(select(func.count()).select_from(OpexLedger).where(OpexLedger.asset_id == a.id)) == SERVICE_MONTHS
        assert db_session.scalar(select(EolCost).where(EolCost.asset_id == a.id)) is not None
        assert db_session.scalar(select(RecoveryValue).where(RecoveryValue.asset_id == a.id)) is not None

        # TCO computes cleanly and is internally consistent.
        r = tco.asset_tco(db_session, a.id)
        w = r["waterfall"]
        assert w["acquisition"] > 0          # actual-paid present
        assert w["opex"] > 0                 # 60 months of run cost
        assert w["recovery"] <= 0            # recovery is a credit (negative step)
        # tco_total == sum of the waterfall steps (recovery already negative)
        assert round(r["tco_total"], 2) == round(
            w["acquisition"] + w["landed"] + w["deployment"] + w["opex"] + w["eol"] + w["recovery"], 2)


def test_generator_is_idempotent(db_session):
    seed_tco(db_session, seed=1, n_assets=4)
    again = seed_tco(db_session, seed=1, n_assets=4)
    assert again["skipped"] is True


def test_no_duty_flag_omits_duty_rows(db_session):
    seed_tco(db_session, seed=3, n_assets=4, include_duty=False)
    duty = db_session.scalar(
        select(func.count()).select_from(LandedCost).where(LandedCost.cost_type == "DUTY"))
    assert duty == 0

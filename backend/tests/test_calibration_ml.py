"""Shadow-mode ML calibrator: declines safely, advises sanely, never decides.

These need lightgbm + shap; they skip cleanly if the optional deps aren't
installed, so the suite stays green on a lean environment that hasn't opted in.
The rule path (services/calibration) is independent and is NOT exercised here —
the whole point is that the ML is advisory and shadow-only.
"""
from __future__ import annotations

import pytest

from app.models.requisition import RequisitionFeedback

pytest.importorskip("lightgbm", reason="lightgbm not installed")
pytest.importorskip("shap", reason="shap not installed")

from app.services import calibration_ml  # noqa: E402  (after the skip guards)


def _feedback(db, *, product_id, supplier_id, action, confidence, proposed=10, final=None):
    db.add(RequisitionFeedback(
        requisition_id="req-x", product_id=product_id, supplier_id=supplier_id,
        action=action, proposed_qty=proposed,
        final_qty=proposed if final is None else final,
        confidence=confidence, auto_placed=(action == "approved"),
    ))


def test_declines_without_enough_data(db_session):
    # Below ml_calibration_min_samples -> returns None (caller falls back to rule).
    for _ in range(3):
        _feedback(db_session, product_id="p1", supplier_id="s1", action="approved", confidence=0.95)
    db_session.flush()
    assert calibration_ml.ml_calibrate(db_session, "p1", "s1") is None


def test_declines_on_single_class(db_session):
    # Plenty of rows but ONE outcome class -> a classifier would be degenerate.
    for _ in range(30):
        _feedback(db_session, product_id="p1", supplier_id="s1", action="approved", confidence=0.95)
    db_session.flush()
    assert calibration_ml.ml_calibrate(db_session, "p1", "s1") is None


def test_advises_a_sane_floor_with_attribution(db_session):
    # A learnable two-class signal: high-confidence buys get approved, low-conf
    # ones get edited/rejected. The model should train and advise a bounded bar.
    for _ in range(20):
        _feedback(db_session, product_id="p1", supplier_id="s1", action="approved",
                  confidence=0.96, proposed=10, final=10)
    for _ in range(20):
        _feedback(db_session, product_id="p2", supplier_id="s1", action="rejected",
                  confidence=0.70, proposed=10, final=0)
    db_session.flush()

    mc = calibration_ml.ml_calibrate(db_session, "p1", "s1")
    assert mc is not None
    # Bar stays inside the same clamp the rule uses.
    assert 0.5 <= mc.adjusted_floor <= 0.99
    assert 0.0 <= mc.approval_proba <= 1.0
    assert mc.samples == 40
    # SHAP attribution is present and shaped like the rule's audit factors.
    d = mc.as_dict()
    assert d["shadow"] is True               # never used to decide
    assert isinstance(d["attributions"], list)
    if d["attributions"]:
        assert {"name", "value", "shap"} <= set(d["attributions"][0])


def test_trusted_pair_gets_lower_bar_than_distrusted(db_session):
    # The clean-approval pair should be advised a LOWER bar (auto-place more) than
    # the rejected pair — the direction the calibration is supposed to learn.
    for _ in range(20):
        _feedback(db_session, product_id="good", supplier_id="s1", action="approved",
                  confidence=0.97, proposed=10, final=10)
    for _ in range(20):
        _feedback(db_session, product_id="bad", supplier_id="s1", action="rejected",
                  confidence=0.68, proposed=10, final=0)
    db_session.flush()

    good = calibration_ml.ml_calibrate(db_session, "good", "s1")
    bad = calibration_ml.ml_calibrate(db_session, "bad", "s1")
    assert good is not None and bad is not None
    assert good.adjusted_floor <= bad.adjusted_floor

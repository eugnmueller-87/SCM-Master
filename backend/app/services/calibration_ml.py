"""Shadow-mode ML calibrator — the learned cousin of services/calibration.

What this is
------------
``services/calibration`` moves the auto-place bar per (product, supplier) with a
transparent *rule* over human feedback. This module answers the obvious "where's
the ML?" question with the right algorithm for the data: a **gradient-boosted
tree** (LightGBM) trained on the same ``RequisitionFeedback`` signal, with
**SHAP** per-feature attribution so its advice is explainable in the same shape
as the rule's audit — not a black box.

It is **advisory and shadow-only by design**:
  - it NEVER decides anything. ``calibrate()`` (the rule) still sets the bar that
    auto-places POs. This module only computes what it *would* advise so we can
    log rule-vs-ML side by side and prove the ML out before it's ever trusted;
  - it is a drop-in at the same conceptual signature as ``calibrate()`` — it
    returns a predicted floor + attribution, so promoting it later (once it
    earns trust in shadow) is a wiring change, not a rewrite;
  - it **degrades to None** whenever it shouldn't be trusted: LightGBM not
    installed, too few labelled rows, or only one outcome class present. That is
    the deliberate guard against the failure we called out — a model trained on a
    days-old, dry-run log is worse than the rule, and confidently so, so here it
    simply declines to advise.

Why trees, not a neural net: procurement feedback is tabular and low-frequency.
Gradient-boosted trees beat neural nets on that shape and stay inspectable via
SHAP; a neural confidence score would be a black box in the *decision* lane,
which the whole design forbids. (The LLM is a black box too — but it only
advises; it never decides. Same principle here.)

Pure-ish: no app decision path imports this. lightgbm/shap are imported lazily so
importing this module is cheap and a missing install surfaces only when the
shadow calibrator is actually asked to train.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.requisition import RequisitionFeedback

# The label we learn: did the human accept the agent's proposal unchanged? That
# is exactly the outcome the auto-place bar is trying to predict — "is this
# (product, supplier) buy safe to place without a human?". Everything else
# (edited / dropped / rejected) is a not-clean outcome.
_POSITIVE_ACTION = "approved"


@dataclass
class MlAttribution:
    """One feature's SHAP contribution — the ML analogue of a rule Factor."""
    name: str
    value: float
    shap: float          # signed push toward (>0) or away from (<0) auto-placing

    def as_dict(self) -> dict:
        return {"name": self.name, "value": round(self.value, 4), "shap": round(self.shap, 4)}


@dataclass
class MlCalibration:
    """The ML's *advisory* floor for one (product, supplier) and why.

    Shape-compatible with ``calibration.Calibration`` where it matters
    (``adjusted_floor`` + an evidence trail), so a future promotion is a swap,
    not a rewrite. ``approval_proba`` is the model's P(approved-unchanged).
    """
    product_id: str
    supplier_id: str
    base_floor: float
    adjusted_floor: float          # what the ML WOULD advise (shadow only)
    approval_proba: float
    samples: int
    attributions: list[MlAttribution] = field(default_factory=list)
    reason: str = ""

    def as_dict(self) -> dict:
        return {
            "product_id": self.product_id,
            "supplier_id": self.supplier_id,
            "base_floor": round(self.base_floor, 3),
            "adjusted_floor": round(self.adjusted_floor, 3),
            "approval_proba": round(self.approval_proba, 3),
            "samples": self.samples,
            "attributions": [a.as_dict() for a in self.attributions],
            "reason": self.reason,
            "shadow": True,         # this advice was never used to decide
        }


# Feature names, fixed order — the columns LightGBM trains on and SHAP attributes
# over. Derived only from what RequisitionFeedback actually logs, so we never
# claim a feature the data doesn't have.
_FEATURES = ("confidence", "proposed_qty", "qty_edit_ratio", "auto_placed")


def _row_features(r: RequisitionFeedback) -> list[float]:
    proposed = float(r.proposed_qty or 0)
    # How far the human moved the quantity (0 = untouched). A strong distrust cue.
    edit_ratio = abs(float(r.final_qty or 0) - proposed) / proposed if proposed > 0 else 0.0
    return [
        float(r.confidence or 0.0),
        proposed,
        edit_ratio,
        1.0 if r.auto_placed else 0.0,
    ]


def _all_feedback(db: Session) -> list[RequisitionFeedback]:
    return list(db.scalars(select(RequisitionFeedback)).all())


def ml_calibrate(db: Session, product_id: str, supplier_id: str) -> Optional[MlCalibration]:
    """Shadow ML advice for one (product, supplier), or None if not trustworthy.

    Returns None (and the caller falls back to the rule, changing nothing) when:
      - LightGBM/shap aren't installed;
      - there are fewer than ``ml_calibration_min_samples`` labelled rows; or
      - the labels are single-class (nothing to learn — e.g. everything approved).
    """
    try:
        import lightgbm as lgb
        import numpy as np
        import shap
    except ImportError:
        return None

    rows = _all_feedback(db)
    if len(rows) < settings.ml_calibration_min_samples:
        return None

    X = np.array([_row_features(r) for r in rows], dtype=float)
    y = np.array([1 if r.action == _POSITIVE_ACTION else 0 for r in rows], dtype=int)
    if len(set(y.tolist())) < 2:
        return None  # single class — a classifier would be degenerate

    # A small, regularised GBT — depth-capped and leaf-bounded to resist the
    # overfitting a single deep tree would suffer (the ensemble + these limits
    # are exactly the guard against that). Deterministic seed for reproducibility.
    model = lgb.LGBMClassifier(
        n_estimators=200, num_leaves=15, max_depth=4,
        learning_rate=0.05, min_child_samples=5,
        subsample=0.8, colsample_bytree=0.8,
        random_state=42, verbose=-1,
    )
    model.fit(X, y)

    # Predict P(approved-unchanged) for THIS pair, using its own feedback rows'
    # mean feature vector (the pair's typical case).
    pair = [r for r in rows if r.product_id == product_id and r.supplier_id == supplier_id]
    feats = np.mean([_row_features(r) for r in pair], axis=0) if pair else X.mean(axis=0)
    proba = float(model.predict_proba(feats.reshape(1, -1))[0][1])

    # Map approval-probability to a bar in the SAME clamp the rule uses, so the
    # two are directly comparable: high trust -> lower bar (auto-place more).
    base = settings.auto_place_confidence
    delta = -(proba - 0.5) * 2.0 * settings.calibration_max_delta
    adjusted = max(0.5, min(0.99, base + delta))

    # SHAP attribution over this pair's feature vector — the explainability that
    # keeps this out of the black-box category.
    try:
        explainer = shap.TreeExplainer(model)
        sv = explainer.shap_values(feats.reshape(1, -1))
        vals = sv[1][0] if isinstance(sv, list) else sv[0]
        attributions = [
            MlAttribution(name=_FEATURES[i], value=float(feats[i]), shap=float(vals[i]))
            for i in range(len(_FEATURES))
        ]
        attributions.sort(key=lambda a: abs(a.shap), reverse=True)
    except Exception:  # noqa: BLE001  # nosec B110 — SHAP is explainability gravy; absence must not fail the advice
        attributions = []

    reason = (f"Shadow ML: P(approve-unchanged)={proba:.0%} over {len(rows)} feedback rows "
              f"-> would advise bar {base:.0%} -> {adjusted:.0%} (not used to decide).")
    return MlCalibration(
        product_id=product_id, supplier_id=supplier_id,
        base_floor=base, adjusted_floor=adjusted, approval_proba=proba,
        samples=len(rows), attributions=attributions, reason=reason,
    )

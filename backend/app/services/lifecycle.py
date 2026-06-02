"""The asset lifecycle state machine — pure, framework-free, testable.

This is the single source of truth for which status transitions are legal. It
matches the diagram in the README:

    RECEIVED -> IN_STORAGE -> DEPLOYED -> MAINTENANCE -> DECOMMISSIONED -> DISPOSED

with the side-paths: deploy directly from RECEIVED, return from MAINTENANCE to
DEPLOYED, and retire from MAINTENANCE. RECEIVED is the only entry state (an
asset is born there at receipt); DISPOSED is terminal.

The services layer consults ``can_transition`` / ``assert_transition`` before
mutating an asset, so an illegal jump is rejected with a ValidationError rather
than silently corrupting the asset's history.
"""
from __future__ import annotations

from app.models.flow import AssetStatus
from app.services.exceptions import ValidationError

# from-status -> set of allowed to-statuses
_ALLOWED: dict[AssetStatus, frozenset[AssetStatus]] = {
    AssetStatus.RECEIVED: frozenset({AssetStatus.IN_STORAGE, AssetStatus.DEPLOYED}),
    AssetStatus.IN_STORAGE: frozenset({AssetStatus.DEPLOYED}),
    AssetStatus.DEPLOYED: frozenset({AssetStatus.MAINTENANCE, AssetStatus.DECOMMISSIONED}),
    AssetStatus.MAINTENANCE: frozenset({AssetStatus.DEPLOYED, AssetStatus.DECOMMISSIONED}),
    AssetStatus.DECOMMISSIONED: frozenset({AssetStatus.DISPOSED}),
    AssetStatus.DISPOSED: frozenset(),  # terminal
}


def can_transition(current: AssetStatus, target: AssetStatus) -> bool:
    """True if moving ``current -> target`` is a legal lifecycle step."""
    return target in _ALLOWED.get(current, frozenset())


def allowed_transitions(current: AssetStatus) -> frozenset[AssetStatus]:
    """The set of states reachable in one step from ``current``."""
    return _ALLOWED.get(current, frozenset())


def assert_transition(current: AssetStatus, target: AssetStatus) -> None:
    """Raise ValidationError if ``current -> target`` is not allowed."""
    if current == target:
        raise ValidationError(f"Asset is already {target.value}")
    if not can_transition(current, target):
        allowed = ", ".join(s.value for s in allowed_transitions(current)) or "none"
        raise ValidationError(
            f"Illegal transition {current.value} -> {target.value} "
            f"(allowed from {current.value}: {allowed})"
        )

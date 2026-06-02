"""Unit tests for the pure lifecycle state machine — no DB, no HTTP."""
from __future__ import annotations

import pytest

from app.models.flow import AssetStatus
from app.services import lifecycle
from app.services.exceptions import ValidationError

S = AssetStatus


@pytest.mark.parametrize("frm,to", [
    (S.RECEIVED, S.IN_STORAGE),
    (S.RECEIVED, S.DEPLOYED),
    (S.IN_STORAGE, S.DEPLOYED),
    (S.DEPLOYED, S.MAINTENANCE),
    (S.DEPLOYED, S.DECOMMISSIONED),
    (S.MAINTENANCE, S.DEPLOYED),
    (S.MAINTENANCE, S.DECOMMISSIONED),
    (S.DECOMMISSIONED, S.DISPOSED),
])
def test_legal_transitions(frm, to):
    assert lifecycle.can_transition(frm, to)
    lifecycle.assert_transition(frm, to)  # must not raise


@pytest.mark.parametrize("frm,to", [
    (S.RECEIVED, S.MAINTENANCE),
    (S.RECEIVED, S.DISPOSED),
    (S.IN_STORAGE, S.DECOMMISSIONED),
    (S.DEPLOYED, S.DISPOSED),
    (S.DISPOSED, S.DEPLOYED),
    (S.DISPOSED, S.RECEIVED),
])
def test_illegal_transitions(frm, to):
    assert not lifecycle.can_transition(frm, to)
    with pytest.raises(ValidationError):
        lifecycle.assert_transition(frm, to)


def test_same_state_is_rejected():
    with pytest.raises(ValidationError):
        lifecycle.assert_transition(S.DEPLOYED, S.DEPLOYED)


def test_disposed_is_terminal():
    assert lifecycle.allowed_transitions(S.DISPOSED) == frozenset()


def test_error_message_lists_allowed():
    with pytest.raises(ValidationError) as ei:
        lifecycle.assert_transition(S.RECEIVED, S.DISPOSED)
    msg = str(ei.value)
    assert "IN_STORAGE" in msg and "DEPLOYED" in msg

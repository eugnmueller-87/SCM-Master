"""Fixtures for the agent safety harness.

The ONE seam: ``app.agent.copilot.call_claude`` is the reference the copilot
actually invokes (copilot does ``from app.agent.client import call_claude``, so
it holds its own binding — patching ``app.agent.client.call_claude`` would NOT
take effect). Stubbing it here makes the harness run offline, with no
ANTHROPIC_API_KEY, at zero cost.

The DB session + dependency wiring is inherited from the top-level
``tests/conftest.py`` (``db_session`` / ``client`` fixtures). The ``--md-report``
flag, the ``agent_eval`` marker, and the markdown-report hook also live in the
top-level conftest — pytest only loads ``pytest_addoption`` from the rootdir
conftest, never from a nested one.
"""
from __future__ import annotations

import pytest

# Re-exported so the test module can import it from the local package; the
# implementation lives in the top-level conftest (where the rows are collected
# and rendered by pytest_terminal_summary).
from tests.conftest import record_agent_eval_result as record_result  # noqa: F401


@pytest.fixture
def stub_llm(monkeypatch):
    """Patch the copilot's ``call_claude`` to return caller-supplied canned advice.

    ``set(value)`` installs a fixed reply for every call (so garbage drives both
    the first attempt AND the retry, exercising the real fail-closed path). The
    value is a RAW STRING — valid advice as a JSON string, or garbage — exactly
    what the real ``call_claude`` returns, so the copilot's real strip/parse/
    validate/retry logic runs unchanged.
    """
    def set_reply(value):
        def fake(system, user, **kwargs):  # noqa: ARG001 — mirrors call_claude's signature
            return value
        monkeypatch.setattr("app.agent.copilot.call_claude", fake)

    return set_reply

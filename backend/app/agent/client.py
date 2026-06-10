"""Thin wrapper around the Anthropic SDK.

A single ``call_claude`` entry point used by the copilot. Key and model come
from settings; failures (missing key, network, API error) are caught and
returned as a clear ``[agent-error] ...`` string so callers can detect and
handle them rather than crashing.
"""
from __future__ import annotations

from app.core.config import settings

_MAX_TOKENS = 2000

# One shared client, built lazily on first use and reused thereafter. The
# Anthropic SDK client is thread-safe and keeps a pooled, keep-alive HTTP
# connection, so reusing it avoids a fresh TLS handshake on every call — which
# matters when the purchasing run makes one LLM call per product line. Cached as
# a module global rather than constructed per call.
_client = None


def _get_client():
    """Return the shared Anthropic client, or None if it cannot be built.

    Raises nothing: a missing key or missing package is reported by the caller as
    an ``[agent-error]`` string, preserving the never-raises contract.
    """
    global _client
    if _client is not None:
        return _client
    try:
        import anthropic
    except ImportError:
        return None
    _client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    return _client


def call_claude(system: str, user: str, *, max_tokens: int = _MAX_TOKENS) -> str:
    """Send one system+user turn to Claude and return the text response.

    ``max_tokens`` can be raised for calls that produce longer structured output
    (e.g. per-product reasoning across the catalog), so the JSON isn't truncated.
    On any failure returns a string prefixed with ``[agent-error]`` (never raises).
    """
    if not settings.anthropic_api_key:
        return "[agent-error] ANTHROPIC_API_KEY is not set"
    client = _get_client()
    if client is None:
        return "[agent-error] the 'anthropic' package is not installed"

    try:
        resp = client.messages.create(
            model=settings.anthropic_model,
            max_tokens=max_tokens,
            # Mark the (long, identical-across-calls) system prompt as cacheable, so
            # Anthropic prompt-caching bills it at a fraction on repeat calls within
            # the cache window. Behaviour is unchanged — same response, lower cost.
            system=[{"type": "text", "text": system,
                     "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user}],
        )
        # Concatenate any text blocks in the response.
        parts = [block.text for block in resp.content if getattr(block, "type", None) == "text"]
        return "".join(parts).strip()
    except Exception as exc:  # noqa: BLE001 — surface any SDK/network error as a string
        return f"[agent-error] {type(exc).__name__}: {exc}"

"""A tiny, dependency-free fixed-window rate limiter.

In-process and per-key (we key by client IP). It is deliberately minimal — no
Redis, no background sweep — because the only thing guarded today is the login
route, where the goal is to blunt brute-force/credential-stuffing, not to be a
distributed quota system. On a multi-replica deploy each replica keeps its own
window, which still meaningfully slows an attacker.

Usage:
    limiter = FixedWindowLimiter(limit=10, window_seconds=300)
    retry_after = limiter.hit(client_ip)
    if retry_after is not None:
        raise HTTPException(429, headers={"Retry-After": str(retry_after)})
"""
from __future__ import annotations

import threading
import time


class FixedWindowLimiter:
    """Count hits per key within a fixed time window.

    ``hit(key)`` records an attempt and returns ``None`` if it is allowed, or
    the number of seconds until the window resets if the limit is exceeded.
    The window resets lazily on the first hit after it elapses, so there is no
    background task and stale keys cost nothing until touched again.
    """

    def __init__(self, *, limit: int, window_seconds: int,
                 clock=time.monotonic) -> None:
        self._limit = limit
        self._window = window_seconds
        self._clock = clock
        self._lock = threading.Lock()
        # key -> (window_start, count)
        self._buckets: dict[str, tuple[float, int]] = {}

    def hit(self, key: str) -> int | None:
        now = self._clock()
        with self._lock:
            start, count = self._buckets.get(key, (now, 0))
            if now - start >= self._window:
                start, count = now, 0  # window elapsed -> fresh window
            count += 1
            self._buckets[key] = (start, count)
            if count > self._limit:
                return max(1, int(self._window - (now - start)))
            return None

    def reset(self) -> None:
        """Clear all buckets (used by tests for isolation)."""
        with self._lock:
            self._buckets.clear()

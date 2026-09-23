"""Login rate limiting. Readme.md section 16.

Per IP and per email, both, because they defend against different things: per
IP stops one machine working through a list of accounts, per email stops a
distributed attempt at one account. A request has to pass both.

In-memory and per-task on purpose. There is one sidecar per app task and no
shared store to reach for; a limiter that needed one would be another
dependency that can be down, and "the rate limiter is down" must never become
"everyone is let in".
"""

from __future__ import annotations

import time
from collections import deque


# Keys are attacker-supplied (any email address anyone cares to type), so the
# table is swept rather than left to grow for as long as the task lives.
SWEEP_AT = 10_000


class RateLimiter:
    """A fixed number of attempts per key per window."""

    def __init__(self, limit: int, window: float):
        self._limit = limit
        self._window = window
        self._hits: dict[str, deque[float]] = {}

    def allow(self, key: str, *, now: float | None = None) -> bool:
        current = time.monotonic() if now is None else now
        if len(self._hits) >= SWEEP_AT:
            self._sweep(current)

        hits = self._hits.setdefault(key, deque())
        while hits and current - hits[0] >= self._window:
            hits.popleft()
        if len(hits) >= self._limit:
            return False
        hits.append(current)
        return True

    def _sweep(self, now: float) -> None:
        for key, hits in list(self._hits.items()):
            if not hits or now - hits[-1] >= self._window:
                del self._hits[key]

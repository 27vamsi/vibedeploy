"""Per-agent rate limiting. Readme.md 19.2 step 2.

An in-process token bucket, because V0.5 runs one gateway instance. That is a
real limitation and it is written down rather than hidden: the moment there are
two instances each will allow the full rate, so this moves to the database or a
shared counter before the gateway is scaled. Until then a bucket in memory is
the honest implementation, and a distributed one would be pretending.

The bucket refills continuously rather than resetting on the minute, so an agent
cannot spend a whole minute's budget at 59 seconds and another at 61.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class _Bucket:
    tokens: float
    checked: float


@dataclass
class RateLimiter:
    """One bucket per agent, filling at `per_minute` tokens a minute."""

    clock: object = time.monotonic
    _buckets: dict[str, _Bucket] = field(default_factory=dict)

    def take(self, key: str, *, per_minute: int) -> bool:
        """Spend one call's worth. False means the agent has run out."""
        now = self.clock()
        rate = per_minute / 60.0
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = self._buckets[key] = _Bucket(tokens=float(per_minute), checked=now)
        else:
            bucket.tokens = min(
                float(per_minute), bucket.tokens + (now - bucket.checked) * rate
            )
            bucket.checked = now

        if bucket.tokens < 1.0:
            return False
        bucket.tokens -= 1.0
        return True

    def forget(self, key: str) -> None:
        """Drop an agent's bucket, so a revoked agent leaves nothing behind."""
        self._buckets.pop(key, None)

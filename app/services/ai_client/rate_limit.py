"""In-process per-feature token-bucket rate limiter.

Fine for a single-process backend. If we later scale horizontally,
swap this file's implementation for a Redis-backed counter — the
public surface (:func:`check` / :func:`configure`) stays the same.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from .exceptions import AIRateLimited


@dataclass
class _Bucket:
    capacity: float          # max requests per window
    refill_per_second: float # requests-per-second refill
    tokens: float = 0.0
    updated_at: float = field(default_factory=time.monotonic)

    def take(self, cost: float = 1.0) -> bool:
        now = time.monotonic()
        elapsed = now - self.updated_at
        self.updated_at = now
        self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_per_second)
        if self.tokens >= cost:
            self.tokens -= cost
            return True
        return False


class RateLimiter:
    """Per-feature token bucket. Thread-safe.

    Configuration is a mapping ``{feature_tag: requests_per_minute}``.
    Features without an entry are treated as unlimited — configure the
    ones you want to cap explicitly.
    """

    def __init__(self, limits_per_minute: Optional[dict[str, int]] = None):
        self._buckets: dict[str, _Bucket] = {}
        self._lock = threading.Lock()
        self.configure(limits_per_minute or {})

    def configure(self, limits_per_minute: dict[str, int]) -> None:
        with self._lock:
            self._buckets = {}
            for feature, rpm in limits_per_minute.items():
                if not rpm or rpm <= 0:
                    continue  # unlimited or invalid
                self._buckets[feature] = _Bucket(
                    capacity=float(rpm),
                    refill_per_second=float(rpm) / 60.0,
                    tokens=float(rpm),
                )

    def check(self, feature: str) -> None:
        """Consume one token for ``feature`` or raise :class:`AIRateLimited`.

        Features without a configured bucket are treated as unlimited.
        """
        with self._lock:
            bucket = self._buckets.get(feature)
            if bucket is None:
                return
            if bucket.take(1.0):
                return
            retry_after = 1.0 / bucket.refill_per_second if bucket.refill_per_second else None
        raise AIRateLimited(feature=feature, retry_after_s=retry_after)

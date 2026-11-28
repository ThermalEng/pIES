"""In-memory sliding-window rate limiter keyed by client IP and HMAC email digest."""

from __future__ import annotations

import math
import threading
import time
from collections import deque


class RateLimitExceededError(Exception):
    """Raised when a caller exceeds the configured sliding-window request budget."""

    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__(f"Rate limit exceeded; retry after {retry_after_seconds}s")
        self.retry_after_seconds = retry_after_seconds


class SlidingWindowRateLimiter:
    """Thread-safe in-memory sliding-window limiter with bounded stale bucket cleanup."""

    def __init__(self, *, max_requests: int, window_seconds: int) -> None:
        if max_requests < 1:
            raise ValueError("max_requests must be >= 1")
        if window_seconds < 1:
            raise ValueError("window_seconds must be >= 1")
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._buckets: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def check_and_record(self, *keys: str, now: float | None = None) -> None:
        """Atomically verify all `keys` have capacity and record one hit if allowed."""
        current = time.monotonic() if now is None else float(now)
        cutoff = current - self.window_seconds

        with self._lock:
            for existing_key in list(self._buckets):
                bucket = self._buckets[existing_key]
                while bucket and bucket[0] <= cutoff:
                    bucket.popleft()
                if not bucket:
                    del self._buckets[existing_key]

            for key in keys:
                bucket = self._buckets.get(key)
                if bucket is not None and len(bucket) >= self.max_requests:
                    wait = max(1, math.ceil(bucket[0] + self.window_seconds - current))
                    raise RateLimitExceededError(wait)

            for key in keys:
                self._buckets.setdefault(key, deque()).append(current)

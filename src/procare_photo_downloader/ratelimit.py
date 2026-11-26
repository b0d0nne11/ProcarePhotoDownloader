"""An adaptive, thread-safe token bucket.

Procare does not publish its rate limit and does not return any rate limit
headers, so the right request rate cannot be known ahead of time. Observation
suggests a fixed quota per time window: a burst of requests succeeds, then
several in a row are refused, then the quota refills.

A fixed rate therefore either wastes time (too slow) or wastes requests in
backoff (too fast). Instead the bucket adapts the way TCP does: multiplicative
decrease when refused, additive increase while succeeding. It converges on
whatever the server currently allows and backs off again if that changes.
"""

import threading
import time


class TokenBucket:
    """Rate limit requests, adapting the rate to what the server actually accepts."""

    def __init__(
        self,
        rate: float,
        capacity: float | None = None,
        min_rate: float | None = None,
        backoff_factor: float = 0.6,
        recovery_step_factor: float = 0.05,
    ) -> None:
        if not rate > 0:
            raise ValueError("rate must be positive")
        if capacity is not None and capacity < rate:
            raise ValueError("capacity must be greater than or equal to rate")
        if min_rate is not None and not 0 < min_rate <= rate:
            raise ValueError("min_rate must be greater than 0 and less than or equal to rate")
        if not 0 < backoff_factor < 1:
            raise ValueError("backoff_factor must be between 0 and 1")
        if not 0 < recovery_step_factor < 1:
            raise ValueError("recovery_step_factor must be between 0 and 1")
        self._rate = rate
        self._capacity = rate if capacity is None else capacity
        self._min_rate = rate * 0.1 if min_rate is None else min_rate
        self._max_rate = rate
        self._backoff_factor = backoff_factor
        self._recovery_step = recovery_step_factor * rate
        self._tokens = self._capacity
        self._updated = time.monotonic()
        self._lock = threading.Lock()

    @property
    def rate(self) -> float:
        return self._rate

    def _drain_locked(self) -> None:
        self._tokens = 0.0
        self._updated = time.monotonic()

    def _refill_locked(self) -> None:
        now = time.monotonic()
        self._tokens = min(self._capacity, self._tokens + (now - self._updated) * self._rate)
        self._updated = now

    def acquire(self, tokens: float = 1.0) -> None:
        """Block until ``tokens`` are available, then consume them."""
        while True:
            with self._lock:
                self._refill_locked()
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return
                wait = (tokens - self._tokens) / self._rate
            time.sleep(wait)

    def recover(self) -> None:
        """Step the rate back up after a success."""
        with self._lock:
            self._refill_locked()
            self._rate = min(self._max_rate, self._rate + self._recovery_step)

    def backoff(self) -> None:
        """Drop the rate down after a refusal."""
        with self._lock:
            self._rate = max(self._min_rate, self._rate * self._backoff_factor)
            self._drain_locked()

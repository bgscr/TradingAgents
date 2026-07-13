from __future__ import annotations

import random
import time
from collections.abc import Callable
from dataclasses import dataclass


class TokenBucketLimiter:
    def __init__(
        self,
        calls_per_minute: int,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if calls_per_minute <= 0:
            raise ValueError("calls_per_minute must be positive")
        self.calls_per_minute = calls_per_minute
        self._clock = clock
        self._sleeper = sleeper
        self._interval = 60.0 / calls_per_minute
        self._last_call: float | None = None
        self._next_available: float | None = None

    def acquire(self) -> None:
        now = self._clock()
        if self._next_available is not None:
            wait = self._next_available - now
            if wait > 0:
                self._sleeper(wait)
                now = self._clock()
        self._last_call = now
        self._next_available = now + self._interval

    def penalize(self) -> None:
        self.calls_per_minute = max(1, self.calls_per_minute // 2)
        self._interval = 60.0 / self.calls_per_minute
        if self._last_call is not None:
            self._next_available = self._last_call + self._interval


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 8
    base_delay: float = 1.0
    max_delay: float = 60.0
    jitter: Callable[[float, float], float] = random.uniform

    def delay(self, attempt: int, retry_after: float | None) -> float:
        if retry_after is not None:
            return retry_after
        exponential = self.base_delay * (2 ** (attempt - 1))
        return min(self.max_delay, exponential) + self.jitter(0.0, 0.25)

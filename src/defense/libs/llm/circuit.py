"""Circuit breaker for LLM backends (architecture §8).

Per-backend breaker so a repeatedly-failing endpoint (Groq 5xx storm, local vLLM
down) is taken out of rotation for a cooldown instead of being hammered on every
request. Standard three states:

    closed     — calls flow; consecutive failures are counted
    open        — calls are short-circuited until the cooldown elapses
    half_open   — one trial call is allowed; success closes, failure re-opens

The clock is injectable so the state machine is unit-testable without sleeping.
"""

from __future__ import annotations

import time
from typing import Callable


class CircuitBreaker:
    def __init__(
        self,
        name: str = "backend",
        failure_threshold: int = 5,
        cooldown_seconds: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.name = name
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        self._clock = clock
        self._failures = 0
        self._opened_at: float | None = None

    @property
    def state(self) -> str:
        if self._opened_at is None:
            return "closed"
        if (self._clock() - self._opened_at) >= self.cooldown_seconds:
            return "half_open"
        return "open"

    def allow(self) -> bool:
        """True if a call may proceed (closed or half-open)."""
        return self.state != "open"

    def record_success(self) -> None:
        self._failures = 0
        self._opened_at = None

    def record_failure(self) -> None:
        # A failure during half-open re-opens immediately with a fresh cooldown.
        if self.state == "half_open":
            self._opened_at = self._clock()
            return
        self._failures += 1
        if self._failures >= self.failure_threshold:
            self._opened_at = self._clock()

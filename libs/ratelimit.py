"""Per-identity rate limiting (architecture §9) — fixed-window counter in Redis.

A single INCR + EXPIRE per request: cheap, atomic enough for abuse/cost
protection, and shared across API replicas via Redis. Returns how many requests
remain in the current window so callers can surface ``X-RateLimit-*`` headers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class RateLimitResult:
    allowed: bool
    limit: int
    remaining: int
    window_seconds: int


async def check_rate_limit(
    redis: Any,
    identifier: str,
    *,
    limit: int,
    window_seconds: int = 60,
    now: int | None = None,
) -> RateLimitResult:
    """Increment the caller's counter for the current fixed window.

    ``now`` (epoch seconds) is injectable for tests; defaults to wall clock.
    """
    if now is None:
        import time
        now = int(time.time())
    window = now // window_seconds
    key = f"ratelimit:{identifier}:{window_seconds}:{window}"

    count = int(await redis.incr(key))
    if count == 1:
        # First hit in this window — set its TTL so the key self-cleans.
        await redis.expire(key, window_seconds)

    remaining = max(0, limit - count)
    return RateLimitResult(
        allowed=count <= limit,
        limit=limit,
        remaining=remaining,
        window_seconds=window_seconds,
    )

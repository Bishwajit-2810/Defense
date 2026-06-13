"""Unit tests for libs/ratelimit.py — fixed-window rate limiting (§9)."""

import asyncio
import sys

sys.path.insert(0, '/home/bk/code/defense')

from libs.ratelimit import check_rate_limit


class FakeRedis:
    def __init__(self):
        self.kv: dict[str, int] = {}
        self.expires: dict[str, int] = {}

    async def incr(self, key):
        self.kv[key] = self.kv.get(key, 0) + 1
        return self.kv[key]

    async def expire(self, key, ttl):
        self.expires[key] = ttl


def test_allows_up_to_limit_then_blocks():
    r = FakeRedis()
    results = [
        asyncio.run(check_rate_limit(r, "user-1", limit=3, window_seconds=60, now=1000))
        for _ in range(4)
    ]
    assert [x.allowed for x in results] == [True, True, True, False]
    assert [x.remaining for x in results] == [2, 1, 0, 0]


def test_window_rollover_resets():
    r = FakeRedis()
    a = asyncio.run(check_rate_limit(r, "u", limit=1, window_seconds=60, now=1000))
    b = asyncio.run(check_rate_limit(r, "u", limit=1, window_seconds=60, now=1000))  # same window
    c = asyncio.run(check_rate_limit(r, "u", limit=1, window_seconds=60, now=1061))  # next window
    assert a.allowed and not b.allowed and c.allowed


def test_ttl_set_once_on_first_hit():
    r = FakeRedis()
    asyncio.run(check_rate_limit(r, "u", limit=5, window_seconds=60, now=1000))
    assert len(r.expires) == 1  # TTL set exactly once for the window key


def test_separate_identities_independent():
    r = FakeRedis()
    a = asyncio.run(check_rate_limit(r, "a", limit=1, window_seconds=60, now=1000))
    b = asyncio.run(check_rate_limit(r, "b", limit=1, window_seconds=60, now=1000))
    assert a.allowed and b.allowed

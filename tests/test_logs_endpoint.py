"""Unit tests for server-side log capture and serving.

Two halves:
  * ``libs/common/logging.py`` — the Redis sink that mirrors every service's
    lines into ``logs:recent`` / ``logs:live``.
  * ``routers/logs.py`` — the filters that serve them to the dashboard drawer.

The sink must never break a caller: a dead Redis, a missing key, an unknown
level all degrade quietly rather than raising into a worker loop.
"""

import asyncio
import json
import sys

sys.path.insert(0, '/home/bk/code/defense/src/defense')
sys.path.insert(0, '/home/bk/code/defense/src/defense/services/api')

from defense.services.api.routers import logs as L  # noqa: E402


class FakeRedis:
    """Just enough Redis for the log router."""

    def __init__(self, entries=None, raise_on_read=False):
        self.items = [json.dumps(e) for e in (entries or [])]
        self.raise_on_read = raise_on_read
        self.deleted = False

    async def lrange(self, key, start, end):
        if self.raise_on_read:
            raise Exception("ERR connection lost")
        if start == 0 and end == -1:
            return list(self.items)
        return self.items[start:] if end == -1 else self.items[start:end + 1]

    async def llen(self, key):
        return len(self.items)

    async def delete(self, key):
        self.deleted = True
        self.items = []


def _entry(service="stage1", level="INFO", message="stage1_text_done", **fields):
    return {
        "ts": 1785371056.0,
        "level": level,
        "service": service,
        "message": message,
        "fields": {k: str(v) for k, v in fields.items()},
        "module": "worker:123",
    }


# ---------------------------------------------------------------------------
# Level ranking
# ---------------------------------------------------------------------------

def test_rank_orders_levels():
    assert L._rank("DEBUG") < L._rank("INFO") < L._rank("WARNING") < L._rank("ERROR")


def test_unknown_level_sorts_high_so_it_is_never_hidden():
    # A level the ranking doesn't know must still pass a min_level filter,
    # otherwise an unexpected level would vanish from the drawer silently.
    assert L._rank("WEIRD") > L._rank("CRITICAL")
    assert L._matches(_entry(level="WEIRD"), None, "ERROR", None)


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------

def test_service_filter():
    e = _entry(service="router")
    assert L._matches(e, {"router"}, None, None)
    assert not L._matches(e, {"stage2"}, None, None)


def test_min_level_filter_drops_below_threshold():
    assert not L._matches(_entry(level="INFO"), None, "WARNING", None)
    assert L._matches(_entry(level="ERROR"), None, "WARNING", None)


def test_contains_searches_message_service_and_fields():
    e = _entry(message="stage1_fusion_done", overall_sentiment="negative")
    assert L._matches(e, None, None, "fusion")
    assert L._matches(e, None, None, "negative")      # value in fields
    assert L._matches(e, None, None, "overall_")      # key in fields
    assert L._matches(e, None, None, "STAGE1")        # case-insensitive
    assert not L._matches(e, None, None, "nonsense")


def test_decode_skips_malformed_lines():
    assert L._decode("not json") is None
    assert L._decode(json.dumps([1, 2])) is None       # not a dict
    assert L._decode(json.dumps({"a": 1})) == {"a": 1}
    assert L._decode(b'{"a": 2}') == {"a": 2}          # bytes from Redis


# ---------------------------------------------------------------------------
# GET /v1/logs
# ---------------------------------------------------------------------------

def test_get_logs_returns_newest_last_and_respects_limit():
    entries = [_entry(message=f"m{i}") for i in range(10)]
    r = FakeRedis(entries)
    out = asyncio.run(L.get_logs(limit=3, service=None, min_level=None,
                                 contains=None, redis=r, current_user={}))
    assert out["buffered"] == 10
    assert out["matched"] == 10
    assert out["count"] == 3
    # newest last, so a caller can append straight into a log view
    assert [e["message"] for e in out["entries"]] == ["m7", "m8", "m9"]


def test_get_logs_combines_filters():
    entries = [
        _entry(service="stage1", level="INFO", message="stage1_text_done"),
        _entry(service="stage1", level="ERROR", message="stage1_text_failed"),
        _entry(service="router", level="ERROR", message="router_boom"),
    ]
    r = FakeRedis(entries)
    out = asyncio.run(L.get_logs(limit=50, service="stage1", min_level="ERROR",
                                 contains=None, redis=r, current_user={}))
    assert [e["message"] for e in out["entries"]] == ["stage1_text_failed"]


def test_get_logs_survives_a_dead_redis():
    out = asyncio.run(L.get_logs(limit=10, service=None, min_level=None,
                                 contains=None, redis=FakeRedis(raise_on_read=True),
                                 current_user={}))
    assert out["entries"] == []
    assert "error" in out


# ---------------------------------------------------------------------------
# GET /v1/logs/services
# ---------------------------------------------------------------------------

def test_services_endpoint_counts_by_service_and_level():
    entries = [
        _entry(service="stage1"), _entry(service="stage1"),
        _entry(service="router", level="WARNING"),
    ]
    out = asyncio.run(L.get_log_services(redis=FakeRedis(entries), current_user={}))
    assert out["services"][0] == {"service": "stage1", "count": 2}   # busiest first
    assert {lvl["level"] for lvl in out["levels"]} == {"INFO", "WARNING"}
    assert out["buffered"] == 3


# ---------------------------------------------------------------------------
# DELETE /v1/logs
# ---------------------------------------------------------------------------

def test_clear_logs_reports_how_many_it_dropped():
    r = FakeRedis([_entry(), _entry()])
    out = asyncio.run(L.clear_logs(redis=r, current_user={}))
    assert out == {"cleared": 2}
    assert r.deleted


# ---------------------------------------------------------------------------
# The Redis sink in libs/common/logging.py
# ---------------------------------------------------------------------------

def _log_record(msg: str = "hello"):
    """A real LogRecord — the sink is a logging.Handler now, not a loguru sink."""
    import logging as _logging

    record = _logging.LogRecord(
        name="mod", level=_logging.INFO, pathname="mod.py", lineno=1,
        msg=msg, args=(), exc_info=None,
    )
    record.service = "test"
    return record


def test_sink_never_raises_when_redis_is_broken(monkeypatch):
    """A logging call must not fail because the log mirror is down."""
    from defense.libs.common import logging as CL

    class Boom:
        def pipeline(self, transaction=False):
            raise Exception("redis gone")

    monkeypatch.setattr(CL, "_sink_client", lambda: Boom())
    monkeypatch.setattr(CL, "_redis_sink_fails", 0)
    monkeypatch.setattr(CL, "_redis_enabled", lambda: True)

    CL.RedisLogHandler().emit(_log_record())   # must not raise
    assert CL._redis_sink_fails == 1


def test_sink_gives_up_after_repeated_failures(monkeypatch):
    """Stops trying so a dead Redis costs nothing per log line."""
    from defense.libs.common import logging as CL

    calls = {"n": 0}

    def counting_client():
        calls["n"] += 1
        raise Exception("still down")

    monkeypatch.setattr(CL, "_sink_client", counting_client)
    monkeypatch.setattr(CL, "_redis_enabled", lambda: True)
    monkeypatch.setattr(CL, "_redis_sink_fails", CL._REDIS_SINK_GIVE_UP)

    CL.RedisLogHandler().emit(_log_record())
    assert calls["n"] == 0                  # short-circuited before touching Redis

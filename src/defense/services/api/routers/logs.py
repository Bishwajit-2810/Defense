"""Server-side log access for the dashboard's log drawer.

Every service mirrors its log lines into Redis (see ``libs/common/logging.py``):
a capped list ``logs:recent`` for history and a ``logs:live`` pub/sub channel for
the tail. This router serves both, so the dashboard can show what the backend is
doing without shell access to ``/tmp/*.log``.

Redis rather than reading the log files because the services do not share a
filesystem once they run as containers or pods — but they always share a Redis.

    GET /v1/logs           — recent entries, newest last, with filters
    GET /v1/logs/stream    — SSE tail (a `log` event per line)
    GET /v1/logs/services  — which services have logged, for the filter UI
    DELETE /v1/logs        — clear the buffer
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from typing import Any

import redis.asyncio as aioredis
import structlog
from defense.services.api.deps import get_current_user, get_redis
from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse

from defense.libs.common.logging import LOG_CHANNEL, LOG_LIST_KEY

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/v1/logs", tags=["logs"])

_LEVELS = ("TRACE", "DEBUG", "INFO", "SUCCESS", "WARNING", "ERROR", "CRITICAL")

# Ranked so `min_level` can filter numerically; anything unrecognised sorts high
# so an unexpected level is shown rather than silently dropped.
_LEVEL_RANK = {name: i for i, name in enumerate(_LEVELS)}

_STREAM_MAX_SECONDS = 600


def _rank(level: str) -> int:
    return _LEVEL_RANK.get((level or "").upper(), len(_LEVELS))


def _decode(raw: Any) -> dict | None:
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode(errors="replace")
    try:
        entry = json.loads(raw)
        return entry if isinstance(entry, dict) else None
    except (json.JSONDecodeError, TypeError):
        return None


def _matches(
    entry: dict,
    services: set[str] | None,
    min_level: str | None,
    contains: str | None,
) -> bool:
    if services and (entry.get("service") or "-") not in services:
        return False
    if min_level and _rank(entry.get("level", "")) < _rank(min_level):
        return False
    if contains:
        needle = contains.lower()
        haystack = " ".join((
            str(entry.get("message", "")),
            str(entry.get("service", "")),
            str(entry.get("module", "")),
            " ".join(f"{k}={v}" for k, v in (entry.get("fields") or {}).items()),
        )).lower()
        if needle not in haystack:
            return False
    return True


@router.get("", summary="Recent server-side log lines from every service")
async def get_logs(
    limit: int = Query(200, ge=1, le=2000, description="Most recent N matching lines"),
    service: str | None = Query(
        None, description="Comma-separated service names (e.g. 'stage1,router')"
    ),
    min_level: str | None = Query(
        None, description="Minimum level: DEBUG, INFO, WARNING, ERROR"
    ),
    contains: str | None = Query(None, description="Substring match over message + fields"),
    redis: aioredis.Redis = Depends(get_redis),
    current_user: dict = Depends(get_current_user),
) -> dict:
    """Newest-last so the caller can append straight into a log view."""
    services = {s.strip() for s in service.split(",") if s.strip()} if service else None

    try:
        raw = await redis.lrange(LOG_LIST_KEY, 0, -1)
    except Exception as exc:
        log.warning("logs_read_failed", error=str(exc))
        return {"entries": [], "count": 0, "buffered": 0, "error": str(exc)}

    entries = [e for e in (_decode(x) for x in raw or []) if e]
    buffered = len(entries)
    matched = [e for e in entries if _matches(e, services, min_level, contains)]

    return {
        "entries": matched[-limit:],
        "count": min(len(matched), limit),
        "matched": len(matched),
        "buffered": buffered,
    }


@router.get("/services", summary="Services present in the log buffer")
async def get_log_services(
    redis: aioredis.Redis = Depends(get_redis),
    current_user: dict = Depends(get_current_user),
) -> dict:
    """Counts per service + per level, so the drawer can build its filters."""
    try:
        raw = await redis.lrange(LOG_LIST_KEY, 0, -1)
    except Exception as exc:
        return {"services": [], "levels": [], "error": str(exc)}

    per_service: dict[str, int] = {}
    per_level: dict[str, int] = {}
    for entry in (_decode(x) for x in raw or []):
        if not entry:
            continue
        svc = entry.get("service") or "-"
        per_service[svc] = per_service.get(svc, 0) + 1
        lvl = (entry.get("level") or "").upper()
        per_level[lvl] = per_level.get(lvl, 0) + 1

    return {
        "services": [
            {"service": s, "count": n}
            for s, n in sorted(per_service.items(), key=lambda kv: -kv[1])
        ],
        "levels": [
            {"level": lvl, "count": per_level[lvl]}
            for lvl in _LEVELS if lvl in per_level
        ],
        "buffered": sum(per_service.values()),
    }


async def _log_sse(
    redis: aioredis.Redis,
    services: set[str] | None,
    min_level: str | None,
    contains: str | None,
    backfill: int,
) -> AsyncGenerator[str, None]:
    """Replay a little history, then tail ``logs:live``.

    Subscribing before the backfill read means no line can slip between the two;
    the duplicate that straddles them is harmless in a log view.
    """
    pubsub = redis.pubsub()
    await pubsub.subscribe(LOG_CHANNEL)

    try:
        yield "event: connected\ndata: {}\n\n"

        if backfill:
            try:
                raw = await redis.lrange(LOG_LIST_KEY, -backfill, -1)
                for entry in (_decode(x) for x in raw or []):
                    if entry and _matches(entry, services, min_level, contains):
                        entry["backfill"] = True
                        yield f"event: log\ndata: {json.dumps(entry, ensure_ascii=False)}\n\n"
            except Exception as exc:
                log.warning("logs_sse_backfill_failed", error=str(exc))

        loop = asyncio.get_event_loop()
        deadline = loop.time() + _STREAM_MAX_SECONDS

        while loop.time() < deadline:
            message = await pubsub.get_message(
                ignore_subscribe_messages=True, timeout=1.0
            )
            if message and message.get("type") == "message":
                entry = _decode(message.get("data", ""))
                if entry and _matches(entry, services, min_level, contains):
                    yield f"event: log\ndata: {json.dumps(entry, ensure_ascii=False)}\n\n"
            else:
                yield ": keepalive\n\n"

        yield "event: timeout\ndata: {}\n\n"

    except asyncio.CancelledError:  # client went away
        raise
    except Exception as exc:
        log.warning("logs_sse_error", error=str(exc))
        yield f"event: error\ndata: {json.dumps({'error': str(exc)})}\n\n"
    finally:
        # Teardown on a connection the client has usually already dropped —
        # unsubscribe/close routinely raise here and there is nothing useful to
        # do about it, so swallow rather than mask the original error.
        try:
            await pubsub.unsubscribe(LOG_CHANNEL)
            await pubsub.close()
        except Exception:  # noqa: S110 — see above
            pass


@router.get(
    "/stream",
    summary="Tail server-side logs via SSE",
    response_class=StreamingResponse,
)
async def stream_logs(
    service: str | None = Query(None, description="Comma-separated service names"),
    min_level: str | None = Query(None, description="Minimum level"),
    contains: str | None = Query(None, description="Substring match"),
    backfill: int = Query(60, ge=0, le=500, description="Replay this many recent lines first"),
    redis: aioredis.Redis = Depends(get_redis),
    current_user: dict = Depends(get_current_user),
) -> StreamingResponse:
    """A ``log`` event per line for 10 minutes, then ``timeout``.

    Auth accepts ``?api_key=`` since EventSource cannot set headers.
    """
    services = {s.strip() for s in service.split(",") if s.strip()} if service else None
    return StreamingResponse(
        _log_sse(redis, services, min_level, contains, backfill),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.delete("", summary="Clear the server-side log buffer")
async def clear_logs(
    redis: aioredis.Redis = Depends(get_redis),
    current_user: dict = Depends(get_current_user),
) -> dict:
    try:
        removed = int(await redis.llen(LOG_LIST_KEY) or 0)
        await redis.delete(LOG_LIST_KEY)
        return {"cleared": removed}
    except Exception as exc:
        log.warning("logs_clear_failed", error=str(exc))
        return {"cleared": 0, "error": str(exc)}

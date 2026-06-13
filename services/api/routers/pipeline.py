"""Pipeline live-flow endpoints — real-time per-stage state of the pipeline.

Surfaces the actual Redis Stream state for each stage (no new instrumentation):

    backlog   — entries delivered to nobody yet (consumer-group lag) → "waiting"
    in_flight — delivered but not yet ACKed (pending entries)         → "processing"
    dlq       — dead-lettered entries on <stream>:dlq                 → "failed"

Plus corpus-level ``completed`` (rows in analysis_results) and the router's
LLM-routing counters. Exposed as a point-in-time GET and an SSE stream the
dashboard's Pipeline tab animates.
"""

from __future__ import annotations

import asyncio
import json
from typing import AsyncGenerator

import redis.asyncio as aioredis
import structlog
from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from deps import get_current_user, get_db, get_redis

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/v1/pipeline", tags=["pipeline"])

# (key, label, stream, consumer-group) in pipeline order.
_STAGES: list[tuple[str, str, str, str]] = [
    ("ingestion", "Ingestion", "ingestion:queue", "ingestion-workers"),
    ("stage1", "Stage-1 NLP", "nlp:stage1:queue", "stage1-nlp-group"),
    ("router", "Router", "router:queue", "router-workers"),
    ("stage2", "Stage-2 LLM", "llm:stage2:queue", "stage2-llm-workers"),
    ("assembler", "Assembler", "assembler:queue", "assembler-group"),
]

_POLL_SECONDS = 1.5
_STREAM_MAX_SECONDS = 600


def _as_str(v) -> str:
    return v.decode() if isinstance(v, (bytes, bytearray)) else str(v)


async def _stage_stats(redis: aioredis.Redis, stream: str, group: str) -> dict:
    """Backlog (group lag) + in-flight (pending) + dlq depth for one stage."""
    backlog = in_flight = total = dlq = 0
    try:
        total = int(await redis.xlen(stream))
    except Exception:
        total = 0
    try:
        for g in await redis.xinfo_groups(stream):
            gname = _as_str(g.get("name", g.get(b"name", "")))
            if gname != group:
                continue
            in_flight += int(g.get("pending", g.get(b"pending", 0)) or 0)
            lag = g.get("lag", g.get(b"lag", None))
            if lag is not None:
                backlog += int(lag)
    except Exception:
        # Stream/group not created yet (no traffic) → zeros.
        pass
    try:
        dlq = int(await redis.xlen(f"{stream}:dlq"))
    except Exception:
        dlq = 0
    return {"backlog": backlog, "in_flight": in_flight, "dlq": dlq, "total": total}


async def _int_key(redis: aioredis.Redis, key: str) -> int:
    try:
        return int(await redis.get(key) or 0)
    except Exception:
        return 0


async def _completed_count(db: AsyncSession) -> int:
    try:
        row = (await db.execute(text("SELECT count(*) AS n FROM analysis_results"))).mappings().first()
        return int(row["n"]) if row else 0
    except Exception:
        return 0


async def collect_stats(redis: aioredis.Redis, db: AsyncSession) -> dict:
    """Assemble the full pipeline snapshot."""
    stages = []
    for key, label, stream, group in _STAGES:
        s = await _stage_stats(redis, stream, group)
        s.update({"key": key, "label": label, "stream": stream})
        stages.append(s)

    return {
        "stages": stages,
        "completed": await _completed_count(db),
        "total_processed": await _int_key(redis, "stats:total_processed"),
        "llm_routed": await _int_key(redis, "stats:llm_routed"),
        "dlq_total": sum(s["dlq"] for s in stages),
        "in_flight_total": sum(s["in_flight"] for s in stages),
        "backlog_total": sum(s["backlog"] for s in stages),
    }


@router.get("/stats", summary="Point-in-time pipeline stage state")
async def pipeline_stats(
    redis: aioredis.Redis = Depends(get_redis),
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> dict:
    return await collect_stats(redis, db)


async def _stats_sse(redis: aioredis.Redis, db: AsyncSession) -> AsyncGenerator[str, None]:
    """Poll the pipeline state and push it as SSE ``stats`` events."""
    yield "event: connected\ndata: {}\n\n"
    loop = asyncio.get_event_loop()
    deadline = loop.time() + _STREAM_MAX_SECONDS
    try:
        while loop.time() < deadline:
            stats = await collect_stats(redis, db)
            yield f"event: stats\ndata: {json.dumps(stats, default=str)}\n\n"
            await asyncio.sleep(_POLL_SECONDS)
        yield "event: timeout\ndata: {}\n\n"
    except asyncio.CancelledError:  # client disconnected
        raise
    except Exception as exc:
        log.warning("pipeline_sse_error", error=str(exc))
        yield f"event: error\ndata: {json.dumps({'error': str(exc)})}\n\n"


@router.get(
    "/stream",
    summary="Stream live pipeline stage state via SSE",
    response_class=StreamingResponse,
)
async def pipeline_stream(
    redis: aioredis.Redis = Depends(get_redis),
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> StreamingResponse:
    """Server-Sent Events: a ``stats`` frame every ~1.5s for 10 minutes.

    Auth accepts ``?api_key=`` (EventSource can't set headers). The dashboard
    Pipeline tab opens this and animates each stage's backlog/in-flight/DLQ.
    """
    return StreamingResponse(
        _stats_sse(redis, db),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

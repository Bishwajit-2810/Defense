"""ingest-mcp — FastAPI MCP server for triggering upstream ingestion pulls.

ARCHITECTURE INVARIANT
-----------------------
These tools trigger pulls from the upstream post-with-details API and write
ONLY to our own database (Postgres + Redis queue).  We NEVER write back to
upstream.  This constraint must be preserved in every future change to this
file.

Tools
-----
pull_campaign       — push a campaign date-range pull job to ingestion:queue
fetch_more_comments — push a comment-fetch job to ingestion:fetch_comments
refresh_post        — push a post re-fetch job to ingestion:refresh

All three handlers return immediately with {job_id, status: "queued"}.
The actual upstream fetch is performed asynchronously by the ingestion worker.

Environment variables
---------------------
REDIS_URL       redis://localhost:6379/0  (default)
PORT            8002                      (default)
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from typing import Any

import redis.asyncio as aioredis
import structlog
from fastapi import FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from manifest import TOOL_MANIFEST

# ---------------------------------------------------------------------------
# Structured logging
# ---------------------------------------------------------------------------

structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
    context_class=dict,
    logger_factory=structlog.PrintLoggerFactory(),
    cache_logger_on_first_use=True,
)

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_REDIS_URL: str = os.environ.get("REDIS_URL", "redis://localhost:6379/0")

# Redis stream keys consumed by the ingestion worker
_STREAM_QUEUE = "ingestion:queue"
_STREAM_FETCH_COMMENTS = "ingestion:fetch_comments"
_STREAM_REFRESH = "ingestion:refresh"

# ---------------------------------------------------------------------------
# Redis connection (lazy singleton pool)
# ---------------------------------------------------------------------------

_redis_pool: aioredis.Redis | None = None


def _get_redis() -> aioredis.Redis:
    global _redis_pool
    if _redis_pool is None:
        _redis_pool = aioredis.from_url(
            _REDIS_URL,
            encoding="utf-8",
            decode_responses=True,
        )
        log.info("redis_pool_initialized", url=_REDIS_URL)
    return _redis_pool


async def _enqueue(stream: str, payload: dict[str, Any]) -> str:
    """Push a JSON payload onto a Redis stream.  Returns the stream entry ID."""
    # These tools write only to our OWN database. We never write upstream.
    client = _get_redis()
    entry_id: str = await client.xadd(
        stream,
        {"data": json.dumps(payload, default=str)},
    )
    return entry_id


# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------

app = FastAPI(
    title="ingest-mcp",
    version="1.0.0",
    description=(
        "MCP server for triggering upstream ingestion pulls. "
        "Writes ONLY to our own database — never to upstream."
    ),
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------


class ToolCallRequest(BaseModel):
    name: str = Field(..., description="Tool name to invoke")
    arguments: dict[str, Any] = Field(default_factory=dict, description="Tool arguments")


class ToolCallResponse(BaseModel):
    name: str
    result: Any
    error: str | None = None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/health", summary="Liveness probe")
async def health() -> dict:
    return {"status": "ok", "service": "ingest-mcp", "version": "1.0.0"}


@app.get("/mcp/manifest", summary="Return the MCP tool manifest")
async def get_manifest() -> list[dict]:
    return TOOL_MANIFEST


@app.post(
    "/tools/call",
    response_model=ToolCallResponse,
    summary="Invoke an ingest tool",
)
async def tools_call(req: ToolCallRequest) -> ToolCallResponse:
    """Route a tool call to the appropriate handler.

    All handlers push a job onto a Redis stream and return immediately.
    The ingestion worker processes the job asynchronously.

    INVARIANT: These tools write only to our OWN database. We never write upstream.
    """
    log.info("tool_call", tool=req.name, args=req.arguments)

    dispatch = {
        "pull_campaign": _handle_pull_campaign,
        "fetch_more_comments": _handle_fetch_more_comments,
        "refresh_post": _handle_refresh_post,
    }

    handler = dispatch.get(req.name)
    if handler is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown tool: {req.name!r}",
        )

    try:
        result = await handler(req.arguments)
        return ToolCallResponse(name=req.name, result=result)
    except HTTPException:
        raise
    except Exception as exc:
        log.error("tool_call_failed", tool=req.name, error=str(exc), exc_info=True)
        return ToolCallResponse(name=req.name, result=None, error=str(exc))


# ---------------------------------------------------------------------------
# Handler: pull_campaign
# ---------------------------------------------------------------------------


async def _handle_pull_campaign(args: dict[str, Any]) -> dict:
    """Push a campaign pull job to the ingestion:queue Redis stream.

    # These tools write only to our OWN database. We never write upstream.
    """
    campaign_id: str = args["campaign_id"]
    from_date: str = args["from_date"]
    to_date: str = args["to_date"]
    limit: int = min(int(args.get("limit", 100)), 1000)

    job_id = str(uuid.uuid4())

    payload = {
        "job_id": job_id,
        "job_type": "pull_campaign",
        "campaign_id": campaign_id,
        "from_date": from_date,
        "to_date": to_date,
        "limit": limit,
    }

    entry_id = await _enqueue(_STREAM_QUEUE, payload)

    log.info(
        "pull_campaign_queued",
        job_id=job_id,
        campaign_id=campaign_id,
        from_date=from_date,
        to_date=to_date,
        limit=limit,
        stream_entry=entry_id,
    )

    return {
        "job_id": job_id,
        "status": "queued",
        "campaign_id": campaign_id,
        "from_date": from_date,
        "to_date": to_date,
        "limit": limit,
    }


# ---------------------------------------------------------------------------
# Handler: fetch_more_comments
# ---------------------------------------------------------------------------


async def _handle_fetch_more_comments(args: dict[str, Any]) -> dict:
    """Push a comment-fetch job to the ingestion:fetch_comments Redis stream.

    # These tools write only to our OWN database. We never write upstream.
    """
    post_id: str = args["post_id"]
    limit: int = min(int(args.get("limit", 100)), 500)

    job_id = str(uuid.uuid4())

    payload = {
        "job_id": job_id,
        "job_type": "fetch_more_comments",
        "post_id": post_id,
        "limit": limit,
    }

    entry_id = await _enqueue(_STREAM_FETCH_COMMENTS, payload)

    log.info(
        "fetch_more_comments_queued",
        job_id=job_id,
        post_id=post_id,
        limit=limit,
        stream_entry=entry_id,
    )

    return {
        "job_id": job_id,
        "post_id": post_id,
        "status": "queued",
        "limit": limit,
    }


# ---------------------------------------------------------------------------
# Handler: refresh_post
# ---------------------------------------------------------------------------


async def _handle_refresh_post(args: dict[str, Any]) -> dict:
    """Push a post-refresh job to the ingestion:refresh Redis stream.

    # These tools write only to our OWN database. We never write upstream.
    """
    post_id: str = args["post_id"]
    job_id = str(uuid.uuid4())

    payload = {
        "job_id": job_id,
        "job_type": "refresh_post",
        "post_id": post_id,
    }

    entry_id = await _enqueue(_STREAM_REFRESH, payload)

    log.info(
        "refresh_post_queued",
        job_id=job_id,
        post_id=post_id,
        stream_entry=entry_id,
    )

    return {
        "job_id": job_id,
        "post_id": post_id,
        "status": "queued",
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", 8002))
    log.info("ingest_mcp_starting", port=port)
    uvicorn.run("server:app", host="0.0.0.0", port=port, reload=False)

"""ingest-mcp — MCP server for triggering upstream ingestion pulls.

Real MCP server (FastMCP, streamable-HTTP transport).

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

All three tools return immediately with {job_id, status: "queued"}.
The actual upstream fetch is performed asynchronously by the ingestion worker.

Environment variables
---------------------
REDIS_URL       redis://localhost:6379/0  (default)
PORT            8102                      (default)
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from typing import Annotated, Any

import redis.asyncio as aioredis
import structlog
from fastmcp import FastMCP
from pydantic import Field
from starlette.requests import Request
from starlette.responses import JSONResponse

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
# MCP server
# ---------------------------------------------------------------------------

mcp = FastMCP(
    name="ingest-mcp",
    instructions=(
        "Trigger upstream ingestion pulls into OUR OWN database (never write "
        "upstream). Each tool enqueues an async job and returns {job_id, status}."
    ),
)


@mcp.tool
async def pull_campaign(
    campaign_id: Annotated[str, Field(description="Campaign id (CUID) to pull.")],
    from_date: Annotated[str, Field(description="Inclusive start date (YYYY-MM-DD).")],
    to_date: Annotated[str, Field(description="Inclusive end date (YYYY-MM-DD).")],
    limit: Annotated[int, Field(description="Max posts to pull (max 1000).", ge=1, le=1000)] = 100,
) -> dict:
    """Queue a campaign date-range pull from the upstream post-with-details API.
    Writes only to our own DB; returns immediately with a queued job id."""
    log.info("tool_call", tool="pull_campaign", campaign_id=campaign_id)
    limit = min(int(limit), 1000)
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


@mcp.tool
async def fetch_more_comments(
    post_id: Annotated[str, Field(description="Post id (CUID) to fetch more comments for.")],
    limit: Annotated[int, Field(description="Max comments to fetch (max 500).", ge=1, le=500)] = 100,
) -> dict:
    """Queue a deeper comment pull for a post (raises comment coverage).
    Writes only to our own DB; returns immediately with a queued job id."""
    log.info("tool_call", tool="fetch_more_comments", post_id=post_id)
    limit = min(int(limit), 500)
    job_id = str(uuid.uuid4())

    payload = {
        "job_id": job_id,
        "job_type": "fetch_more_comments",
        "post_id": post_id,
        "limit": limit,
    }
    entry_id = await _enqueue(_STREAM_FETCH_COMMENTS, payload)

    log.info("fetch_more_comments_queued", job_id=job_id, post_id=post_id, limit=limit, stream_entry=entry_id)
    return {"job_id": job_id, "post_id": post_id, "status": "queued", "limit": limit}


@mcp.tool
async def refresh_post(
    post_id: Annotated[str, Field(description="Post id (CUID) to re-fetch from upstream.")],
) -> dict:
    """Queue a re-fetch of a single post from upstream.
    Writes only to our own DB; returns immediately with a queued job id."""
    log.info("tool_call", tool="refresh_post", post_id=post_id)
    job_id = str(uuid.uuid4())

    payload = {
        "job_id": job_id,
        "job_type": "refresh_post",
        "post_id": post_id,
    }
    entry_id = await _enqueue(_STREAM_REFRESH, payload)

    log.info("refresh_post_queued", job_id=job_id, post_id=post_id, stream_entry=entry_id)
    return {"job_id": job_id, "post_id": post_id, "status": "queued"}


@mcp.custom_route("/health", methods=["GET"])
async def health(_request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "service": "ingest-mcp"})


# ASGI app served by uvicorn: streamable-HTTP MCP endpoint mounted at /mcp.
app = mcp.http_app(path="/mcp")


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", 8102))
    log.info("ingest_mcp_starting", port=port)
    uvicorn.run(app, host="0.0.0.0", port=port)

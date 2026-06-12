"""retrieval-mcp — FastAPI MCP server for semantic search and post retrieval.

Backed by:
  - Postgres + pgvector (vector search on analysis_results.embedding)
  - Postgres (full analysis_results + comments tables)

Stub mode
---------
Set RETRIEVAL_MCP_STUB=true to skip vector search entirely.  semantic_search
will return the first N rows from analysis_results matching the optional
campaign_id filter.  All other handlers always hit Postgres.

Environment variables
---------------------
DATABASE_URL            postgresql+asyncpg://... (default: localhost defense DB)
RETRIEVAL_MCP_STUB      true | false  (default false)
MODEL_STUB_MODE         true | false — query embedding via libs/embeddings.py
EMBEDDING_MODEL         SentenceTransformer name (default 768-dim multilingual;
                        must match the analysis_results.embedding column dim)
PORT                    8001  (default)
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any

import structlog

# Repo root on path for `libs.*` (no-op when PYTHONPATH already provides it).
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
from fastapi import FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

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

_DATABASE_URL: str = os.environ.get(
    "DATABASE_URL",
    "postgresql+asyncpg://defense:defense@localhost:5432/defense",
)
if _DATABASE_URL.startswith("postgresql://"):
    _DATABASE_URL = _DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)
elif _DATABASE_URL.startswith("postgres://"):
    _DATABASE_URL = _DATABASE_URL.replace("postgres://", "postgresql+asyncpg://", 1)

_STUB_MODE: bool = os.environ.get("RETRIEVAL_MCP_STUB", "false").lower() == "true"

# ---------------------------------------------------------------------------
# Postgres engine
# ---------------------------------------------------------------------------

_engine = create_async_engine(
    _DATABASE_URL,
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=10,
    echo=False,
)
_AsyncSessionLocal = async_sessionmaker(
    bind=_engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
    autocommit=False,
)

# Query embedding comes from the shared helper so its dimension always matches
# the pgvector column and the Stage-1 document embeddings (libs/embeddings.py:
# real SentenceTransformer when MODEL_STUB_MODE=false, deterministic stub otherwise).
from libs.embeddings import embed_text, to_pgvector_literal  # noqa: E402


# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------

app = FastAPI(
    title="retrieval-mcp",
    version="1.0.0",
    description="MCP server: semantic search (pgvector) + post/thread retrieval (Postgres).",
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
    return {"status": "ok", "service": "retrieval-mcp", "version": "1.0.0"}


@app.get("/mcp/manifest", summary="Return the MCP tool manifest")
async def get_manifest() -> list[dict]:
    return TOOL_MANIFEST


@app.post(
    "/tools/call",
    response_model=ToolCallResponse,
    summary="Invoke a retrieval tool",
)
async def tools_call(req: ToolCallRequest) -> ToolCallResponse:
    """Route a tool call to the appropriate handler."""
    log.info("tool_call", tool=req.name, args=req.arguments)

    dispatch = {
        "semantic_search": _handle_semantic_search,
        "get_post": _handle_get_post,
        "get_thread": _handle_get_thread,
        "representative_comments": _handle_representative_comments,
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
# Handler: semantic_search
# ---------------------------------------------------------------------------


async def _handle_semantic_search(args: dict[str, Any]) -> list[dict]:
    """Vector search via Postgres/pgvector, or stub fallback by recency."""
    query: str = args["query"]
    campaign_id: str | None = args.get("campaign_id")
    limit: int = min(int(args.get("limit", 10)), 50)
    sentiment_filter: str | None = args.get("sentiment_filter")

    if _STUB_MODE:
        log.info(
            "semantic_search_stub",
            query=query,
            campaign_id=campaign_id,
            limit=limit,
            sentiment_filter=sentiment_filter,
        )
        return await _stub_semantic_search(campaign_id, limit, sentiment_filter)

    # --- Real path: embed query and run a pgvector cosine-similarity search ---
    qvec = to_pgvector_literal(embed_text(query))

    params: dict[str, Any] = {"qvec": qvec, "limit": limit}
    where_parts = ["ar.embedding IS NOT NULL"]
    if campaign_id:
        where_parts.append("ar.campaign_id = :campaign_id")
        params["campaign_id"] = campaign_id
    if sentiment_filter:
        where_parts.append("ar.result->>'overall_sentiment' = :sentiment")
        params["sentiment"] = sentiment_filter

    # ``<=>`` is pgvector's cosine-distance operator; score = 1 - distance.
    sql = text(
        f"""
        SELECT
            ar.post_id,
            ar.campaign_id,
            ar.result->>'overall_sentiment'  AS overall_sentiment,
            ar.result->>'post_summary'       AS post_summary,
            1 - (ar.embedding <=> CAST(:qvec AS vector)) AS score
        FROM analysis_results ar
        WHERE {' AND '.join(where_parts)}
        ORDER BY ar.embedding <=> CAST(:qvec AS vector)
        LIMIT :limit
        """
    )

    async with _AsyncSessionLocal() as session:
        rows = (await session.execute(sql, params)).mappings().all()

    results = [
        {
            "post_id": row["post_id"],
            "score": round(float(row["score"]), 6) if row["score"] is not None else None,
            "campaign_id": row["campaign_id"],
            "overall_sentiment": row["overall_sentiment"],
            "post_summary": row["post_summary"],
        }
        for row in rows
    ]

    log.info(
        "semantic_search_completed",
        query=query,
        hits=len(results),
        stub=False,
    )
    return results


async def _stub_semantic_search(
    campaign_id: str | None,
    limit: int,
    sentiment_filter: str | None,
) -> list[dict]:
    """Stub: return first N analysis_results from Postgres matching filters."""
    params: dict[str, Any] = {"limit": limit}
    where_parts = ["1=1"]

    if campaign_id:
        where_parts.append("ar.campaign_id = :campaign_id")
        params["campaign_id"] = campaign_id
    if sentiment_filter:
        where_parts.append("ar.result->>'overall_sentiment' = :sentiment")
        params["sentiment"] = sentiment_filter

    sql = text(
        f"""
        SELECT
            ar.post_id,
            ar.campaign_id,
            ar.result->>'overall_sentiment'  AS overall_sentiment,
            ar.result->>'post_summary'       AS post_summary
        FROM analysis_results ar
        WHERE {' AND '.join(where_parts)}
        ORDER BY ar.created_at DESC
        LIMIT :limit
        """
    )

    async with _AsyncSessionLocal() as session:
        rows = (await session.execute(sql, params)).mappings().all()

    return [
        {
            "post_id": row["post_id"],
            "score": 1.0,
            "campaign_id": row["campaign_id"],
            "overall_sentiment": row["overall_sentiment"],
            "post_summary": row["post_summary"],
        }
        for row in rows
    ]


# ---------------------------------------------------------------------------
# Handler: get_post
# ---------------------------------------------------------------------------


async def _handle_get_post(args: dict[str, Any]) -> dict | None:
    """SELECT result FROM analysis_results WHERE post_id=$1."""
    post_id: str = args["post_id"]

    # scraped_at lives on the posts table (analysis_results has no such column).
    sql = text(
        """
        SELECT
            ar.id,
            ar.post_id,
            ar.campaign_id,
            ar.result,
            ar.created_at,
            p.scraped_at
        FROM analysis_results ar
        JOIN posts p ON p.id = ar.post_id
        WHERE ar.post_id = :post_id
        ORDER BY ar.created_at DESC
        LIMIT 1
        """
    )

    async with _AsyncSessionLocal() as session:
        row = (await session.execute(sql, {"post_id": post_id})).mappings().first()

    if row is None:
        log.warning("get_post_not_found", post_id=post_id)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Post not found: {post_id!r}",
        )

    return {
        "id": row["id"],
        "post_id": row["post_id"],
        "campaign_id": row["campaign_id"],
        "result": row["result"],
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "scraped_at": row["scraped_at"].isoformat() if row["scraped_at"] else None,
    }


# ---------------------------------------------------------------------------
# Handler: get_thread
# ---------------------------------------------------------------------------


async def _handle_get_thread(args: dict[str, Any]) -> dict | None:
    """Fetch post analysis + optionally all stored comments."""
    post_id: str = args["post_id"]
    include_comments: bool = bool(args.get("include_comments", True))

    # Fetch the post analysis result
    post = await _handle_get_post({"post_id": post_id})

    if not include_comments:
        return {"post": post, "comments": None}

    # Fetch all comments stored for this post
    sql = text(
        """
        SELECT
            c.id,
            c.comment_id,
            c.post_id,
            c.text,
            c.author,
            c.likes,
            c.sentiment,
            c.created_at
        FROM comments c
        WHERE c.post_id = :post_id
        ORDER BY c.likes DESC NULLS LAST, c.created_at ASC
        """
    )

    async with _AsyncSessionLocal() as session:
        rows = (await session.execute(sql, {"post_id": post_id})).mappings().all()

    comments = [
        {
            "id": row["id"],
            "comment_id": row["comment_id"],
            "post_id": row["post_id"],
            "text": row["text"],
            "author": row["author"],
            "likes": row["likes"],
            "sentiment": row["sentiment"],
            "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        }
        for row in rows
    ]

    log.info("get_thread_completed", post_id=post_id, comment_count=len(comments))
    return {"post": post, "comments": comments}


# ---------------------------------------------------------------------------
# Handler: representative_comments
# ---------------------------------------------------------------------------


async def _handle_representative_comments(args: dict[str, Any]) -> list[dict]:
    """Extract representative comments from the stored analysis result JSON.

    Falls back to querying the comments table directly when the
    comment_analysis.representative_comments field is absent.
    """
    post_id: str = args["post_id"]
    sentiment: str = args.get("sentiment", "all")
    limit: int = int(args.get("limit", 5))

    # First try: pull from stored analysis JSON
    sql_result = text(
        """
        SELECT ar.result->'comment_analysis'->'representative_comments' AS rcs
        FROM analysis_results ar
        WHERE ar.post_id = :post_id
        ORDER BY ar.created_at DESC
        LIMIT 1
        """
    )

    async with _AsyncSessionLocal() as session:
        row = (
            await session.execute(sql_result, {"post_id": post_id})
        ).mappings().first()

    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Post not found: {post_id!r}",
        )

    rcs = row["rcs"]  # list[dict] or None
    if rcs and isinstance(rcs, list) and len(rcs) > 0:
        filtered = _filter_representative_comments(rcs, sentiment)
        # Sort by likes descending if available, then slice
        filtered.sort(key=lambda c: c.get("likes", 0) or 0, reverse=True)
        return filtered[:limit]

    # Fallback: query comments table directly
    log.info(
        "representative_comments_fallback_to_table",
        post_id=post_id,
        sentiment=sentiment,
    )
    return await _representative_comments_from_table(post_id, sentiment, limit)


def _filter_representative_comments(
    comments: list[dict],
    sentiment: str,
) -> list[dict]:
    """Filter a list of comment dicts by sentiment bucket."""
    if sentiment == "all":
        return list(comments)
    return [c for c in comments if (c.get("sentiment") or "").lower() == sentiment]


async def _representative_comments_from_table(
    post_id: str,
    sentiment: str,
    limit: int,
) -> list[dict]:
    """Query the comments table directly as a fallback."""
    params: dict[str, Any] = {"post_id": post_id, "limit": limit}
    where_parts = ["c.post_id = :post_id"]

    if sentiment != "all":
        where_parts.append("c.sentiment = :sentiment")
        params["sentiment"] = sentiment

    sql = text(
        f"""
        SELECT
            c.comment_id,
            c.text,
            c.author,
            c.likes,
            c.sentiment
        FROM comments c
        WHERE {' AND '.join(where_parts)}
        ORDER BY c.likes DESC NULLS LAST
        LIMIT :limit
        """
    )

    async with _AsyncSessionLocal() as session:
        rows = (await session.execute(sql, params)).mappings().all()

    return [
        {
            "comment_id": row["comment_id"],
            "text": row["text"],
            "author": row["author"],
            "likes": row["likes"],
            "sentiment": row["sentiment"],
        }
        for row in rows
    ]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", 8001))
    log.info("retrieval_mcp_starting", port=port, stub=_STUB_MODE)
    uvicorn.run("server:app", host="0.0.0.0", port=port, reload=False)

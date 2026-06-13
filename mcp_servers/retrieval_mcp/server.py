"""retrieval-mcp — MCP server for semantic search and post retrieval.

Real MCP server (FastMCP, streamable-HTTP transport). Backed by:
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
PORT                    8101  (default)
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Annotated, Any

import structlog

# Repo root on path for `libs.*` (no-op when PYTHONPATH already provides it).
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from fastmcp import FastMCP
from pydantic import Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
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
# MCP server
# ---------------------------------------------------------------------------

mcp = FastMCP(
    name="retrieval-mcp",
    instructions=(
        "Semantic search (pgvector) + post/thread retrieval (Postgres) over "
        "analyzed posts. Read-only; scoped to a campaign when campaign_id is given."
    ),
)


# ---------------------------------------------------------------------------
# Tool: semantic_search
# ---------------------------------------------------------------------------


@mcp.tool
async def semantic_search(
    query: Annotated[str, Field(description="Natural-language search query.")],
    campaign_id: Annotated[str | None, Field(description="Optional campaign to scope the search.")] = None,
    limit: Annotated[int, Field(description="Max results to return (max 50).", ge=1, le=50)] = 10,
    sentiment_filter: Annotated[
        str | None, Field(description="Optional overall_sentiment filter (positive/negative/neutral/mixed).")
    ] = None,
) -> list[dict]:
    """Vector (pgvector cosine) search over analyzed posts; returns post_id, score,
    campaign_id, overall_sentiment, and post_summary. Falls back to recency in stub mode."""
    limit = min(int(limit), 50)
    log.info("tool_call", tool="semantic_search", campaign_id=campaign_id, stub=_STUB_MODE)

    if _STUB_MODE:
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

    log.info("semantic_search_completed", query=query, hits=len(results), stub=False)
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
# Tool: get_post
# ---------------------------------------------------------------------------


@mcp.tool
async def get_post(
    post_id: Annotated[str, Field(description="Post id (CUID) to fetch the analysis result for.")],
) -> dict:
    """Return the latest stored analysis result for a single post."""
    log.info("tool_call", tool="get_post", post_id=post_id)
    return await _get_post(post_id)


async def _get_post(post_id: str) -> dict:
    """SELECT result FROM analysis_results WHERE post_id=$1 (latest)."""
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
        raise ValueError(f"Post not found: {post_id!r}")

    return {
        "id": row["id"],
        "post_id": row["post_id"],
        "campaign_id": row["campaign_id"],
        "result": row["result"],
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "scraped_at": row["scraped_at"].isoformat() if row["scraped_at"] else None,
    }


# ---------------------------------------------------------------------------
# Tool: get_thread
# ---------------------------------------------------------------------------


@mcp.tool
async def get_thread(
    post_id: Annotated[str, Field(description="Post id (CUID) whose thread to fetch.")],
    include_comments: Annotated[bool, Field(description="Include all stored comments.")] = True,
) -> dict:
    """Fetch a post's analysis result plus (optionally) all of its stored comments,
    ordered by likes."""
    log.info("tool_call", tool="get_thread", post_id=post_id, include_comments=include_comments)
    post = await _get_post(post_id)

    if not include_comments:
        return {"post": post, "comments": None}

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
# Tool: representative_comments
# ---------------------------------------------------------------------------


@mcp.tool
async def representative_comments(
    post_id: Annotated[str, Field(description="Post id (CUID) to pull representative comments for.")],
    sentiment: Annotated[
        str, Field(description="Sentiment bucket filter: positive/negative/neutral, or 'all'.")
    ] = "all",
    limit: Annotated[int, Field(description="Max comments to return.", ge=1, le=50)] = 5,
) -> list[dict]:
    """Return representative comments for a post (from the stored analysis JSON,
    falling back to the comments table), filtered by sentiment and ranked by likes."""
    log.info("tool_call", tool="representative_comments", post_id=post_id, sentiment=sentiment)

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
        row = (await session.execute(sql_result, {"post_id": post_id})).mappings().first()

    if row is None:
        raise ValueError(f"Post not found: {post_id!r}")

    rcs = row["rcs"]  # list[dict] or None
    if rcs and isinstance(rcs, list) and len(rcs) > 0:
        filtered = _filter_representative_comments(rcs, sentiment)
        # Sort by likes descending if available, then slice
        filtered.sort(key=lambda c: c.get("likes", 0) or 0, reverse=True)
        return filtered[:limit]

    # Fallback: query comments table directly
    log.info("representative_comments_fallback_to_table", post_id=post_id, sentiment=sentiment)
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
# Health probe + ASGI app
# ---------------------------------------------------------------------------


@mcp.custom_route("/health", methods=["GET"])
async def health(_request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "service": "retrieval-mcp", "stub_mode": _STUB_MODE})


# ASGI app served by uvicorn: streamable-HTTP MCP endpoint mounted at /mcp.
app = mcp.http_app(path="/mcp")


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", 8101))
    log.info("retrieval_mcp_starting", port=port, stub=_STUB_MODE)
    uvicorn.run(app, host="0.0.0.0", port=port)

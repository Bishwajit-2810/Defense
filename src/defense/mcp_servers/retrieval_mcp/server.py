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
import re
from defense.libs.common.config import get_settings

config = get_settings()
import sys
from typing import Annotated, Any

import structlog

# Repo root on path for `libs.*` (no-op when PYTHONPATH already provides it).

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

_DATABASE_URL: str = config.database_url or "postgresql+asyncpg://defense:defense@localhost:5432/defense"
if _DATABASE_URL.startswith("postgresql://"):
    _DATABASE_URL = _DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)
elif _DATABASE_URL.startswith("postgres://"):
    _DATABASE_URL = _DATABASE_URL.replace("postgres://", "postgresql+asyncpg://", 1)

_STUB_MODE: bool = config.retrieval_mcp_stub

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
from defense.libs.embeddings import embed_text, to_pgvector_literal  # noqa: E402


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


_ALL_CAMPAIGNS = {"all", "all campaigns", "all_campaigns", "*", "any"}
_UNSPECIFIED = {"", "none", "null", "undefined", "n/a"}

# A campaign id is a CUID/UUID/short slug: no spaces, no punctuation beyond -
# and _. An argument that does not match is an unfilled schema placeholder
# ("CUID/UUID of the campaign to query."), not an id.
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def _clean_campaign_id(cid: Any) -> str | None:
    """Normalise an LLM-supplied campaign_id, or raise if it is a placeholder.

    ``None`` means "no campaign filter" and is returned only when the caller
    left the argument out or explicitly asked for every campaign. Junk is
    rejected rather than coerced: dropping the filter silently answers a
    corpus-wide question with a campaign-shaped label on it, which reads as
    correct and is not.
    """
    if cid is None:
        return None
    s = str(cid).strip()
    low = s.lower()
    if low in _UNSPECIFIED or low in _ALL_CAMPAIGNS:
        return None
    if not _ID_RE.match(s):
        raise ValueError(
            f"campaign_id {s!r} is not a campaign id. Pass a real campaign id, "
            "or 'all' to query every campaign."
        )
    return s


@mcp.tool
async def semantic_search(
    query: Annotated[str, Field(description="Natural-language search query.")],
    campaign_id: Annotated[str | None, Field(description="Optional campaign to scope the search.")] = None,
    limit: Annotated[int, Field(description="Max results to return (max 50).", ge=1, le=50)] = 10,
    sentiment_filter: Annotated[
        str | None, Field(description="Optional overall_sentiment filter (positive/negative/neutral/mixed).")
    ] = None,
    tenant_id: Annotated[str | None, Field(description="Optional tenant ID filter.")] = None,
) -> list[dict]:
    """Vector (pgvector cosine) search over analyzed posts; returns post_id, score,
    campaign_id, overall_sentiment, and post_summary. Falls back to recency in stub mode."""
    limit = min(int(limit), 50)
    clean_cid = _clean_campaign_id(campaign_id)
    log.info("tool_call", tool="semantic_search", campaign_id=clean_cid, tenant_id=tenant_id, stub=_STUB_MODE)

    if _STUB_MODE:
        return await _stub_semantic_search(clean_cid, limit, sentiment_filter, tenant_id=tenant_id)

    # --- Real path: embed query and run a pgvector cosine-similarity search ---
    qvec = to_pgvector_literal(embed_text(query))

    params: dict[str, Any] = {"qvec": qvec, "limit": limit}
    where_parts = ["ar.embedding IS NOT NULL"]
    if clean_cid:
        where_parts.append("ar.campaign_id = :campaign_id")
        params["campaign_id"] = clean_cid
    if tenant_id:
        where_parts.append("ar.tenant_id = :tenant_id")
        params["tenant_id"] = tenant_id
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
    tenant_id: str | None = None,
) -> list[dict]:
    """Stub: return first N analysis_results from Postgres matching filters."""
    clean_cid = _clean_campaign_id(campaign_id)
    params: dict[str, Any] = {"limit": limit}
    where_parts = ["1=1"]

    if clean_cid:
        where_parts.append("ar.campaign_id = :campaign_id")
        params["campaign_id"] = clean_cid
    if tenant_id:
        where_parts.append("ar.tenant_id = :tenant_id")
        params["tenant_id"] = tenant_id
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
    tenant_id: Annotated[str | None, Field(description="Optional tenant ID filter.")] = None,
) -> dict:
    """Return the latest stored analysis result for a single post."""
    log.info("tool_call", tool="get_post", post_id=post_id, tenant_id=tenant_id)
    return await _get_post(post_id, tenant_id=tenant_id)


async def _get_post(post_id: str, tenant_id: str | None = None) -> dict:
    """SELECT result FROM analysis_results WHERE post_id=$1 (latest)."""
    pid = str(post_id or "").strip()
    if not pid or "cuid" in pid.lower() or "uuid" in pid.lower():
        return {
            "error": f"Invalid or placeholder post_id: {post_id!r}",
            "post_id": pid,
            "result": None,
        }

    where_parts = ["ar.post_id = :post_id"]
    params: dict[str, Any] = {"post_id": pid}
    if tenant_id:
        where_parts.append("ar.tenant_id = :tenant_id")
        params["tenant_id"] = tenant_id

    sql = text(
        f"""
        SELECT
            ar.id,
            ar.post_id,
            ar.campaign_id,
            ar.result,
            ar.created_at,
            p.scraped_at
        FROM analysis_results ar
        JOIN posts p ON p.id = ar.post_id
        WHERE {' AND '.join(where_parts)}
        ORDER BY ar.created_at DESC
        LIMIT 1
        """
    )

    async with _AsyncSessionLocal() as session:
        row = (await session.execute(sql, params)).mappings().first()

    if row is None:
        log.warning("get_post_not_found", post_id=pid)
        return {
            "error": f"Post not found in database: {pid!r}",
            "post_id": pid,
            "result": None,
        }

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
    tenant_id: Annotated[str | None, Field(description="Optional tenant ID filter.")] = None,
) -> dict:
    """Fetch a post's analysis result plus (optionally) all of its stored comments,
    ordered by likes."""
    pid = str(post_id or "").strip()
    log.info("tool_call", tool="get_thread", post_id=pid, include_comments=include_comments, tenant_id=tenant_id)
    post = await _get_post(pid, tenant_id=tenant_id)

    if not include_comments or post.get("error"):
        return {"post": post, "comments": []}

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
        rows = (await session.execute(sql, {"post_id": pid})).mappings().all()

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

    log.info("get_thread_completed", post_id=pid, comment_count=len(comments))
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
    tenant_id: Annotated[str | None, Field(description="Optional tenant ID filter.")] = None,
) -> list[dict]:
    """Return representative comments for a post (from the stored analysis JSON,
    falling back to the comments table), filtered by sentiment and ranked by likes."""
    log.info("tool_call", tool="representative_comments", post_id=post_id, sentiment=sentiment, tenant_id=tenant_id)

    where_parts = ["ar.post_id = :post_id"]
    params: dict[str, Any] = {"post_id": post_id}
    if tenant_id:
        where_parts.append("ar.tenant_id = :tenant_id")
        params["tenant_id"] = tenant_id

    # First try: pull from stored analysis JSON
    sql_result = text(
        f"""
        SELECT ar.result->'comment_analysis'->'representative_comments' AS rcs
        FROM analysis_results ar
        WHERE {' AND '.join(where_parts)}
        ORDER BY ar.created_at DESC
        LIMIT 1
        """
    )

    async with _AsyncSessionLocal() as session:
        row = (await session.execute(sql_result, params)).mappings().first()

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
# Tool: get_clusters
# ---------------------------------------------------------------------------


@mcp.tool
async def get_clusters(
    campaign_id: Annotated[str | None, Field(description="Optional campaign to scope the clustering.")] = None,
    max_clusters: Annotated[int, Field(description="Max clusters to produce (2..8).", ge=2, le=8)] = 8,
    algo: Annotated[str, Field(description="Clustering algorithm: 'kmeans' or 'hdbscan'.")] = "kmeans",
    tenant_id: Annotated[str | None, Field(description="Optional tenant ID filter.")] = None,
) -> list[dict]:
    """Cluster analyzed posts by embedding similarity (pgvector + k-means/HDBSCAN).
    Returns cluster metadata, sizes, dominant sentiments, representative post summaries, and member post IDs."""
    clean_cid = _clean_campaign_id(campaign_id)
    log.info("tool_call", tool="get_clusters", campaign_id=clean_cid, max_clusters=max_clusters, stub=_STUB_MODE)

    if _STUB_MODE:
        return await _stub_get_clusters(clean_cid, max_clusters, tenant_id=tenant_id)

    from defense.libs.clustering import cluster_embeddings, parse_pgvector

    where_parts = ["ar.embedding IS NOT NULL"]
    params: dict[str, Any] = {"limit": 100}
    if clean_cid:
        where_parts.append("ar.campaign_id = :campaign_id")
        params["campaign_id"] = clean_cid
    if tenant_id:
        where_parts.append("ar.tenant_id = :tenant_id")
        params["tenant_id"] = tenant_id

    sql = text(
        f"""
        SELECT
            ar.post_id,
            ar.embedding::text AS emb,
            ar.result->>'post_summary' AS summary,
            ar.result->>'overall_sentiment' AS sentiment,
            COALESCE(ar.embedding_is_stub, FALSE) AS is_stub
        FROM analysis_results ar
        WHERE {' AND '.join(where_parts)}
        ORDER BY ar.created_at DESC
        LIMIT :limit
        """
    )

    async with _AsyncSessionLocal() as session:
        rows = (await session.execute(sql, params)).mappings().all()

    # No embeddings is a finding ("nothing has been vectorised yet"), not a
    # reason to hand the agent synthetic clusters it cannot tell apart.
    if not rows:
        log.info("get_clusters_no_embeddings", campaign_id=clean_cid)
        return []

    vectors: list[list[float]] = []
    meta: list[dict] = []
    stub_count = 0
    for r in rows:
        vec = parse_pgvector(r["emb"])
        if vec:
            vectors.append(vec)
            if r["is_stub"]:
                stub_count += 1
            meta.append({
                "post_id": r["post_id"],
                "summary": r["summary"] or "",
                "sentiment": r["sentiment"] or "neutral",
            })

    if len(vectors) < 2:
        return [
            {
                "cluster_id": 0,
                "size": len(meta),
                "dominant_sentiment": meta[0]["sentiment"] if meta else "neutral",
                "representative_post_id": meta[0]["post_id"] if meta else "none",
                "representative_summary": meta[0]["summary"] if meta else "No post summary available",
                "member_post_ids": [m["post_id"] for m in meta],
                "is_stub": bool(stub_count),
            }
        ]

    cr = cluster_embeddings(vectors, max_k=max_clusters, algo=algo)

    results: list[dict] = []
    for ci in range(cr.k):
        member_indices = cr.members[ci] if ci < len(cr.members) else []
        if not member_indices:
            continue
        rep_idx = cr.representative_indices[ci] if ci < len(cr.representative_indices) else member_indices[0]

        sent_counts: dict[str, int] = {}
        for mi in member_indices:
            s = meta[mi]["sentiment"]
            sent_counts[s] = sent_counts.get(s, 0) + 1
        dominant_sent = max(sent_counts, key=sent_counts.get) if sent_counts else "neutral"

        rep_meta = meta[rep_idx]
        results.append({
            "cluster_id": ci,
            "size": len(member_indices),
            "dominant_sentiment": dominant_sent,
            "representative_post_id": rep_meta["post_id"],
            "representative_summary": rep_meta["summary"] or "Summary unavailable",
            "member_post_ids": [meta[mi]["post_id"] for mi in member_indices[:25]],
            "is_stub": bool(stub_count),
        })

    log.info("get_clusters_completed", clusters=len(results), total_posts=len(meta))
    return results


async def _stub_get_clusters(
    campaign_id: str | None,
    max_clusters: int,
    tenant_id: str | None = None,
) -> list[dict]:
    """Stub: return synthetic cluster groupings with representative summaries."""
    stub_themes = [
        ("Public reaction to economic policies and fuel price adjustments", "negative"),
        ("Student community discourse and educational reform discussions", "mixed"),
        ("Government infrastructure developments and civic announcements", "positive"),
        ("Law enforcement, security measures, and public safety discussions", "neutral"),
    ]
    k = min(max(2, max_clusters), len(stub_themes))
    return [
        {
            "cluster_id": i,
            "size": 12 + i * 5,
            "dominant_sentiment": stub_themes[i][1],
            "representative_post_id": f"stub-cluster-post-{i:04d}",
            "representative_summary": stub_themes[i][0],
            "member_post_ids": [f"stub-post-{i * 10 + j:04d}" for j in range(5)],
            "is_stub": True,
        }
        for i in range(k)
    ]


# ---------------------------------------------------------------------------
# Tools: stance_by_target / stance_over_time
#
# These live here, not in analytics-mcp, because the per-entity stance rollup is
# written to Postgres and nowhere else: `aggregate_target_stances()` (libs/
# stance_scoring.py) lands at
#     analysis_results.result -> 'comment_analysis' -> 'target_stances'
# keyed by target_id. ClickHouse `analysis_events` carries post-level SENTIMENT
# only, and stance-toward-an-entity is a deliberately separate judgement from
# sentiment-toward-the-post (stage2_llm/prompts.py) — a comment can praise a post
# that attacks an entity. Reading one as the other would report a number the
# pipeline never measured.
# ---------------------------------------------------------------------------


_STANCE_KEYS = ("supportive", "opposing", "neutral")


def _date_filter(
    where_parts: list[str],
    params: dict[str, Any],
    from_date: str | None,
    to_date: str | None,
) -> None:
    """Append an inclusive ar.created_at window for whichever bounds parse.

    Bounds are bound as ``date`` objects, not ISO strings: asyncpg types a
    parameter from the column it is compared against and rejects a str against
    ``timestamptz``. The upper bound is turned into an exclusive next-day
    boundary here rather than in SQL, so `to_date` stays inclusive for the caller
    without a date-arithmetic expression around the bind.
    """
    from datetime import date as _date, timedelta as _timedelta

    def _iso(v: Any) -> _date | None:
        s = str(v or "").strip()[:10]
        try:
            return _date.fromisoformat(s)
        except ValueError:
            return None

    f_d, t_d = _iso(from_date), _iso(to_date)
    if f_d:
        where_parts.append("ar.created_at >= :from_date")
        params["from_date"] = f_d
    if t_d:
        where_parts.append("ar.created_at < :to_date_exclusive")
        params["to_date_exclusive"] = t_d + _timedelta(days=1)


async def _latest_target_stances(
    campaign_id: str | None,
    tenant_id: str | None,
    from_date: str | None,
    to_date: str | None,
    bucket: str | None = None,
) -> list[dict]:
    """One row per post (the latest analysis of it) with its target_stances blob.

    ``bucket`` adds a date_trunc'd period column for the time-series tool.
    """
    where_parts = ["ar.result -> 'comment_analysis' -> 'target_stances' IS NOT NULL"]
    params: dict[str, Any] = {}
    if campaign_id:
        where_parts.append("ar.campaign_id = :campaign_id")
        params["campaign_id"] = campaign_id
    if tenant_id:
        where_parts.append("ar.tenant_id = :tenant_id")
        params["tenant_id"] = tenant_id
    _date_filter(where_parts, params, from_date, to_date)

    period_col = (
        f"date_trunc('{bucket}', ar.created_at) AS period," if bucket else ""
    )

    sql = text(
        f"""
        SELECT DISTINCT ON (ar.post_id)
            ar.post_id,
            {period_col}
            ar.result -> 'comment_analysis' -> 'target_stances' AS target_stances
        FROM analysis_results ar
        WHERE {' AND '.join(where_parts)}
        ORDER BY ar.post_id, ar.created_at DESC
        """
    )

    async with _AsyncSessionLocal() as session:
        rows = (await session.execute(sql, params)).mappings().all()
    return [dict(r) for r in rows]


def _blank_bucket(target_id: str) -> dict:
    return {
        "target_id": target_id,
        "display": None,
        "mentions": 0,
        "supportive": 0,
        "opposing": 0,
        "neutral": 0,
        "posts_mentioning": 0,
        "method_breakdown": {},
    }


def _fold_target_stances(
    rollup: dict[str, dict],
    blob: Any,
    target_id: str | None,
) -> None:
    """Merge one post's target_stances dict into the corpus-level rollup."""
    if not isinstance(blob, dict):
        return
    for tid, entry in blob.items():
        if target_id and tid != target_id:
            continue
        if not isinstance(entry, dict):
            continue
        bucket = rollup.setdefault(tid, _blank_bucket(tid))
        bucket["display"] = bucket["display"] or entry.get("display")
        bucket["mentions"] += int(entry.get("mentions") or 0)
        for key in _STANCE_KEYS:
            bucket[key] += int(entry.get(key) or 0)
        bucket["posts_mentioning"] += 1
        for method, count in (entry.get("method_breakdown") or {}).items():
            bucket["method_breakdown"][method] = (
                bucket["method_breakdown"].get(method, 0) + int(count or 0)
            )


def _finalise_target(bucket: dict) -> dict:
    """Add shares, polarity and provenance to a folded target bucket."""
    mentions = bucket["mentions"] or 0
    denom = mentions or 1
    sup, opp, neu = (bucket[k] for k in _STANCE_KEYS)
    mb = bucket["method_breakdown"]
    return {
        **bucket,
        "support_share": round(sup / denom, 3),
        "oppose_share": round(opp / denom, 3),
        "neutral_share": round(neu / denom, 3),
        "net_polarity": sup - opp,
        "dominant_stance": (
            "opposing" if opp > sup else ("supportive" if sup > opp else "mixed")
        ),
        # Which scorer produced these labels — the LLM stance call or the
        # deterministic clause-level cues. Reported so a finding can state its
        # own provenance instead of implying one.
        "method": "llm" if mb.get("llm") else ("deterministic" if mb else "none"),
    }


@mcp.tool
async def stance_by_target(
    campaign_id: Annotated[str | None, Field(description="Campaign to scope to; 'all' for every campaign.")] = None,
    from_date: Annotated[str | None, Field(description="Optional inclusive start date (YYYY-MM-DD).")] = None,
    to_date: Annotated[str | None, Field(description="Optional inclusive end date (YYYY-MM-DD).")] = None,
    target_id: Annotated[str | None, Field(description="Optional watchlist target id to filter to (e.g. 'pm_hasina').")] = None,
    tenant_id: Annotated[str | None, Field(description="Optional tenant ID filter.")] = None,
) -> list[dict]:
    """Stance distribution per watchlist target entity (supportive / opposing / neutral
    counts, shares, net polarity and scorer provenance), aggregated over the analysed
    posts of a campaign. Targets nobody mentioned are absent, not zero-filled — an
    empty result means no watchlist entity was mentioned in the selected posts."""
    clean_cid = _clean_campaign_id(campaign_id)
    log.info(
        "tool_call", tool="stance_by_target", campaign_id=clean_cid,
        target_id=target_id, scope="all_campaigns" if clean_cid is None else "campaign",
    )

    rows = await _latest_target_stances(clean_cid, tenant_id, from_date, to_date)

    rollup: dict[str, dict] = {}
    for row in rows:
        _fold_target_stances(rollup, row.get("target_stances"), target_id)

    out = [_finalise_target(b) for b in rollup.values()]
    out.sort(key=lambda t: t["mentions"], reverse=True)
    log.info("stance_by_target_completed", targets=len(out), posts_scanned=len(rows))
    return out


@mcp.tool
async def stance_over_time(
    campaign_id: Annotated[str | None, Field(description="Campaign to scope to; 'all' for every campaign.")] = None,
    from_date: Annotated[str | None, Field(description="Optional inclusive start date (YYYY-MM-DD).")] = None,
    to_date: Annotated[str | None, Field(description="Optional inclusive end date (YYYY-MM-DD).")] = None,
    granularity: Annotated[str, Field(description="Bucket size: 'hour', 'day' or 'week'.")] = "day",
    target_id: Annotated[str | None, Field(description="Optional watchlist target id; omit to sum all targets.")] = None,
    tenant_id: Annotated[str | None, Field(description="Optional tenant ID filter.")] = None,
) -> list[dict]:
    """Time series of per-target stance (supportive / opposing / neutral and net polarity),
    bucketed by the analysis timestamp. One row per (period, target)."""
    clean_cid = _clean_campaign_id(campaign_id)
    bucket = granularity if granularity in ("hour", "day", "week") else "day"
    log.info(
        "tool_call", tool="stance_over_time", campaign_id=clean_cid,
        target_id=target_id, granularity=bucket,
    )

    rows = await _latest_target_stances(
        clean_cid, tenant_id, from_date, to_date, bucket=bucket
    )

    periods: dict[str, dict[str, dict]] = {}
    for row in rows:
        period = row.get("period")
        key = period.isoformat() if hasattr(period, "isoformat") else str(period)
        _fold_target_stances(periods.setdefault(key, {}), row.get("target_stances"), target_id)

    out: list[dict] = []
    for period in sorted(periods):
        for target in periods[period].values():
            out.append({"period": period, **_finalise_target(target)})

    log.info("stance_over_time_completed", rows=len(out), posts_scanned=len(rows))
    return out


# ---------------------------------------------------------------------------
# Tool: coverage_stats
#
# Also Postgres-side: answering "what fraction of posts is analysed?" needs the
# `posts` table as the denominator, and "are these embeddings meaningful?" needs
# analysis_results.embedding_is_stub. Neither exists in ClickHouse, which only
# ever receives rows for posts that were already analysed.
# ---------------------------------------------------------------------------


@mcp.tool
async def coverage_stats(
    campaign_id: Annotated[str | None, Field(description="Campaign to scope to; 'all' for every campaign.")] = None,
    tenant_id: Annotated[str | None, Field(description="Optional tenant ID filter.")] = None,
) -> dict:
    """Audit analysis coverage: how many collected posts have been analysed, how many
    of their reported comments were actually stored and labelled, how many posts carry
    a coverage anomaly, and how many embeddings are deterministic stubs rather than
    semantic vectors (stub vectors make semantic_search results non-meaningful)."""
    clean_cid = _clean_campaign_id(campaign_id)
    log.info("tool_call", tool="coverage_stats", campaign_id=clean_cid)

    post_where = ["1=1"]
    params: dict[str, Any] = {}
    if clean_cid:
        post_where.append("p.campaign_id = :campaign_id")
        params["campaign_id"] = clean_cid
    if tenant_id:
        post_where.append("p.tenant_id = :tenant_id")
        params["tenant_id"] = tenant_id

    ar_where = ["1=1"]
    if clean_cid:
        ar_where.append("ar.campaign_id = :campaign_id")
    if tenant_id:
        ar_where.append("ar.tenant_id = :tenant_id")

    sql = text(
        f"""
        WITH collected AS (
            SELECT count(*) AS total_posts
            FROM posts p
            WHERE {' AND '.join(post_where)}
        ),
        latest AS (
            SELECT DISTINCT ON (ar.post_id)
                ar.post_id,
                ar.embedding,
                ar.embedding_is_stub,
                ar.result
            FROM analysis_results ar
            WHERE {' AND '.join(ar_where)}
            ORDER BY ar.post_id, ar.created_at DESC
        )
        SELECT
            (SELECT total_posts FROM collected)                          AS total_posts,
            count(*)                                                     AS analyzed_posts,
            COALESCE(SUM((result->'engagement'->>'comment_count')::numeric), 0)      AS reported_comments,
            COALESCE(SUM((result->'engagement'->>'stored_comments')::numeric), 0)    AS stored_comments,
            COALESCE(SUM((result->'comment_analysis'->>'analyzed')::numeric), 0)     AS analyzed_comments,
            AVG((result->'comment_analysis'->>'coverage')::numeric)                  AS mean_post_coverage,
            count(*) FILTER (
                WHERE result->'comment_analysis'->'coverage_anomaly' IS NOT NULL
                  AND result->'comment_analysis'->>'coverage_anomaly' <> 'null'
            )                                                            AS coverage_anomalies,
            count(*) FILTER (WHERE embedding IS NOT NULL)                AS posts_with_embedding,
            count(*) FILTER (WHERE COALESCE(embedding_is_stub, FALSE))   AS stub_embeddings
        FROM latest
        """
    )

    async with _AsyncSessionLocal() as session:
        row = (await session.execute(sql, params)).mappings().first()

    def _int(v: Any) -> int:
        return int(v or 0)

    total_posts = _int(row["total_posts"]) if row else 0
    analyzed_posts = _int(row["analyzed_posts"]) if row else 0
    reported = _int(row["reported_comments"]) if row else 0
    stored = _int(row["stored_comments"]) if row else 0
    analyzed_comments = _int(row["analyzed_comments"]) if row else 0
    embedded = _int(row["posts_with_embedding"]) if row else 0
    stubbed = _int(row["stub_embeddings"]) if row else 0
    mean_cov = row["mean_post_coverage"] if row else None

    notes: list[str] = []
    if analyzed_posts < total_posts:
        notes.append(
            f"{total_posts - analyzed_posts} collected post(s) have no analysis result yet."
        )
    if stubbed:
        notes.append(
            f"{stubbed} of {embedded} embeddings are deterministic stubs — semantic_search "
            "over those rows returns arbitrary neighbours, not semantically similar posts."
        )
    if row and _int(row["coverage_anomalies"]):
        notes.append(
            f"{_int(row['coverage_anomalies'])} post(s) stored more comments than the "
            "platform reported (coverage anomaly)."
        )

    return {
        "campaign_id": clean_cid or "all",
        "total_posts": total_posts,
        "analyzed_posts": analyzed_posts,
        "post_analysis_coverage": round(analyzed_posts / total_posts, 4) if total_posts else 0.0,
        "reported_comments": reported,
        "stored_comments": stored,
        "analyzed_comments": analyzed_comments,
        "comment_coverage": round(stored / reported, 4) if reported else 0.0,
        "mean_post_comment_coverage": round(float(mean_cov), 4) if mean_cov is not None else None,
        "coverage_anomalies": _int(row["coverage_anomalies"]) if row else 0,
        "posts_with_embedding": embedded,
        "stub_embeddings": stubbed,
        "stub_embedding_share": round(stubbed / embedded, 4) if embedded else 0.0,
        "notes": notes,
    }


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

    port = config.retrieval_mcp_port
    log.info("retrieval_mcp_starting", port=port, stub=_STUB_MODE)
    uvicorn.run(app, host="0.0.0.0", port=port)

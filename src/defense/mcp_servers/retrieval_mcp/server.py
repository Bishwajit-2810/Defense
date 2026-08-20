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

import asyncio
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
from defense.libs.embeddings import (  # noqa: E402
    active_model_name,
    embed_text_with_provenance,
    to_pgvector_literal,
)
from defense.libs.retrieval import (  # noqa: E402
    candidate_pool,
    fuse,
    lexical_terms,
    rerank,
)

_HYBRID: bool = config.retrieval_hybrid
_CHUNK_SEARCH: bool = config.retrieval_chunk_search
_CLUSTER_SCAN_CAP: int = config.retrieval_cluster_scan_cap


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


#: Words that only ever appear in an unfilled schema placeholder, never in a real
#: id. `_ID_RE` alone does not catch these: "post-uuid" and "cuid-of-the-post"
#: are made of legal id characters and match it happily.
_ID_PLACEHOLDER_WORDS = ("cuid", "uuid", "post_id", "postid", "example", "placeholder")


def _clean_post_id(pid: Any, *, field: str = "post_id") -> str | None:
    """Normalise an LLM-supplied post_id, or raise if it is a placeholder.

    Same contract as :func:`_clean_campaign_id`, and for the same reason — but
    this one is load-bearing in a way the campaign version is not, because
    ``search_comments`` takes ``post_id`` as an OPTIONAL filter.

    The previous behaviour dropped an unrecognised value and searched on:

        search_comments(query=…, post_id="<CUID>")  ->  5 comments from 4
        DIFFERENT posts, presented as the comments on one post

    which is the failure `_clean_campaign_id` exists to prevent, one field over.
    Worse, it inverted the incentive an agent learns from: a correctly scoped
    call can legitimately return zero rows, while the placeholder call always
    returns something. Rejecting it means the model is told to go and fetch a
    real id — which the runner's system directive already instructs it to do.

    ``None`` (argument omitted, or explicitly unspecified) means "no post filter"
    and is the only value that widens the search.
    """
    if pid is None:
        return None
    s = str(pid).strip()
    low = s.lower()
    if low in _UNSPECIFIED:
        return None
    if low in _ALL_CAMPAIGNS:
        raise ValueError(
            f"{field} {s!r} names every post, but {field} identifies exactly one. "
            f"Omit {field} to search across all posts."
        )
    if any(w in low for w in _ID_PLACEHOLDER_WORDS) or not _ID_RE.match(s):
        raise ValueError(
            f"{field} {s!r} is not a post id — it looks like an unfilled "
            f"placeholder. Call top_posts or semantic_search first and pass a real "
            f"post_id from the result, or omit {field} to search across all posts."
        )
    return s


# The projection every retrieval arm returns, so a fused row is assembled from
# whichever arm found it without a second round trip.
_POST_PROJECTION = """
    ar.post_id,
    ar.campaign_id,
    ar.result->>'overall_sentiment'  AS overall_sentiment,
    ar.result->>'post_summary'       AS post_summary,
    ar.result->>'post_text'          AS post_text,
    COALESCE(ar.embedding_is_stub, FALSE) AS embedding_is_stub
"""

# The lexical arm's match expression. It must stay character-for-character in
# step with `idx_analysis_fts_simple` in deploy/init-db.sql — Postgres matches an
# expression index by the expression, so a stray space here silently turns an
# index scan into a sequential scan over the whole table.
_FTS_EXPR = (
    "to_tsvector('simple', "
    "coalesce(result->>'post_summary', '') || ' ' || "
    "coalesce(result->>'post_text', ''))"
)


def _tenant_scope(
    where_parts: list[str],
    params: dict[str, Any],
    tenant_id: str | None,
) -> tuple[list[str], dict[str, Any]]:
    """Add the tenant predicate to an arm's own WHERE clause.

    Each arm applies this itself rather than trusting the caller's
    ``where_parts`` to already contain it. Tenant isolation that depends on
    every call site remembering a predicate is isolation that holds until
    somebody adds a call site — and these arms are built to be reused (the
    retrieval evaluation harness imports them directly).
    """
    if not tenant_id:
        return list(where_parts), dict(params)
    return [*where_parts, "ar.tenant_id = :tenant_id"], {**params, "tenant_id": tenant_id}


async def _vector_arm(
    session: AsyncSession,
    qvec: str,
    where_parts: list[str],
    params: dict[str, Any],
    pool: int,
    tenant_id: str | None = None,
) -> list[dict]:
    """kNN over analysis_results.embedding, best cosine similarity first."""
    scoped, scoped_params = _tenant_scope(where_parts, params, tenant_id)
    sql = text(
        f"""
        SELECT {_POST_PROJECTION},
               1 - (ar.embedding <=> CAST(:qvec AS vector)) AS vector_score
        FROM analysis_results ar
        WHERE {' AND '.join([*scoped, 'ar.embedding IS NOT NULL'])}
        ORDER BY ar.embedding <=> CAST(:qvec AS vector)
        LIMIT :pool
        """
    )
    rows = (
        await session.execute(sql, {**scoped_params, "qvec": qvec, "pool": pool})
    ).mappings().all()
    return [dict(r) for r in rows]


async def _chunk_arm(
    session: AsyncSession,
    qvec: str,
    where_parts: list[str],
    params: dict[str, Any],
    pool: int,
    tenant_id: str | None = None,
) -> list[dict]:
    """kNN over post_chunks, collapsed to one best chunk per post.

    Replaces the post-level vector arm when chunks are available. A post is
    ranked by its BEST-matching chunk rather than by the average of everything it
    says, which is the entire argument for chunking: a 5,463-character post that
    argues three things is reachable by a query about any one of them.

    The collapse happens after a bounded kNN rather than over the whole table, so
    the HNSW index still does the work — ``DISTINCT ON`` across all chunks would
    force a full scan and sort. Over-fetching 3x the pool covers the case where a
    single verbose post owns several of the top chunks.

    Degrades to ``[]`` (caller falls back to the post-level arm) when the table
    is absent, so an un-migrated deployment loses chunk precision rather than
    search.
    """
    scoped_pc = ["1=1"]
    pc_params = dict(params)
    if tenant_id:
        scoped_pc.append("pc.tenant_id = :tenant_id")
        pc_params["tenant_id"] = tenant_id
    if "campaign_id" in params:
        scoped_pc.append("pc.campaign_id = :campaign_id")

    # `where_parts` is written against the `ar` alias, so it needs
    # analysis_results in scope.
    outer, outer_params = _tenant_scope(where_parts, pc_params, tenant_id)

    # Predicates beyond tenant/campaign — sentiment and the date window — live on
    # analysis_results, and applying them only in the OUTER query silently loses
    # rows: the inner kNN takes the `chunk_pool` nearest chunks corpus-wide and
    # the filter then deletes most of them. Measured on this corpus with
    # sentiment='neutral' (4 posts of 50): the chunk arm returned 0 where the
    # post-level arm returned 4, because none of the nearest chunks belonged to a
    # neutral post. Total loss is masked by the caller's fallback to
    # `_vector_arm`; PARTIAL loss is not masked at all, and begins as soon as the
    # corpus exceeds `chunk_pool` chunks (120 at the default limit, against 86
    # today — roughly 17 posts of headroom).
    #
    # So the filter is pushed into the inner CTE, but ONLY when one exists. In the
    # common unfiltered case (agents are told to omit the date window) the inner
    # query keeps its original shape, with no join, so the HNSW plan is unchanged.
    # When a filter IS present the join may cost the index — that is the standard
    # filtered-ANN tradeoff, and a narrowed candidate set is exactly when a scan
    # is affordable. Correctness first either way.
    inner_filters = [p for p in where_parts if p != "1=1"]
    inner_join = ""
    if inner_filters:
        inner_join = "JOIN analysis_results ar ON ar.post_id = pc.post_id"
        scoped_pc = [*scoped_pc, *inner_filters]

    sql = text(
        f"""
        WITH top_chunks AS (
            SELECT pc.post_id,
                   pc.chunk_idx,
                   pc.text,
                   COALESCE(pc.embedding_is_stub, FALSE) AS embedding_is_stub,
                   pc.embedding <=> CAST(:qvec AS vector) AS dist
            FROM post_chunks pc
            {inner_join}
            WHERE {' AND '.join(scoped_pc)}
              AND pc.embedding IS NOT NULL
            ORDER BY pc.embedding <=> CAST(:qvec AS vector)
            LIMIT :chunk_pool
        ),
        best AS (
            SELECT DISTINCT ON (post_id)
                   post_id, chunk_idx, text, embedding_is_stub, dist
            FROM top_chunks
            ORDER BY post_id, dist
        )
        SELECT b.post_id,
               b.chunk_idx,
               b.text                            AS matched_chunk,
               b.embedding_is_stub,
               ar.campaign_id,
               ar.result->>'overall_sentiment'   AS overall_sentiment,
               ar.result->>'post_summary'        AS post_summary,
               ar.result->>'post_text'           AS post_text,
               1 - b.dist                        AS vector_score
        FROM best b
        JOIN analysis_results ar ON ar.post_id = b.post_id
        WHERE {' AND '.join(outer)}
        ORDER BY b.dist
        LIMIT :pool
        """
    )
    try:
        rows = (
            await session.execute(
                sql,
                {**outer_params, "qvec": qvec, "pool": pool, "chunk_pool": pool * 3},
            )
        ).mappings().all()
    except Exception as exc:
        log.warning("chunk_arm_unavailable", error=str(exc), detail="falling back to post vectors")
        return []
    return [dict(r) for r in rows]


async def _lexical_arm(
    session: AsyncSession,
    query: str,
    where_parts: list[str],
    params: dict[str, Any],
    pool: int,
    tenant_id: str | None = None,
) -> list[dict]:
    """The lexical half of hybrid retrieval on its own: ``fts`` fused with ``trgm``.

    Not used by :func:`semantic_search`, which fuses ``fts`` and ``trgm`` as two
    of its three arms directly so that a row found by both outranks a row found
    by one. This composes the same two arms with the same RRF and no dense arm,
    which is what the evaluation harness's ``lexical`` configuration measures —
    "what would this query retrieve with the vectors switched off". Keep it
    fusing rather than merging: an earlier version scored the two signals into
    one arm with ``GREATEST(ts_rank, similarity)``, a max across two unrelated
    scales, and that cost 0.10 of MRR@10 (see :func:`_trgm_arm`).

    Each sub-arm returns ``[]`` rather than raising when its extension or index
    is missing, so losing lexical matching costs recall, not the query.
    """
    fts, trgm = await asyncio.gather(
        _fts_arm(session, query, where_parts, params, pool, tenant_id),
        _trgm_arm(session, query, where_parts, params, pool, tenant_id),
    )
    by_id = {r["post_id"]: r for r in [*trgm, *fts]}
    ranked = fuse(
        {"fts": [r["post_id"] for r in fts], "trgm": [r["post_id"] for r in trgm]}
    )
    return [by_id[pid] for pid, _s, _a in ranked if pid in by_id][:pool]


async def _fts_arm(
    session: AsyncSession,
    query: str,
    where_parts: list[str],
    params: dict[str, Any],
    pool: int,
    tenant_id: str | None = None,
) -> list[dict]:
    """Full-text search over caption + summary, ranked by ts_rank.

    OR, not AND. ``plainto_tsquery`` conjoins every term, so an eight-token
    question demanded that all eight appear in one caption — which never
    happened: across the 32-query evaluation set that predicate matched ZERO
    rows. The FTS half of hybrid retrieval was dead, the GIN index built for it
    was never used, and the numbers attributed to "full-text + trigram" were
    trigram alone. Fixing it to OR took lexical recall@10 from 0.375 to 0.750.

    ``to_tsquery('simple', 'a | b | c')`` matches a post containing ANY term and
    lets ts_rank discriminate — a post matching four terms outranks one matching
    one. Safe to interpolate: ``lexical_terms`` emits word characters only, so no
    tsquery operator can reach the parser. ``websearch_to_tsquery`` is not an
    alternative; it also conjoins, and scored zero on the same set.
    """
    terms = lexical_terms(query)
    if not terms:
        return []
    scoped, scoped_params = _tenant_scope(where_parts, params, tenant_id)
    sql = text(
        f"""
        SELECT {_POST_PROJECTION},
               ts_rank({_FTS_EXPR}, to_tsquery('simple', :or_query)) AS lexical_score
        FROM analysis_results ar
        WHERE {' AND '.join(scoped)}
          AND {_FTS_EXPR} @@ to_tsquery('simple', :or_query)
        ORDER BY lexical_score DESC
        LIMIT :pool
        """
    )
    try:
        rows = (
            await session.execute(
                sql, {**scoped_params, "or_query": " | ".join(terms), "pool": pool}
            )
        ).mappings().all()
    except Exception as exc:
        log.warning("fts_arm_unavailable", error=str(exc))
        return []
    return [dict(r) for r in rows]


async def _trgm_arm(
    session: AsyncSession,
    query: str,
    where_parts: list[str],
    params: dict[str, Any],
    pool: int,
    tenant_id: str | None = None,
) -> list[dict]:
    """Trigram similarity over the caption — the fuzzy half of lexical matching.

    Separate from FTS rather than folded into it. They used to share one arm via
    ``GREATEST(ts_rank, similarity)``, which is a max across two scales that have
    nothing to do with each other — the exact comparison RRF exists to avoid, and
    it cost real ranking quality: splitting them raised MRR@10 from 0.551 to
    0.648 on the evaluation set with no other change.

    What it catches that FTS cannot: a transliterated Bangla name spelled three
    different ways, where no token matches exactly but the trigrams overlap.
    """
    terms = lexical_terms(query)
    if not terms:
        return []
    scoped, scoped_params = _tenant_scope(where_parts, params, tenant_id)
    sql = text(
        f"""
        SELECT {_POST_PROJECTION},
               similarity(coalesce(ar.result->>'post_text', ''), :probe) AS lexical_score
        FROM analysis_results ar
        WHERE {' AND '.join(scoped)}
          AND similarity(coalesce(ar.result->>'post_text', ''), :probe) > 0.1
        ORDER BY lexical_score DESC
        LIMIT :pool
        """
    )
    try:
        rows = (
            await session.execute(
                sql, {**scoped_params, "probe": " ".join(terms), "pool": pool}
            )
        ).mappings().all()
    except Exception as exc:
        log.warning("trgm_arm_unavailable", error=str(exc))
        return []
    return [dict(r) for r in rows]


@mcp.tool
async def semantic_search(
    query: Annotated[str, Field(description="Natural-language search query.")],
    campaign_id: Annotated[str | None, Field(description="Optional campaign to scope the search.")] = None,
    limit: Annotated[int, Field(description="Max results to return (max 50).", ge=1, le=50)] = 10,
    sentiment_filter: Annotated[
        str | None, Field(description="Optional overall_sentiment filter (positive/negative/neutral/mixed).")
    ] = None,
    tenant_id: Annotated[str | None, Field(description="Optional tenant ID filter.")] = None,
    from_date: Annotated[
        str | None,
        Field(description="Optional inclusive start date (YYYY-MM-DD). Omit to search the whole corpus."),
    ] = None,
    to_date: Annotated[
        str | None,
        Field(description="Optional inclusive end date (YYYY-MM-DD). Omit to search the whole corpus."),
    ] = None,
) -> list[dict]:
    """Hybrid search over analysed posts: pgvector cosine kNN fused with a lexical
    (full-text + trigram) scan by reciprocal rank fusion.

    Each row carries `embedding_is_stub` — TRUE means that post's vector is a
    deterministic hash, NOT a semantic embedding, so its position in the ranking
    is arbitrary and only the lexical arm's contribution is meaningful. Report
    that rather than describing such a row as "semantically similar".

    `matched_by` names the arms that found the row ("chunk" or "vector" for
    meaning, "fts" for exact words, "trgm" for fuzzy spelling); a row found by
    several arms is a stronger match than one found by a single arm.

    `score` is a rank-fusion score, NOT a similarity or a confidence. It is
    computed from each arm's RANK, so a top hit scores about 0.016 no matter how
    good it is — use it to order results and never report it as a relevance
    figure. `vector_similarity`, when present, IS a real cosine similarity in
    [-1, 1]; it is absent when only the lexical arms matched the row.

    When present, `matched_chunk` is the passage of the post that actually
    matched and `chunk_idx` is its position — quote from that passage rather than
    the start of the post, but ALWAYS cite the `post_id`, and call `get_post` when
    you need the full document. Optional from_date/to_date scope by analysis
    timestamp — omit them to search the whole corpus, which is usually what you
    want."""
    limit = min(int(limit), 50)
    clean_cid = _clean_campaign_id(campaign_id)
    log.info(
        "tool_call", tool="semantic_search", campaign_id=clean_cid,
        tenant_id=tenant_id, stub=_STUB_MODE, hybrid=_HYBRID,
        from_date=from_date, to_date=to_date,
    )

    if _STUB_MODE:
        return await _stub_semantic_search(clean_cid, limit, sentiment_filter, tenant_id=tenant_id)

    # Filters shared by both arms. Tenancy is deliberately NOT here — each arm
    # applies it itself (see _tenant_scope), so isolation does not depend on
    # every call site remembering to build the predicate.
    where_parts: list[str] = ["1=1"]
    params: dict[str, Any] = {}
    if clean_cid:
        where_parts.append("ar.campaign_id = :campaign_id")
        params["campaign_id"] = clean_cid
    if sentiment_filter:
        where_parts.append("ar.result->>'overall_sentiment' = :sentiment")
        params["sentiment"] = sentiment_filter
    # The same window helper the stance tools use. semantic_search had no date
    # argument at all, which on a corpus roughly three months behind the current
    # date left recency scoping impossible to ask for (§3.6).
    _date_filter(where_parts, params, from_date, to_date)

    # Over-fetch per arm so fusion and reranking have something to reorder: if
    # each arm returns exactly `limit` rows, nothing ranked limit+1 by either arm
    # can ever be promoted, and fusion becomes a no-op reshuffle.
    pool = candidate_pool(limit)
    query_vec, query_is_stub = embed_text_with_provenance(query)
    qvec = to_pgvector_literal(query_vec)

    async with _AsyncSessionLocal() as session:
        # Chunk vectors first when available: a post is then ranked by its
        # best-matching passage rather than by the average of everything it says.
        # Falls back to the post-level arm when chunking is off or the table has
        # not been migrated in — precision degrades, search does not.
        vector_arm_name = "vector"
        vector_rows: list[dict] = []
        if _CHUNK_SEARCH:
            vector_rows = await _chunk_arm(
                session, qvec, where_parts, params, pool, tenant_id=tenant_id
            )
            if vector_rows:
                vector_arm_name = "chunk"
        if not vector_rows:
            vector_rows = await _vector_arm(
                session, qvec, where_parts, params, pool, tenant_id=tenant_id
            )
        # Full-text and trigram are fused as SEPARATE arms, not merged into one
        # lexical score. They measure different things on incomparable scales —
        # combining them with GREATEST() cost 0.10 of MRR@10 against doing this.
        fts_rows, trgm_rows = (
            await asyncio.gather(
                _fts_arm(session, query, where_parts, params, pool, tenant_id=tenant_id),
                _trgm_arm(session, query, where_parts, params, pool, tenant_id=tenant_id),
            )
            if _HYBRID
            else ([], [])
        )

    lexical_rows = [*trgm_rows, *fts_rows]
    by_id: dict[str, dict] = {}
    for row in [*lexical_rows, *vector_rows]:
        by_id.setdefault(row["post_id"], row).update(
            {k: v for k, v in row.items() if v is not None}
        )

    arms: dict[str, list[str]] = {vector_arm_name: [r["post_id"] for r in vector_rows]}
    if fts_rows:
        arms["fts"] = [r["post_id"] for r in fts_rows]
    if trgm_rows:
        arms["trgm"] = [r["post_id"] for r in trgm_rows]

    # A hash-stub query vector ranks nothing meaningfully, so its arm must not
    # outvote the arms that do. Down-weighting rather than dropping it keeps the
    # tool's behaviour continuous across the stub-mode flip and still lets kNN
    # supply candidates when the lexical arms find none.
    weights = {vector_arm_name: 0.2 if query_is_stub else 1.0}
    fused = fuse(arms, weights=weights)

    candidates: list[dict] = []
    for post_id, score, matched_by in fused[: max(pool, limit)]:
        row = by_id.get(post_id)
        if row is None:  # pragma: no cover — every fused id came from an arm
            continue
        candidates.append(
            {
                "post_id": post_id,
                "score": round(float(score), 6),
                "campaign_id": row.get("campaign_id"),
                "overall_sentiment": row.get("overall_sentiment"),
                "post_summary": row.get("post_summary"),
                # §5.2: the honesty gap real embeddings do NOT close. /v1/search
                # has always returned this; the tool the agents actually use
                # returned nothing of the kind, so an agent asserting "the most
                # semantically similar posts are…" was asserting something it
                # had no way to check — and in the default configuration that
                # assertion is false.
                "embedding_is_stub": bool(row.get("embedding_is_stub")) or query_is_stub,
                "matched_by": matched_by,
                # Kept for the reranker, dropped before the row is returned:
                # captions run to several KB and the agent already has
                # post_summary plus get_post for the full document.
                #
                # The matched CHUNK is preferred as the rerank input when there
                # is one: the cross-encoder is being asked "is this relevant to
                # the query", and handing it 5 KB of caption to judge a match
                # that happened in one passage is the same averaging problem
                # chunking exists to solve, moved one stage later.
                "_text": (
                    row.get("matched_chunk")
                    or row.get("post_text")
                    or row.get("post_summary")
                    or ""
                ),
            }
        )
        # The actual cosine similarity, when a dense arm found this row. `score`
        # above is an RRF score — it is built from RANKS, so its absolute value
        # is an artefact of position (rank 1 in one arm is always 1/61 ≈ 0.0164)
        # and says nothing about how similar anything is. Handing an LLM only
        # that invites it to read a perfectly good top hit as a 1.6% match.
        # This is the number that is on a meaningful scale; it is absent when
        # only the lexical arms matched, which is itself worth knowing.
        if row.get("vector_score") is not None:
            candidates[-1]["vector_similarity"] = round(float(row["vector_score"]), 4)
        if row.get("matched_chunk") is not None:
            # Citation granularity (§3.5). The post_id is still the citation —
            # see the agent prompts — and this says WHERE in the post the match
            # was, so a briefing can quote the right passage instead of the
            # first paragraph.
            candidates[-1]["chunk_idx"] = row.get("chunk_idx")
            candidates[-1]["matched_chunk"] = (row.get("matched_chunk") or "")[:400]

    ranked, was_reranked = rerank(query, candidates, text_key="_text", limit=limit)
    results = [{k: v for k, v in row.items() if k != "_text"} for row in ranked[:limit]]

    stub_rows = sum(1 for r in results if r["embedding_is_stub"])
    if query_is_stub or stub_rows:
        log.warning(
            "semantic_search_over_stub_vectors",
            query_is_stub=query_is_stub,
            stub_rows=stub_rows,
            total_rows=len(results),
            detail="vector ranking is not semantically meaningful",
        )
    log.info(
        "semantic_search_completed",
        query=query, hits=len(results), stub=False,
        vector_hits=len(vector_rows), lexical_hits=len(lexical_rows),
        reranked=was_reranked,
    )
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

    # Same shape as the real path, including the provenance fields: a caller
    # that only ever sees stub output must not learn a row shape that the live
    # server does not produce. `embedding_is_stub` is True because no vector was
    # consulted at all — these are the newest rows, not the nearest ones.
    return [
        {
            "post_id": row["post_id"],
            "score": 1.0,
            "campaign_id": row["campaign_id"],
            "overall_sentiment": row["overall_sentiment"],
            "post_summary": row["post_summary"],
            "embedding_is_stub": True,
            "matched_by": ["recency"],
        }
        for row in rows
    ]


# ---------------------------------------------------------------------------
# Tool: search_comments
#
# The structural gap this closes: one vector per post, built from the CAPTION,
# while the signal in this corpus lives in the comment threads — that is the
# entire reason the per-comment ensemble exists. "Where are people angry about
# fuel prices?" could only ever match caption text; the anger sat in rows that
# carried no vector.
#
# Hybrid for the same reason semantic_search is, more so: comments are shorter
# than captions, so a single misjudged token costs a bi-encoder more, and
# comment text is where the transliteration variance is worst.
# ---------------------------------------------------------------------------


_COMMENT_PROJECTION = """
    c.comment_id,
    c.post_id,
    c.text,
    c.author,
    c.likes,
    c.sentiment
"""


async def _comment_vector_arm(
    session: AsyncSession,
    qvec: str,
    where_parts: list[str],
    params: dict[str, Any],
    pool: int,
) -> list[dict]:
    """kNN over comment_embeddings, joined back to the comment text."""
    sql = text(
        f"""
        SELECT {_COMMENT_PROJECTION},
               COALESCE(ce.embedding_is_stub, FALSE) AS embedding_is_stub,
               1 - (ce.embedding <=> CAST(:qvec AS vector)) AS vector_score
        FROM comment_embeddings ce
        JOIN comments c
          ON c.post_id = ce.post_id AND c.comment_id = ce.comment_id
        WHERE {' AND '.join([*where_parts, 'ce.embedding IS NOT NULL'])}
        ORDER BY ce.embedding <=> CAST(:qvec AS vector)
        LIMIT :pool
        """
    )
    try:
        rows = (await session.execute(sql, {**params, "qvec": qvec, "pool": pool})).mappings().all()
    except Exception as exc:
        # The table exists only after the schema migration in deploy/init-db.sql
        # has been applied. An un-migrated deployment should lose the vector arm
        # and keep the lexical one, not lose comment search entirely.
        log.warning("comment_vector_arm_unavailable", error=str(exc))
        return []
    return [dict(r) for r in rows]


async def _comment_term_arm(
    session: AsyncSession,
    query: str,
    where_parts: list[str],
    params: dict[str, Any],
    pool: int,
) -> list[dict]:
    """Comments that literally CONTAIN one of the query's terms.

    The exact-match half of comment lexical search, and the reason a
    transliterated entity name the encoder has blurred is still findable.

    Ranked by how many distinct query terms the comment contains, tie-broken by
    likes. Term count rather than a similarity score: this arm's claim is
    "these words are present", so the comment containing three of them is the
    better answer, and mixing a trigram score in here is what the split below
    exists to undo.
    """
    terms = lexical_terms(query)
    if not terms:
        return []
    sql = text(
        f"""
        SELECT {_COMMENT_PROJECTION},
               FALSE AS embedding_is_stub,
               (SELECT count(*)
                  FROM unnest(CAST(:likes AS text[])) AS t
                 WHERE coalesce(c.text, '') ILIKE t) AS lexical_score
        FROM comments c
        WHERE {' AND '.join(where_parts)}
          AND coalesce(c.text, '') ILIKE ANY(CAST(:likes AS text[]))
        ORDER BY lexical_score DESC, c.likes DESC NULLS LAST
        LIMIT :pool
        """
    )
    try:
        rows = (
            await session.execute(
                sql, {**params, "likes": [f"%{t}%" for t in terms], "pool": pool}
            )
        ).mappings().all()
    except Exception as exc:
        log.warning("comment_term_arm_unavailable", error=str(exc))
        return []
    return [dict(r) for r in rows]


async def _comment_trgm_arm(
    session: AsyncSession,
    query: str,
    where_parts: list[str],
    params: dict[str, Any],
    pool: int,
) -> list[dict]:
    """Comments whose trigrams overlap the whole query strongly.

    The fuzzy half, for the spelling the term arm's ILIKE misses.

    Kept a SEPARATE fusion arm from :func:`_comment_term_arm` rather than merged
    into one score with ``GREATEST(similarity, <term bonus>)``. That merge is a
    max across two scales that have nothing to do with each other — precisely
    the comparison RRF exists to remove, and on the post side splitting the
    equivalent pair raised MRR@10 from 0.551 to 0.648 with no other change
    (see :func:`_trgm_arm`). There is no comment-level gold set to put a number
    on it here, so this is applied for consistency of mechanism, not on a
    measurement.

    The 0.3 floor is high on purpose. Trigram similarity is length-normalised,
    so on text this short a bare ``> 0.05`` is close to meaningless: a
    nine-character "Very good" cleared it for the query "angry criticism of the
    government" and, being rank 1 in its own arm, tied with the best genuine
    semantic hit under RRF.
    """
    terms = lexical_terms(query)
    if not terms:
        return []
    sql = text(
        f"""
        SELECT {_COMMENT_PROJECTION},
               FALSE AS embedding_is_stub,
               similarity(coalesce(c.text, ''), :probe) AS lexical_score
        FROM comments c
        WHERE {' AND '.join(where_parts)}
          AND similarity(coalesce(c.text, ''), :probe) > 0.3
        ORDER BY lexical_score DESC, c.likes DESC NULLS LAST
        LIMIT :pool
        """
    )
    try:
        rows = (
            await session.execute(
                sql, {**params, "probe": " ".join(terms), "pool": pool}
            )
        ).mappings().all()
    except Exception as exc:
        log.warning("comment_trgm_arm_unavailable", error=str(exc))
        return []
    return [dict(r) for r in rows]


@mcp.tool
async def search_comments(
    query: Annotated[str, Field(description="Natural-language description of the comments to find.")],
    campaign_id: Annotated[str | None, Field(description="Optional campaign to scope the search.")] = None,
    post_id: Annotated[str | None, Field(description="Optional single post to search within.")] = None,
    sentiment: Annotated[
        str | None, Field(description="Optional comment sentiment filter (positive/negative/neutral).")
    ] = None,
    limit: Annotated[int, Field(description="Max comments to return (max 50).", ge=1, le=50)] = 15,
    tenant_id: Annotated[str | None, Field(description="Optional tenant ID filter.")] = None,
) -> list[dict]:
    """Search COMMENT TEXT by meaning across the corpus — hybrid vector + lexical,
    fused by reciprocal rank fusion.

    Use this to find what people are actually saying (harassment patterns, a
    recurring complaint, reactions to a named entity) WITHOUT first having to
    guess which posts to open. Returns comment_id, post_id, the verbatim comment
    text, author, likes and stored sentiment.

    Quote only the `text` returned here, verbatim, and cite the `post_id`
    alongside the `comment_id`. `embedding_is_stub` TRUE means that comment's
    vector is a deterministic hash rather than a semantic embedding, so only the
    lexical arms' contribution to its ranking is meaningful.

    `matched_by` names the arms that found the comment: "vector" (meaning),
    "term" (contains your query's words) and "trgm" (fuzzy spelling match). A
    comment found by more than one is a stronger match than one found by a single
    arm. `score` is a rank-fusion score, NOT a similarity or a confidence — its
    absolute value carries no meaning, so rank the results by it but never report
    it as a relevance percentage."""
    limit = min(int(limit), 50)
    clean_cid = _clean_campaign_id(campaign_id)
    log.info(
        "tool_call", tool="search_comments", campaign_id=clean_cid,
        post_id=post_id, sentiment=sentiment, tenant_id=tenant_id,
    )

    # The vector arm filters on comment_embeddings (which carries campaign and
    # tenant); the lexical arm filters on comments (which carries neither), so
    # each gets its own scoping clause rather than sharing one that cannot apply.
    vec_where: list[str] = ["1=1"]
    lex_where: list[str] = ["1=1"]
    params: dict[str, Any] = {}
    if clean_cid:
        vec_where.append("ce.campaign_id = :campaign_id")
        lex_where.append(
            "EXISTS (SELECT 1 FROM posts p WHERE p.id = c.post_id "
            "AND p.campaign_id = :campaign_id)"
        )
        params["campaign_id"] = clean_cid
    if tenant_id:
        vec_where.append("ce.tenant_id = :tenant_id")
        lex_where.append(
            "EXISTS (SELECT 1 FROM posts p WHERE p.id = c.post_id "
            "AND p.tenant_id = :tenant_id)"
        )
        params["tenant_id"] = tenant_id
    # Raises on a placeholder rather than dropping the filter. Silently widening
    # a single-post question to the whole corpus is the one failure this tool must
    # not have: the caller asked for the comments on ONE post.
    clean_pid = _clean_post_id(post_id)
    if clean_pid:
        vec_where.append("ce.post_id = :post_id")
        lex_where.append("c.post_id = :post_id")
        params["post_id"] = clean_pid
    if sentiment and sentiment.lower() != "all":
        vec_where.append("c.sentiment = :sentiment")
        lex_where.append("c.sentiment = :sentiment")
        params["sentiment"] = sentiment.lower()

    pool = candidate_pool(limit)
    query_vec, query_is_stub = embed_text_with_provenance(query)
    qvec = to_pgvector_literal(query_vec)

    async with _AsyncSessionLocal() as session:
        vector_rows = await _comment_vector_arm(session, qvec, vec_where, params, pool)
        # Exact-term and trigram are SEPARATE arms, for the reason given on
        # _comment_trgm_arm: merging them into one score is the comparison RRF
        # exists to remove.
        term_rows, trgm_rows = (
            await asyncio.gather(
                _comment_term_arm(session, query, lex_where, params, pool),
                _comment_trgm_arm(session, query, lex_where, params, pool),
            )
            if _HYBRID
            else ([], [])
        )

    lexical_rows = [*term_rows, *trgm_rows]

    def _key(row: dict) -> str:
        return f"{row['post_id']}#{row['comment_id']}"

    by_id: dict[str, dict] = {}
    for row in [*lexical_rows, *vector_rows]:
        by_id.setdefault(_key(row), dict(row)).update(
            {k: v for k, v in row.items() if v is not None}
        )

    arms: dict[str, list[str]] = {"vector": [_key(r) for r in vector_rows]}
    if term_rows:
        arms["term"] = [_key(r) for r in term_rows]
    if trgm_rows:
        arms["trgm"] = [_key(r) for r in trgm_rows]
    weights = {
        "vector": 0.2 if query_is_stub else 1.0,
        "term": 1.0,
        "trgm": 1.0,
    }

    candidates: list[dict] = []
    for key, score, matched_by in fuse(arms, weights=weights)[: max(pool, limit)]:
        row = by_id.get(key)
        if row is None:  # pragma: no cover
            continue
        candidates.append(
            {
                "comment_id": row["comment_id"],
                "post_id": row["post_id"],
                "text": row.get("text"),
                "author": row.get("author"),
                "likes": row.get("likes"),
                "sentiment": row.get("sentiment"),
                "score": round(float(score), 6),
                "embedding_is_stub": bool(row.get("embedding_is_stub")) or query_is_stub,
                "matched_by": matched_by,
            }
        )

    results, was_reranked = rerank(query, candidates, text_key="text", limit=limit)
    results = results[:limit]

    if not results:
        log.info("search_comments_empty", query=query, hybrid=_HYBRID)
    log.info(
        "search_comments_completed",
        query=query, hits=len(results),
        vector_hits=len(vector_rows), lexical_hits=len(lexical_rows),
        reranked=was_reranked,
    )
    return results


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
    Returns cluster sizes, dominant sentiments, a representative post summary per
    cluster, and member post IDs.

    Clusters have NO name — `representative_summary` is the summary of one real
    post, not a label for the group. Describe a cluster by that summary; do not
    invent a theme name and present it as data.

    `posts_clustered` / `posts_available` / `scan_truncated` state which slice of
    the corpus was actually clustered. When `scan_truncated` is true these are
    the themes of the most recent `posts_clustered` posts, not corpus-wide
    themes, and must be reported as such. `is_stub` true means the vectors are
    deterministic hashes, so the groupings are arbitrary."""
    clean_cid = _clean_campaign_id(campaign_id)
    log.info("tool_call", tool="get_clusters", campaign_id=clean_cid, max_clusters=max_clusters, stub=_STUB_MODE)

    if _STUB_MODE:
        return await _stub_get_clusters(clean_cid, max_clusters, tenant_id=tenant_id)

    from defense.libs.clustering import cluster_embeddings, parse_pgvector

    where_parts = ["ar.embedding IS NOT NULL"]
    # Was a hardcoded 100 with `ORDER BY created_at DESC`, which made "corpus
    # themes" mean "themes of the 100 newest posts, in at most 8 buckets" while
    # reading as corpus-wide. The cap is now configurable AND disclosed on every
    # cluster row below — raising it alone would just move the number at which
    # the tool quietly lies (§3.6).
    params: dict[str, Any] = {"limit": _CLUSTER_SCAN_CAP}
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

    # How many posts COULD have been clustered, so the rows can say what the cap
    # dropped instead of presenting a sample as the corpus.
    count_sql = text(
        f"""
        SELECT count(*) AS n
        FROM analysis_results ar
        WHERE {' AND '.join(where_parts)}
        """
    )
    count_params = {k: v for k, v in params.items() if k != "limit"}

    async with _AsyncSessionLocal() as session:
        rows = (await session.execute(sql, params)).mappings().all()
        available = int((await session.execute(count_sql, count_params)).scalar() or 0)

    # No embeddings is a finding ("nothing has been vectorised yet"), not a
    # reason to hand the agent synthetic clusters it cannot tell apart.
    if not rows:
        log.info("get_clusters_no_embeddings", campaign_id=clean_cid)
        return []

    truncated = available > len(rows)
    scope = {
        "posts_clustered": len(rows),
        "posts_available": available,
        "scan_truncated": truncated,
    }
    if truncated:
        scope["scope_note"] = (
            f"These clusters describe the {len(rows)} most recent analysed posts, "
            f"not all {available} in scope. Do NOT describe them as corpus-wide "
            "themes; say which slice they cover."
        )

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
                **scope,
            }
        ]

    cr = cluster_embeddings(vectors, max_k=max_clusters, algo=algo)

    # Stable names, when somebody has written any. Matched by centroid rather
    # than by cluster_id, which k-means reassigns on every run.
    centroids = [list(c) for c in (getattr(cr, "centroids", None) or [])]
    async with _AsyncSessionLocal() as session:
        labels = await _match_persisted_labels(session, centroids, clean_cid, tenant_id)

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
            # Present ONLY when a label has been persisted for a centroid this
            # close to this cluster's. Absent means nobody has named this group
            # — which the narrative prompt is told to treat as "describe it by
            # representative_summary", not as licence to invent a name.
            **({"label": labels[ci]} if ci in labels else {}),
            "size": len(member_indices),
            "dominant_sentiment": dominant_sent,
            "representative_post_id": rep_meta["post_id"],
            "representative_summary": rep_meta["summary"] or "Summary unavailable",
            "member_post_ids": [meta[mi]["post_id"] for mi in member_indices[:25]],
            "is_stub": bool(stub_count),
            **scope,
        })

    log.info(
        "get_clusters_completed", clusters=len(results), total_posts=len(meta),
        posts_available=available, truncated=truncated,
    )
    return results


async def _match_persisted_labels(
    session: AsyncSession,
    centroids: list[list[float]],
    campaign_id: str | None,
    tenant_id: str | None,
    max_distance: float = 0.35,
) -> dict[int, str]:
    """Map cluster index → a previously stored human-readable label.

    Matching is by centroid cosine distance, NOT by cluster_id: k-means assigns
    ids arbitrarily on each run, so "cluster 3" in today's report and "cluster 3"
    in last week's are unrelated. That is precisely why themes could not be
    tracked over time, and why the narrative agent had nothing stable to name.

    ``max_distance`` is a floor on similarity, not a nicety: without it every
    cluster matches *something*, and a genuinely new narrative silently inherits
    the label of the nearest old one — which is worse than having no label,
    because it reads as continuity that was never observed.

    Centroids from a DIFFERENT embedding model are excluded outright, for the
    same reason `coverage_stats` warns about a mixed index: a cosine distance
    between two vector spaces is computed, comparable-looking and meaningless, so
    a distance floor cannot filter it. Rows predating the column (NULL) are
    allowed through — the alternative is discarding every label written before
    provenance was recorded.

    Returns ``{}` on any failure. A missing label costs a nicer column; a raised
    exception would cost the clustering.
    """
    if not centroids:
        return {}
    where = ["1=1", "(cl.embedding_model IS NULL OR cl.embedding_model = :model)"]
    params: dict[str, Any] = {"model": active_model_name()}
    if campaign_id:
        where.append("cl.campaign_id = :campaign_id")
        params["campaign_id"] = campaign_id
    if tenant_id:
        where.append("cl.tenant_id = :tenant_id")
        params["tenant_id"] = tenant_id

    out: dict[int, str] = {}
    sql = text(
        f"""
        SELECT cl.label, cl.centroid <=> CAST(:vec AS vector) AS dist
        FROM cluster_labels cl
        WHERE {' AND '.join(where)}
        ORDER BY cl.centroid <=> CAST(:vec AS vector)
        LIMIT 1
        """
    )
    try:
        for i, centroid in enumerate(centroids):
            row = (
                await session.execute(sql, {**params, "vec": to_pgvector_literal(centroid)})
            ).mappings().first()
            if row and row["dist"] is not None and float(row["dist"]) <= max_distance:
                out[i] = row["label"]
    except Exception as exc:
        log.warning("cluster_label_lookup_unavailable", error=str(exc))
        return {}
    return out


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
            "posts_clustered": 0,
            "posts_available": 0,
            "scan_truncated": False,
            "scope_note": (
                "SYNTHETIC — RETRIEVAL_MCP_STUB is on. These themes are canned "
                "strings, not clusters of anything in the corpus."
            ),
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


# ---------------------------------------------------------------------------
# The watchlist roster — what a `target_id` is allowed to be
#
# Both stance tools take an optional `target_id`, and it used to be applied as a
# plain equality filter against whatever string the model passed. An id nobody
# tracks then produced exactly the same empty list as a quiet corpus, so:
#
#     stance_over_time(target_id="primary_political_figures")  ->  []
#
# was reported to the operator as "no stance data exists for the primary
# political figures" — while the one entity actually on the watchlist had stance
# rows in seven posts the whole time. The model cannot guess an id (the roster
# is an operator-owned file it never sees) and it has no tool to list one, so it
# invents one, and inventing one is indistinguishable from finding nothing.
#
# The roster is the authority, so an unknown id is now REJECTED the way
# `_clean_campaign_id` rejects a placeholder: the runner turns the raised error
# into a tool result the model reads, and the message names every valid id so
# the correction costs one turn out of the budget rather than the whole run.
#
# A target that IS on the watchlist and has no rows still returns empty. That is
# a real answer ("nobody mentioned them" — or an alias-coverage bug, which is
# what `libs/stance_targets.unmatched_targets` is for), and it is the answer the
# tool docstrings promise.
# ---------------------------------------------------------------------------

#: Loaded watchlist, or ``False`` once loading has failed (so it is tried once).
_WATCHLIST_CACHE: Any = None


def _watchlist() -> Any:
    """The operator watchlist, cached. ``None`` when it could not be read."""
    global _WATCHLIST_CACHE
    if _WATCHLIST_CACHE is None:
        path = config.stance_targets_file
        try:
            from defense.libs.stance_targets import load_targets  # noqa: PLC0415

            _WATCHLIST_CACHE = load_targets(path)
            log.info(
                "watchlist_loaded",
                path=path,
                targets=[t.id for t in _WATCHLIST_CACHE.targets],
            )
        except Exception as exc:
            # Do not take the stance tools down with a bad config file: fall
            # back to the unvalidated filter and say so in the log.
            log.error("watchlist_load_failed", path=path, error=str(exc))
            _WATCHLIST_CACHE = False
    return _WATCHLIST_CACHE or None


def _clean_target_ids(target_id: Any) -> set[str] | None:
    """Resolve an LLM-supplied target to watchlist ids, or raise if unknown.

    Accepts an id, a display name, or any alias — including the Bangla and
    Banglish spellings, since those are what the corpus and therefore the
    retrieved comments actually contain. Several-in-one-string
    ("pm_hasina, imran_khan") is split on commas rather than rejected: it is the
    shape a model reaches for when the question names a group of people, and
    every part still has to resolve.

    ``None`` means "no filter" and is the only value that widens the query.
    """
    if target_id is None:
        return None
    parts = [p.strip() for p in str(target_id).split(",")]
    wanted = [p for p in parts if p and p.lower() not in _UNSPECIFIED]
    if not wanted:
        return None
    # "all" is a real intent here and it is the *unfiltered* query, not an
    # entity — rejecting it as an unknown target would be pedantry.
    if any(w.lower() in _ALL_CAMPAIGNS for w in wanted):
        return None

    wl = _watchlist()
    if wl is None or not wl.targets:
        # No roster to check against — behave as before rather than reject
        # everything, but leave a trail explaining why nothing was validated.
        log.warning("target_id_unvalidated", target_id=target_id)
        return set(wanted)

    lookup: dict[str, str] = {}
    for t in wl.targets:
        for key in (t.id, t.display, *t.aliases):
            k = str(key or "").strip().lower()
            if k:
                lookup.setdefault(k, t.id)

    resolved: set[str] = set()
    unknown: list[str] = []
    for w in wanted:
        tid = lookup.get(w.lower())
        if tid:
            resolved.add(tid)
        else:
            unknown.append(w)

    if unknown:
        roster = ", ".join(f"{t.id} ({t.display})" for t in wl.targets)
        raise ValueError(
            f"target_id {', '.join(repr(u) for u in unknown)} is not on the "
            f"watchlist, so no stance was ever scored for it. The watchlist "
            f"tracks exactly: {roster}. Pass one of those ids, or omit target_id "
            f"to get every tracked target. Do not report this as an absence of "
            f"data in the corpus."
        )
    return resolved


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
    target_ids: set[str] | None,
) -> None:
    """Merge one post's target_stances dict into the corpus-level rollup."""
    if not isinstance(blob, dict):
        return
    for tid, entry in blob.items():
        if target_ids and tid not in target_ids:
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
    target_id: Annotated[str | None, Field(description="Optional watchlist target: an id, display name or alias from the operator watchlist. An entity that is not on the watchlist is rejected (the error lists the valid ids) — it was never scored, so there is nothing to filter to. Omit to report every tracked target.")] = None,
    tenant_id: Annotated[str | None, Field(description="Optional tenant ID filter.")] = None,
) -> list[dict]:
    """Stance distribution per watchlist target entity (supportive / opposing / neutral
    counts, shares, net polarity and scorer provenance), aggregated over the analysed
    posts of a campaign. Targets nobody mentioned are absent, not zero-filled — an
    empty result means no watchlist entity was mentioned in the selected posts."""
    clean_cid = _clean_campaign_id(campaign_id)
    target_ids = _clean_target_ids(target_id)
    log.info(
        "tool_call", tool="stance_by_target", campaign_id=clean_cid,
        target_id=target_id, resolved_target_ids=sorted(target_ids or ()),
        scope="all_campaigns" if clean_cid is None else "campaign",
    )

    rows = await _latest_target_stances(clean_cid, tenant_id, from_date, to_date)

    rollup: dict[str, dict] = {}
    for row in rows:
        _fold_target_stances(rollup, row.get("target_stances"), target_ids)

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
    target_id: Annotated[str | None, Field(description="Optional watchlist target: an id, display name or alias from the operator watchlist. An entity that is not on the watchlist is rejected (the error lists the valid ids) — it was never scored, so there is nothing to filter to. Omit to sum all tracked targets.")] = None,
    tenant_id: Annotated[str | None, Field(description="Optional tenant ID filter.")] = None,
) -> list[dict]:
    """Time series of per-target stance (supportive / opposing / neutral and net polarity),
    bucketed by the analysis timestamp. One row per (period, target)."""
    clean_cid = _clean_campaign_id(campaign_id)
    target_ids = _clean_target_ids(target_id)
    bucket = granularity if granularity in ("hour", "day", "week") else "day"
    log.info(
        "tool_call", tool="stance_over_time", campaign_id=clean_cid,
        target_id=target_id, resolved_target_ids=sorted(target_ids or ()),
        granularity=bucket,
    )

    rows = await _latest_target_stances(
        clean_cid, tenant_id, from_date, to_date, bucket=bucket
    )

    periods: dict[str, dict[str, dict]] = {}
    for row in rows:
        period = row.get("period")
        key = period.isoformat() if hasattr(period, "isoformat") else str(period)
        _fold_target_stances(periods.setdefault(key, {}), row.get("target_stances"), target_ids)

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
    """Audit analysis coverage AND vector provenance: how many collected posts have
    been analysed, how many of their reported comments were stored and labelled, how
    many posts carry a coverage anomaly, how many embeddings are deterministic stubs
    rather than semantic vectors, which embedding model(s) wrote them, and how much of
    the comment corpus is embedded at all.

    Stub vectors make semantic_search rankings arbitrary; more than one entry in
    `embedding_models` means the index mixes two incomparable vector spaces; a
    `comment_vector_coverage` of 0 means search_comments can reach nothing; and
    `posts_over_comment_cap` / `comments_dropped_by_cap` say how much of the
    comment corpus is unreachable because a thread ran past the per-post
    embedding cap rather than because the encoder failed."""
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

    # Vector provenance, separately: which model produced the post vectors, and
    # how much of the comment corpus is retrievable by meaning at all. Comment
    # vectors are the newer half of the index (§3.2) and a deployment that has
    # not run the migration has none — a fact an audit must state rather than
    # leave to be inferred from an empty search_comments result.
    vec_sql = text(
        f"""
        WITH latest AS (
            SELECT DISTINCT ON (ar.post_id)
                ar.post_id, ar.embedding_model, ar.embedding_dim
            FROM analysis_results ar
            WHERE {' AND '.join(ar_where)}
            ORDER BY ar.post_id, ar.created_at DESC
        )
        SELECT
            COALESCE(embedding_model, 'unknown') AS model,
            COALESCE(embedding_dim, 0)           AS dim,
            count(*)                             AS n
        FROM latest
        GROUP BY 1, 2
        ORDER BY n DESC
        """
    )
    comment_vec_sql = text(
        """
        SELECT
            count(*)                                                   AS vectors,
            count(*) FILTER (WHERE COALESCE(embedding_is_stub, FALSE)) AS stub_vectors,
            count(*) FILTER (WHERE represented_by IS NOT NULL)         AS propagated
        FROM comment_embeddings ce
        -- CAST(...) rather than `:param::varchar`: SQLAlchemy's bind-parameter
        -- parser reads the second colon of a postfix cast as the start of
        -- another parameter, and the statement reaches Postgres malformed.
        WHERE (CAST(:campaign_id AS varchar) IS NULL OR ce.campaign_id = :campaign_id)
          AND (CAST(:tenant_id AS varchar) IS NULL OR ce.tenant_id = :tenant_id)
        """
    )

    # Posts whose thread is longer than the per-post embedding cap. Those
    # comments are stored and labelled but have NO vector, so search_comments
    # cannot reach them — the same class of silent truncation as the cluster scan
    # cap (§3.6), and one that otherwise shows up only as an unexplained dip in
    # `comment_vector_coverage`. Derived from the cap and the comments table
    # rather than recorded at write time, so it is correct for a corpus ingested
    # before the cap was set, or after it was changed.
    cap = config.comment_embedding_max_per_post
    cap_sql = text(
        """
        SELECT count(*) AS posts, COALESCE(SUM(n - :cap), 0) AS dropped
        FROM (
            SELECT c.post_id, count(*) AS n
            FROM comments c
            JOIN posts p ON p.id = c.post_id
            WHERE (CAST(:campaign_id AS varchar) IS NULL OR p.campaign_id = :campaign_id)
              AND (CAST(:tenant_id AS varchar) IS NULL OR p.tenant_id = :tenant_id)
            GROUP BY c.post_id
            HAVING count(*) > :cap
        ) over_cap
        """
    )

    async with _AsyncSessionLocal() as session:
        row = (await session.execute(sql, params)).mappings().first()
        try:
            model_rows = (await session.execute(vec_sql, params)).mappings().all()
        except Exception as exc:
            log.warning("embedding_model_stats_unavailable", error=str(exc))
            model_rows = []
        capped = None
        if cap:
            try:
                capped = (
                    await session.execute(
                        cap_sql,
                        {
                            "cap": cap,
                            "campaign_id": clean_cid,
                            "tenant_id": tenant_id,
                        },
                    )
                ).mappings().first()
            except Exception as exc:
                log.warning("comment_cap_stats_unavailable", error=str(exc))
        try:
            cvec = (
                await session.execute(
                    comment_vec_sql,
                    {"campaign_id": clean_cid, "tenant_id": tenant_id},
                )
            ).mappings().first()
        except Exception as exc:
            # comment_embeddings only exists after the schema migration.
            log.warning("comment_vector_stats_unavailable", error=str(exc))
            cvec = None

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

    # `.get` rather than `[...]`: this is a second query against the same
    # session, and a caller that substitutes a session double (or a deployment
    # whose columns predate the migration) must lose the provenance breakdown,
    # not the coverage audit it is attached to.
    embedding_models = [
        {"model": r.get("model"), "dim": _int(r.get("dim")), "posts": _int(r.get("n"))}
        for r in model_rows
        if r.get("model") is not None
    ]
    # Two models in one column means two vector spaces in one HNSW index:
    # distances across them are computed, comparable, and meaningless.
    if len(embedding_models) > 1:
        notes.append(
            "Post vectors were written by more than one embedding model ("
            + ", ".join(f"{m['model']} × {m['posts']}" for m in embedding_models)
            + "). Distances between rows from different models are not comparable; "
            "re-embed the corpus before trusting semantic_search rankings."
        )

    comment_vectors = _int(cvec.get("vectors")) if cvec else 0
    comment_stub_vectors = _int(cvec.get("stub_vectors")) if cvec else 0
    comment_propagated = _int(cvec.get("propagated")) if cvec else 0

    capped_posts = _int(capped.get("posts")) if capped else 0
    capped_dropped = _int(capped.get("dropped")) if capped else 0
    if capped_posts:
        notes.append(
            f"{capped_posts} post(s) have threads longer than the per-post embedding "
            f"cap of {cap}, so roughly {capped_dropped} comment(s) are stored and "
            "labelled but carry NO vector and cannot be reached by search_comments. "
            "This lowers comment_vector_coverage for a reason unrelated to the "
            "encoder — raise COMMENT_EMBEDDING_MAX_PER_POST and re-run the backfill "
            "to embed them."
        )

    if analyzed_comments and not comment_vectors:
        notes.append(
            f"{analyzed_comments} comment(s) are labelled but NONE are embedded, so "
            "search_comments cannot reach them. Comment vectors are written at "
            "analysis time — re-run the pipeline, or apply deploy/init-db.sql if "
            "the comment_embeddings table is missing."
        )

    return {
        "embedding_models": embedding_models,
        "comment_vectors": comment_vectors,
        "comment_stub_vectors": comment_stub_vectors,
        # Vectors shared with a near-duplicate rather than computed. Not a
        # defect — it is the cost lever — but it means N vectors do not imply N
        # distinct encoder calls.
        "comment_vectors_propagated": comment_propagated,
        "comment_vector_coverage": (
            round(comment_vectors / analyzed_comments, 4) if analyzed_comments else 0.0
        ),
        # Disclosed rather than left to be inferred from the coverage figure.
        "comment_embedding_cap": cap,
        "posts_over_comment_cap": capped_posts,
        "comments_dropped_by_cap": capped_dropped,
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

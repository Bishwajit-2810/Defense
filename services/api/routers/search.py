"""Search endpoints — keyword and semantic search over analysis results."""

from __future__ import annotations

import os
import sys
from typing import Any

import structlog
from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from deps import get_current_user, get_db
from models import SearchResponse, SearchResult

# Repo root on path for `libs.*` (no-op when PYTHONPATH already provides it).
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from libs.embeddings import embed_text_with_provenance, to_pgvector_literal  # noqa: E402

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/v1/search", tags=["search"])


# ---------------------------------------------------------------------------
# GET /v1/search
# ---------------------------------------------------------------------------


@router.get(
    "",
    response_model=SearchResponse,
    summary="Keyword or semantic search over analysis results",
)
async def search(
    q: str = Query(..., min_length=1, description="Search query string"),
    semantic: bool = Query(False, description="Use semantic (vector) search"),
    campaign_id: str | None = Query(None, description="Filter by campaign ID"),
    limit: int = Query(20, ge=1, le=200, description="Maximum results to return"),
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> SearchResponse:
    """Search analysis results.

    - **Keyword search** (default): performs a case-insensitive JSONB text
      scan across ``analysis_results.result``.  Matches on post summary,
      topics, keywords, and comment themes.
    - **Semantic search** (``?semantic=true``): embeds the query and runs a
      pgvector cosine search over ``analysis_results.embedding``. Semantic
      *quality* requires real embeddings (``MODEL_STUB_MODE=false`` +
      ``uv sync --extra ml``); in stub mode vectors are deterministic hashes,
      so results are stable but not meaning-based.
    """
    if semantic:
        results = await _semantic_search(db, q=q, campaign_id=campaign_id, limit=limit)
        log.info("semantic_search_completed", q=q, total=len(results))
        return SearchResponse(query=q, semantic=True, total=len(results), results=results)

    # --- Keyword search via Postgres JSONB ---
    results = await _keyword_search(db, q=q, campaign_id=campaign_id, limit=limit)

    log.info(
        "keyword_search_completed",
        q=q,
        campaign_id=campaign_id,
        total=len(results),
    )
    return SearchResponse(query=q, semantic=False, total=len(results), results=results)


async def _semantic_search(
    db: AsyncSession,
    q: str,
    campaign_id: str | None,
    limit: int,
) -> list[SearchResult]:
    """pgvector cosine-similarity search over analysis_results.embedding.

    ``<=>`` is pgvector's cosine-distance operator; score = 1 - distance.
    """
    query_vec, query_is_stub = embed_text_with_provenance(q)
    qvec = to_pgvector_literal(query_vec)

    params: dict[str, Any] = {"qvec": qvec, "limit": limit}
    where_parts = ["ar.embedding IS NOT NULL"]
    if campaign_id:
        where_parts.append("ar.campaign_id = :campaign_id")
        params["campaign_id"] = campaign_id

    sql = text(
        f"""
        SELECT
            ar.post_id,
            ar.campaign_id,
            ar.result->>'post_summary' AS snippet,
            ar.result,
            COALESCE(ar.embedding_is_stub, FALSE) AS embedding_is_stub,
            1 - (ar.embedding <=> CAST(:qvec AS vector)) AS score
        FROM analysis_results ar
        WHERE {' AND '.join(where_parts)}
        ORDER BY ar.embedding <=> CAST(:qvec AS vector)
        LIMIT :limit
        """
    )

    rows = (await db.execute(sql, params)).mappings().all()

    # §5.9: a stub vector is "deterministic ... not semantic", so kNN over stub
    # rows returns arbitrary neighbours. The scores look exactly as plausible as
    # real ones, which is why this has to be said rather than left inferable.
    stub_rows = sum(1 for row in rows if row.get("embedding_is_stub"))
    if query_is_stub or stub_rows:
        log.warning(
            "semantic_search_over_stub_vectors",
            query_is_stub=query_is_stub,
            stub_rows=stub_rows,
            total_rows=len(rows),
            detail="results are not semantically meaningful",
        )

    return [
        SearchResult(
            post_id=row["post_id"],
            campaign_id=row["campaign_id"],
            score=round(float(row["score"]), 4) if row["score"] is not None else 0.0,
            snippet=(row["snippet"] or "")[:200] or None,
            result=row["result"],
            # True when this row's vector — or the query's — is the hash stub.
            # A caller that renders a "semantic match" badge must not do so here.
            embedding_is_stub=bool(row.get("embedding_is_stub")) or query_is_stub,
        )
        for row in rows
    ]


async def _keyword_search(
    db: AsyncSession,
    q: str,
    campaign_id: str | None,
    limit: int,
) -> list[SearchResult]:
    """Run a keyword search against analysis_results using JSONB operators.

    Searches the following JSONB fields (case-insensitive):
      - result->>'post_summary'
      - result->'keywords' (array contains)
      - result->'topics'   (array contains)
      - result->'comment_analysis'->'themes' (array contains)
    """
    pattern = f"%{q.lower()}%"
    params: dict[str, Any] = {"pattern": pattern, "limit": limit}

    # Base WHERE clause — text fields
    where_clauses = [
        "LOWER(ar.result->>'post_summary') LIKE :pattern",
        "EXISTS (SELECT 1 FROM jsonb_array_elements_text(ar.result->'keywords') kw WHERE LOWER(kw) LIKE :pattern)",
        "EXISTS (SELECT 1 FROM jsonb_array_elements_text(ar.result->'topics') t WHERE LOWER(t) LIKE :pattern)",
        "EXISTS (SELECT 1 FROM jsonb_array_elements_text(ar.result->'comment_analysis'->'themes') th WHERE LOWER(th) LIKE :pattern)",
    ]

    if campaign_id:
        params["campaign_id"] = campaign_id
        campaign_filter = "AND ar.campaign_id = :campaign_id"
    else:
        campaign_filter = ""

    sql = text(
        f"""
        SELECT
            ar.post_id,
            ar.campaign_id,
            ar.result->>'post_summary' AS snippet,
            ar.result
        FROM analysis_results ar
        WHERE ({" OR ".join(where_clauses)})
        {campaign_filter}
        ORDER BY ar.created_at DESC
        LIMIT :limit
        """
    )

    rows = (await db.execute(sql, params)).mappings().all()

    out: list[SearchResult] = []
    for row in rows:
        # Simple relevance score: count of pattern occurrences in summary
        snippet: str = row["snippet"] or ""
        score = snippet.lower().count(q.lower()) / max(len(snippet), 1) + 0.5

        out.append(
            SearchResult(
                post_id=row["post_id"],
                campaign_id=row["campaign_id"],
                score=round(min(score, 1.0), 4),
                snippet=snippet[:200] if snippet else None,
                result=row["result"],
            )
        )

    return out

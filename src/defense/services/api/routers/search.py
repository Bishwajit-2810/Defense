"""Search endpoints — keyword and semantic search over analysis results."""

from __future__ import annotations

import os
import sys
from typing import Any

import structlog
from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from defense.services.api.deps import get_current_user, get_db
from defense.services.api.models import SearchResponse, SearchResult

# Repo root on path for `libs.*` (no-op when PYTHONPATH already provides it).

from defense.libs.embeddings import embed_text_with_provenance, to_pgvector_literal  # noqa: E402
from defense.libs.retrieval import candidate_pool, fuse  # noqa: E402

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/v1/search", tags=["search"])


# An identifier is looked up, not searched for.
#
# Every post carries three ids — the upstream CUID (`post_id`), the platform's
# own numeric id (`platform_post_id`) and its URL — and pasting one of them was
# the one query the search could not answer. Keyword search read only
# summary/caption/keywords/topics/themes, so a real post id returned **0
# results**; semantic search embedded the id and returned **20 cosine
# neighbours**, none of them the post asked for, which is worse because it looks
# like a successful search.
#
# So an exact identifier match short-circuits ahead of the requested mode. It is
# not a ranking tweak: "show me this post" and "show me posts that read like
# these words" are different questions, and only one of them has a right answer.
# Shape test. It may only decide two things: whether to *also* try a partial-id
# lookup, and whether to warn that an id matched nothing. It can never suppress
# a search — a false positive here must cost an extra query and a notice, never
# results, because "one long word" is a perfectly ordinary text query.
#
# Requiring a digit is what keeps it off natural language: `bangladesh`,
# `মুসলিমদের` and `disinformation` are words, while a CUID
# (`cmp58e24s04pgwglq7g9u9jz0`) and a platform id (`122162468462710684`) both
# carry digits. A URL is taken as an id on its prefix alone.
_ID_MIN_LEN = 8


def _looks_like_identifier(q: str) -> bool:
    q = q.strip()
    if q.startswith(("http://", "https://")):
        return True
    if len(q) < _ID_MIN_LEN or " " in q:
        return False
    body = q.replace("-", "").replace("_", "")
    return body.isalnum() and any(c.isdigit() for c in body)


async def _partial_id_search(
    db: AsyncSession,
    q: str,
    limit: int,
    tenant_id: str,
) -> list[SearchResult]:
    """Identifier *prefix/substring* match — the clipboard case.

    The dashboard's post table renders `post_id.slice(0, 8)`, so what an operator
    actually copies is a prefix. Exact match cannot answer that, and in semantic
    mode nothing else would either.
    """
    needle = q.strip().lower()
    if not needle:
        return []
    escaped = needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    rows = (
        await db.execute(
            text(
                r"""
                SELECT ar.post_id, ar.campaign_id,
                       ar.result->>'post_summary' AS snippet,
                       ar.result
                FROM analysis_results ar
                WHERE ar.tenant_id = :tid
                  AND (
                        LOWER(ar.post_id) LIKE :pattern ESCAPE ''
                     OR LOWER(COALESCE(ar.result->>'platform_post_id', '')) LIKE :pattern ESCAPE ''
                     OR LOWER(COALESCE(ar.result->>'url', '')) LIKE :pattern ESCAPE ''
                     OR LOWER(COALESCE(ar.campaign_id, '')) LIKE :pattern ESCAPE ''
                  )
                ORDER BY ar.created_at DESC
                LIMIT :limit
                """
            ),
            {"pattern": f"%{escaped}%", "tid": tenant_id, "limit": limit},
        )
    ).mappings().all()

    return [
        SearchResult(
            post_id=r["post_id"],
            campaign_id=r["campaign_id"],
            score=1.0,
            snippet=(r["snippet"] or "")[:200] or None,
            result=r["result"],
            embedding_is_stub=bool((r["result"] or {}).get("embedding_is_stub")),
        )
        for r in rows
    ]


async def _exact_id_search(
    db: AsyncSession,
    q: str,
    limit: int,
    tenant_id: str,
) -> list[SearchResult]:
    """Return the post(s) whose identifier IS *q*, or an empty list.

    Matched case-insensitively on the CUID, the platform id and the URL.
    `campaign_id` is matched too, so pasting a campaign id lists its posts
    instead of returning nothing.
    """
    needle = q.strip()
    if not needle:
        return []
    rows = (
        await db.execute(
            text(
                """
                SELECT ar.post_id, ar.campaign_id,
                       ar.result->>'post_summary' AS snippet,
                       ar.result
                FROM analysis_results ar
                WHERE ar.tenant_id = :tid
                  AND (
                        LOWER(ar.post_id) = LOWER(:needle)
                     OR LOWER(COALESCE(ar.result->>'platform_post_id', '')) = LOWER(:needle)
                     OR LOWER(COALESCE(ar.result->>'url', '')) = LOWER(:needle)
                     OR LOWER(COALESCE(ar.campaign_id, '')) = LOWER(:needle)
                  )
                ORDER BY ar.created_at DESC
                LIMIT :limit
                """
            ),
            {"needle": needle, "tid": tenant_id, "limit": limit},
        )
    ).mappings().all()

    return [
        SearchResult(
            post_id=r["post_id"],
            campaign_id=r["campaign_id"],
            score=1.0,  # An exact id match is not a similarity.
            snippet=(r["snippet"] or "")[:200] or None,
            result=r["result"],
            embedding_is_stub=bool((r["result"] or {}).get("embedding_is_stub")),
        )
        for r in rows
    ]


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
    mode: str | None = Query(
        None,
        description="keyword | semantic | hybrid. Overrides `semantic` when given.",
    ),
    campaign_id: str | None = Query(None, description="Filter by campaign ID"),
    limit: int = Query(20, ge=1, le=200, description="Maximum results to return"),
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> SearchResponse:
    """Search analysis results.

    - **Keyword search** (default): a case-insensitive JSONB text scan across
      ``analysis_results.result``. Matches post summary, caption, topics,
      keywords and comment themes.
    - **Semantic search** (``?semantic=true`` / ``?mode=semantic``): embeds the
      query and runs a pgvector cosine search over ``analysis_results.embedding``.
      Semantic *quality* requires real embeddings (``MODEL_STUB_MODE=false`` +
      ``uv sync --extra ml``); in stub mode vectors are deterministic hashes, so
      results are stable but not meaning-based.
    - **Hybrid** (``?mode=hybrid``): runs both and fuses them with reciprocal
      rank fusion. On a code-mixed Bangla/English corpus neither arm dominates —
      the encoder blurs the exact entity names and transliterations that the
      keyword scan nails — so this is the mode to prefer when you have no reason
      to force one.

    ``mode`` is not the default because ``semantic`` is the existing contract and
    silently changing what a caller's query returns is worse than making them ask.
    """
    tenant_id = current_user.get("tenant_id", "default")

    requested = (mode or ("semantic" if semantic else "keyword")).strip().lower()

    # Identifier first, in EVERY mode. Runs unconditionally — no shape test gates
    # it — so a query that happens to be an id can never be answered by cosine
    # distance instead.
    exact = await _exact_id_search(db, q=q, limit=limit, tenant_id=tenant_id)
    if exact:
        log.info("exact_id_search_hit", q=q, total=len(exact))
        return SearchResponse(
            query=q,
            semantic=False,
            total=len(exact),
            results=exact,
            match_type="exact_id",
        )

    # Looks like an id but no exact match: try it as a prefix before falling
    # back. This is the case the post table creates by rendering 8 characters of
    # a 25-character id.
    id_shaped = _looks_like_identifier(q)
    if id_shaped:
        partial = await _partial_id_search(db, q=q, limit=limit, tenant_id=tenant_id)
        if partial:
            log.info("partial_id_search_hit", q=q, total=len(partial))
            return SearchResponse(
                query=q,
                semantic=False,
                total=len(partial),
                results=partial,
                match_type="id_prefix",
            )

    # No identifier matched, and the query is id-shaped. Downgrade to the keyword
    # arm whatever was asked for: an identifier has no meaning to embed, so the
    # semantic arm answers it with its nearest neighbours in the vector space —
    # 20 unrelated posts that look exactly like a successful search. The keyword
    # arm is kept (rather than returning nothing) because an id-shaped string can
    # legitimately appear in a caption, and because the shape test is a guess.
    if id_shaped and requested in ("semantic", "hybrid"):
        results = await _keyword_search(
            db, q=q, campaign_id=campaign_id, limit=limit, tenant_id=tenant_id
        )
        log.info("id_lookup_missed_downgraded_to_keyword", q=q, requested=requested, total=len(results))
        return SearchResponse(
            query=q, semantic=False, total=len(results), results=results,
            match_type="keyword", id_lookup_missed=True,
        )

    # Otherwise run exactly the mode that was asked for. `id_lookup_missed`
    # travels with the response so a UI can lead with "no post has that id"
    # rather than presenting text matches as if they were the post.
    if requested == "hybrid":
        results = await _hybrid_search(
            db, q=q, campaign_id=campaign_id, limit=limit, tenant_id=tenant_id
        )
        log.info("hybrid_search_completed", q=q, total=len(results))
        return SearchResponse(
            query=q, semantic=True, total=len(results), results=results,
            match_type="hybrid", id_lookup_missed=id_shaped,
        )

    if requested == "semantic":
        results = await _semantic_search(db, q=q, campaign_id=campaign_id, limit=limit, tenant_id=tenant_id)
        log.info("semantic_search_completed", q=q, total=len(results))
        return SearchResponse(
            query=q, semantic=True, total=len(results), results=results,
            match_type="semantic", id_lookup_missed=id_shaped,
        )

    # --- Keyword search via Postgres JSONB ---
    results = await _keyword_search(db, q=q, campaign_id=campaign_id, limit=limit, tenant_id=tenant_id)

    log.info(
        "keyword_search_completed",
        q=q,
        campaign_id=campaign_id,
        total=len(results),
    )
    return SearchResponse(
        query=q, semantic=False, total=len(results), results=results,
        match_type="keyword", id_lookup_missed=id_shaped,
    )


async def _semantic_search(
    db: AsyncSession,
    q: str,
    campaign_id: str | None,
    limit: int,
    tenant_id: str = "default",
) -> list[SearchResult]:
    """pgvector cosine-similarity search over analysis_results.embedding.

    ``<=>`` is pgvector's cosine-distance operator; score = 1 - distance.
    """
    query_vec, query_is_stub = embed_text_with_provenance(q)
    qvec = to_pgvector_literal(query_vec)

    params: dict[str, Any] = {"qvec": qvec, "limit": limit, "tid": tenant_id}
    where_parts = ["ar.embedding IS NOT NULL", "ar.tenant_id = :tid"]
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


async def _hybrid_search(
    db: AsyncSession,
    q: str,
    campaign_id: str | None,
    limit: int,
    tenant_id: str = "default",
) -> list[SearchResult]:
    """Fuse the vector and keyword arms with reciprocal rank fusion.

    Both arms over-fetch, then RRF combines their RANKS — never their scores,
    which live on incomparable scales (cosine similarity vs. a substring-density
    heuristic) and cannot be normalised into each other.

    When the vector arm is running on hash stubs its rank order carries no
    information, so it is down-weighted rather than dropped: it still supplies
    candidates the keyword scan missed, but it can no longer outvote the arm that
    is actually measuring something.
    """
    pool = candidate_pool(limit)
    vector_hits = await _semantic_search(
        db, q=q, campaign_id=campaign_id, limit=pool, tenant_id=tenant_id
    )
    keyword_hits = await _keyword_search(
        db, q=q, campaign_id=campaign_id, limit=pool, tenant_id=tenant_id
    )

    by_id: dict[str, SearchResult] = {}
    for hit in [*keyword_hits, *vector_hits]:
        by_id.setdefault(hit.post_id, hit)

    vectors_are_stubs = any(h.embedding_is_stub for h in vector_hits)
    fused = fuse(
        {
            "vector": [h.post_id for h in vector_hits],
            "keyword": [h.post_id for h in keyword_hits],
        },
        weights={"vector": 0.2 if vectors_are_stubs else 1.0, "keyword": 1.0},
    )

    out: list[SearchResult] = []
    for post_id, score, _matched_by in fused[:limit]:
        hit = by_id.get(post_id)
        if hit is None:  # pragma: no cover — every fused id came from an arm
            continue
        out.append(hit.model_copy(update={"score": round(float(score), 6)}))

    log.info(
        "hybrid_search_fused",
        q=q, vector_hits=len(vector_hits), keyword_hits=len(keyword_hits),
        fused=len(out), vector_arm_is_stub=vectors_are_stubs,
    )
    return out


async def _keyword_search(
    db: AsyncSession,
    q: str,
    campaign_id: str | None,
    limit: int,
    tenant_id: str = "default",
) -> list[SearchResult]:
    """Run a keyword search against analysis_results using JSONB operators.

    Searches the following (case-insensitive substring):
      - result->>'post_summary'
      - result->>'post_text'   (the post's own caption)
      - result->'keywords' (array contains)
      - result->'topics'   (array contains)
      - result->'comment_analysis'->'themes' (array contains)
      - the identifiers: post_id, platform_post_id, url, campaign_id

    The identifiers are here for the **partial** case; an exact one is answered
    before this function is reached. It matters because the dashboard's post
    table renders only the first 8 characters of a post id, so the string an
    operator has in their clipboard is usually a prefix, not the whole thing.

    LIKE metacharacters in ``q`` are escaped, so a query of ``%`` searches for a
    literal percent sign rather than matching every row.
    """
    # Escape the LIKE metacharacters in the user's query before wrapping it in
    # wildcards. The query is parameterised (so this was never SQL injection),
    # but an unescaped `%` or `_` is still a wildcard *inside* the pattern: a
    # search for "%" matched every row, and "a_c" matched "abc". The ESCAPE
    # clause in each predicate below makes the backslash the escape character.
    escaped = q.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    pattern = f"%{escaped}%"
    params: dict[str, Any] = {"pattern": pattern, "limit": limit, "tid": tenant_id}

    # Base WHERE clause — text fields, then the identifiers
    where_clauses = [
        r"LOWER(ar.post_id) LIKE :pattern ESCAPE '\'",
        r"LOWER(COALESCE(ar.result->>'platform_post_id', '')) LIKE :pattern ESCAPE '\'",
        r"LOWER(COALESCE(ar.result->>'url', '')) LIKE :pattern ESCAPE '\'",
        r"LOWER(COALESCE(ar.campaign_id, '')) LIKE :pattern ESCAPE '\'",
        r"LOWER(ar.result->>'post_summary') LIKE :pattern ESCAPE '\'",
        r"LOWER(ar.result->>'post_text') LIKE :pattern ESCAPE '\'",
        r"EXISTS (SELECT 1 FROM jsonb_array_elements_text(ar.result->'keywords') kw WHERE LOWER(kw) LIKE :pattern ESCAPE '\')",
        r"EXISTS (SELECT 1 FROM jsonb_array_elements_text(ar.result->'topics') t WHERE LOWER(t) LIKE :pattern ESCAPE '\')",
        r"EXISTS (SELECT 1 FROM jsonb_array_elements_text(ar.result->'comment_analysis'->'themes') th WHERE LOWER(th) LIKE :pattern ESCAPE '\')",
    ]

    campaign_filter = "AND ar.tenant_id = :tid"
    if campaign_id:
        params["campaign_id"] = campaign_id
        campaign_filter += " AND ar.campaign_id = :campaign_id"

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

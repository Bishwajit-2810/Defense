"""Usage / cost tracking endpoint."""

from __future__ import annotations

from typing import Any

import redis.asyncio as aioredis
import structlog
from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from deps import get_current_user, get_db, get_redis

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/v1/usage", tags=["usage"])

# Rough cost estimate per 1 000 tokens (blended input+output, USD).
_COST_PER_1K_TOKENS: float = 0.002


# ---------------------------------------------------------------------------
# Response model
# ---------------------------------------------------------------------------


class UsageResponse(BaseModel):
    posts_analyzed: int
    """Total number of posts that have an analysis result row."""

    llm_calls: int
    """Number of those posts whose analysis was produced by an LLM."""

    llm_routing_rate: float
    """Fraction of analyses that used an LLM (0.0 – 1.0)."""

    total_tokens: int
    """Sum of token counts recorded in the llm_cache response JSONB.

    Reads ``response->>'total_tokens'`` when present; falls back to
    ``(response->'usage'->>'total_tokens')``.
    """

    estimated_cost_usd: float
    """Rough cost estimate: ``total_tokens / 1 000 * 0.002``."""

    llm_api_calls: int = 0
    """Fresh (non-cached) LLM API calls made by Stage-2 (Redis counter)."""

    cache_hits: int = 0
    """LLM tasks served from the response cache (Redis counter)."""

    cache_hit_rate: float
    """Fraction of LLM calls served from the cache vs issued fresh.

    Computed as ``cached_rows / (cached_rows + fresh_calls)`` where
    ``cached_rows`` is the size of ``llm_cache`` and ``fresh_calls`` is
    the LLM call count that could not have been cached (approximated as
    ``llm_calls - cached_rows`` when positive, else 0).
    """


# ---------------------------------------------------------------------------
# GET /v1/usage
# ---------------------------------------------------------------------------


@router.get(
    "",
    response_model=UsageResponse,
    status_code=status.HTTP_200_OK,
    summary="Per-tenant usage: posts analyzed, LLM calls, token counts and estimated cost",
)
async def get_usage(
    campaign_id: str | None = Query(None, description="Filter by campaign ID; omit for all campaigns"),
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
    user: dict = Depends(get_current_user),
) -> UsageResponse:
    """Return usage statistics drawn from the ``analysis_results`` and
    ``llm_cache`` Postgres tables.

    **Fields returned**

    | Field | Source |
    |-------|--------|
    | ``posts_analyzed`` | ``COUNT(*)`` from ``analysis_results`` |
    | ``llm_calls`` | rows where ``result->>'llm_used' = 'true'`` or ``result->'processing'->>'llm_used' = 'true'`` |
    | ``llm_routing_rate`` | ``llm_calls / posts_analyzed`` |
    | ``total_tokens`` | ``SUM`` of token counts extracted from ``llm_cache.response`` JSONB |
    | ``estimated_cost_usd`` | ``total_tokens / 1 000 * 0.002`` |
    | ``cache_hit_rate`` | ``llm_cache`` row count vs total LLM calls |
    """

    # ------------------------------------------------------------------
    # Query 1: analysis_results — post counts and LLM call counts
    # ------------------------------------------------------------------
    if campaign_id:
        analysis_row: Any = (
            await db.execute(
                text(
                    """
                    SELECT
                        COUNT(*)                                                   AS posts_analyzed,
                        SUM(
                            CASE
                                WHEN result->>'llm_used' = 'true'
                                  OR result->'processing'->>'llm_used' = 'true'
                                THEN 1 ELSE 0
                            END
                        )                                                          AS llm_calls
                    FROM analysis_results
                    WHERE campaign_id = :campaign_id
                    """
                ),
                {"campaign_id": campaign_id},
            )
        ).mappings().first()
    else:
        analysis_row = (
            await db.execute(
                text(
                    """
                    SELECT
                        COUNT(*)                                                   AS posts_analyzed,
                        SUM(
                            CASE
                                WHEN result->>'llm_used' = 'true'
                                  OR result->'processing'->>'llm_used' = 'true'
                                THEN 1 ELSE 0
                            END
                        )                                                          AS llm_calls
                    FROM analysis_results
                    """
                )
            )
        ).mappings().first()

    posts_analyzed: int = int(analysis_row["posts_analyzed"] or 0)
    llm_calls: int = int(analysis_row["llm_calls"] or 0)

    # ------------------------------------------------------------------
    # Query 2: llm_cache — total token counts and cache row count
    #
    # The response JSONB may store tokens under different keys depending
    # on which LLM backend wrote the cache entry:
    #   • response->>'total_tokens'                  (flat)
    #   • response->'usage'->>'total_tokens'         (OpenAI-style usage object)
    #   • response->'usage'->>'input_tokens' +
    #     response->'usage'->>'output_tokens'        (Anthropic-style)
    # ------------------------------------------------------------------
    token_row: Any = (
        await db.execute(
            text(
                """
                SELECT
                    COUNT(*)                                                 AS cache_rows,
                    COALESCE(SUM(
                        COALESCE(
                            (response->>'total_tokens')::bigint,
                            (response->'usage'->>'total_tokens')::bigint,
                            (
                                COALESCE((response->'usage'->>'input_tokens')::bigint, 0)
                                + COALESCE((response->'usage'->>'output_tokens')::bigint, 0)
                            )
                        )
                    ), 0)                                                    AS total_tokens
                FROM llm_cache
                """
            )
        )
    ).mappings().first()

    cache_rows: int = int(token_row["cache_rows"] or 0)
    total_tokens: int = int(token_row["total_tokens"] or 0)

    # ------------------------------------------------------------------
    # Query 3: Redis usage counters maintained by the Stage-2 worker
    # (usage:llm_calls, usage:cache_hits, usage:tokens:total). These are
    # authoritative when present; the llm_cache table read above is the
    # fallback for deployments without the counters.
    # ------------------------------------------------------------------
    r_calls = r_hits = r_tokens = 0
    try:
        r_calls = int(await redis.get("usage:llm_calls") or 0)
        r_hits = int(await redis.get("usage:cache_hits") or 0)
        r_tokens = int(await redis.get("usage:tokens:total") or 0)
    except Exception as exc:
        log.warning("usage_redis_counters_failed", error=str(exc))

    if r_tokens:
        total_tokens = r_tokens

    # ------------------------------------------------------------------
    # Derived metrics
    # ------------------------------------------------------------------
    llm_routing_rate: float = (llm_calls / posts_analyzed) if posts_analyzed > 0 else 0.0
    estimated_cost_usd: float = (total_tokens / 1_000) * _COST_PER_1K_TOKENS

    # cache_hit_rate: prefer the live Redis counters; fall back to the cache
    # row count as a proxy when no counters exist yet.
    if r_calls or r_hits:
        cache_hit_rate: float = r_hits / (r_calls + r_hits)
    else:
        total_llm_demand: int = max(llm_calls, cache_rows)
        cache_hit_rate = (cache_rows / total_llm_demand) if total_llm_demand > 0 else 0.0

    log.info(
        "usage_queried",
        campaign_id=campaign_id,
        posts_analyzed=posts_analyzed,
        llm_calls=llm_calls,
        total_tokens=total_tokens,
        cache_rows=cache_rows,
    )

    return UsageResponse(
        posts_analyzed=posts_analyzed,
        llm_calls=llm_calls,
        llm_routing_rate=round(llm_routing_rate, 4),
        total_tokens=total_tokens,
        estimated_cost_usd=round(estimated_cost_usd, 6),
        llm_api_calls=r_calls,
        cache_hits=r_hits,
        cache_hit_rate=round(cache_hit_rate, 4),
    )

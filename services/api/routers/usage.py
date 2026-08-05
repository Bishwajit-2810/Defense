"""Usage / cost tracking endpoint."""

from __future__ import annotations

import os
from typing import Any, Dict

import redis.asyncio as aioredis
import structlog
from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from deps import get_current_user, get_db, get_redis

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/v1/usage", tags=["usage"])

# Cost per 1 000 tokens, in USD, PER BACKEND (§5.8).
#
# One blended price used to be applied to every token, which is wrong for both
# backends in opposite directions: the `local` backend's marginal token cost is
# **zero** — that is the entire point of the pluggable local/cloud design — and
# Groq's real per-model prices differ by more than an order of magnitude. A
# cost-efficiency thesis cannot lean on a number that is wrong for every row.
#
# Local is 0.0 by definition (self-hosted; the cost is GPU time, not tokens, and
# reporting it as a token price would be double-counting). Groq prices are
# per-model, blended input+output, and env-overridable so they can be corrected
# without a code change when the vendor's pricing moves.
_COST_PER_1K_TOKENS_LOCAL: float = 0.0

_GROQ_PRICE_PER_1K: dict[str, float] = {
    "llama-3.1-8b-instant": 0.00008,
    "llama-3.3-70b-versatile": 0.00075,
    "meta-llama/llama-4-scout-17b-16e-instruct": 0.00019,
}
# Used for a Groq model not in the table above — deliberately the most expensive
# known price, so an unknown model over-estimates rather than under-estimates.
_GROQ_PRICE_FALLBACK: float = 0.00075


def _price_per_1k(backend: str, model: str) -> float:
    """USD per 1 000 tokens for a (backend, model) pair."""
    if backend == "local":
        return _COST_PER_1K_TOKENS_LOCAL
    env_key = f"GROQ_PRICE_PER_1K_{model.replace('/', '_').replace('-', '_').replace('.', '_').upper()}"
    override = os.environ.get(env_key)
    if override:
        try:
            return float(override)
        except ValueError:
            log.warning("usage_price_override_invalid", key=env_key, value=override)
    return _GROQ_PRICE_PER_1K.get(model, _GROQ_PRICE_FALLBACK)


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

    tokens_by_backend_model: Dict[str, int] = {}
    """Tokens per ``"{backend}:{model}"`` — the dimension §5.8 says to add
    BEFORE a measurement run, or the run has to be repeated. Without it there
    is no way to price local and Groq differently."""

    cost_by_backend_model: Dict[str, float] = {}
    """Per-model cost in USD, priced by backend. Local is 0.0 by definition."""

    lane_split: Dict[str, Dict[str, float]] = {}
    """Post-level vs comment-level cost split (§6.7).

    Before §6.3, Stage 2 was a handful of per-post calls, so post-level routing
    was the dominant cost lever and the 28% routing figure roughly *was* the
    cost story. With every comment reaching the LLM, comment labelling
    outnumbers post-level calls several-fold and the gate governs a minority of
    total spend. This field is what lets that be stated as a measurement rather
    than an assertion — the thesis lever becomes *"cheap NLP filters which
    comments and which posts deserve an LLM"*, not *"only N% of posts reach the
    LLM"*.
    """

    scope_note: str = ""
    """What the numbers above actually cover — see the ``campaign_id`` caveat."""


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
    | ``total_tokens`` | Redis ``usage:tokens:total``, else ``llm_cache.response`` JSONB |
    | ``tokens_by_backend_model`` | Redis ``usage:tokens:{backend}:{model}`` |
    | ``estimated_cost_usd`` | Σ per-model tokens × that model's price (local = 0) |
    | ``lane_split`` | Redis ``usage:{tokens,calls}:lane:{post,comment}`` |
    | ``cache_hit_rate`` | Redis counters, else ``llm_cache`` row count |

    **Scope caveat:** ``?campaign_id=`` filters the post counts only. Token,
    cost, cache and lane figures come from process-wide Redis counters. The
    response's ``scope_note`` states which is which rather than presenting
    campaign-scoped posts beside system-wide tokens as if both were filtered.
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
    tokens_by_backend_model: dict[str, int] = {}
    lane_tokens: dict[str, int] = {}
    lane_calls: dict[str, int] = {}
    try:
        r_calls = int(await redis.get("usage:llm_calls") or 0)
        r_hits = int(await redis.get("usage:cache_hits") or 0)
        r_tokens = int(await redis.get("usage:tokens:total") or 0)

        # Per-(backend, model) tokens — the §5.8 dimension.
        for entry in (await redis.smembers("usage:models")) or []:
            key = entry.decode() if isinstance(entry, bytes) else entry
            n = int(await redis.get(f"usage:tokens:{key}") or 0)
            if n:
                tokens_by_backend_model[key] = n

        # Post-level vs comment-level — the §6.7 split.
        for lane in ("post", "comment"):
            lane_tokens[lane] = int(await redis.get(f"usage:tokens:lane:{lane}") or 0)
            lane_calls[lane] = int(await redis.get(f"usage:calls:lane:{lane}") or 0)
    except Exception as exc:
        log.warning("usage_redis_counters_failed", error=str(exc))

    if r_tokens:
        total_tokens = r_tokens

    # ------------------------------------------------------------------
    # Derived metrics
    # ------------------------------------------------------------------
    llm_routing_rate: float = (llm_calls / posts_analyzed) if posts_analyzed > 0 else 0.0

    # Cost is summed per (backend, model) — local tokens are free, Groq tokens
    # are priced per model. A single blended rate was wrong for both.
    cost_by_backend_model: dict[str, float] = {}
    for key, n in tokens_by_backend_model.items():
        backend, _, model = key.partition(":")
        cost_by_backend_model[key] = round((n / 1_000) * _price_per_1k(backend, model), 6)
    if cost_by_backend_model:
        estimated_cost_usd: float = round(sum(cost_by_backend_model.values()), 6)
    else:
        # No dimensioned counters yet (nothing has run since the upgrade). Do
        # not invent a blended price — report 0 and say so in scope_note.
        estimated_cost_usd = 0.0

    lane_total_tokens = sum(lane_tokens.values())
    lane_total_calls = sum(lane_calls.values())
    lane_split: dict[str, dict[str, float]] = {
        lane: {
            "calls": lane_calls.get(lane, 0),
            "tokens": lane_tokens.get(lane, 0),
            "token_share": round(lane_tokens.get(lane, 0) / lane_total_tokens, 4)
            if lane_total_tokens else 0.0,
            "call_share": round(lane_calls.get(lane, 0) / lane_total_calls, 4)
            if lane_total_calls else 0.0,
        }
        for lane in ("post", "comment")
    }

    # §5.8 defect 3: ?campaign_id= is honoured by ONE query out of three. The
    # post counts below are campaign-scoped; the Redis counters and the
    # llm_cache token/row query are process-global. Rather than return
    # campaign-scoped posts beside system-wide tokens under a docstring that
    # says "per-tenant", say which is which.
    if campaign_id:
        scope_note = (
            f"posts_analyzed/llm_calls/llm_routing_rate are scoped to campaign "
            f"'{campaign_id}'. Token, cost, cache and lane figures come from "
            "process-wide counters and are NOT campaign-scoped."
        )
    else:
        scope_note = "All figures are system-wide."
    if not cost_by_backend_model:
        scope_note += (
            " No per-(backend, model) token counters recorded yet, so "
            "estimated_cost_usd is 0.0 rather than a blended guess."
        )

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
        tokens_by_backend_model=tokens_by_backend_model,
        cost_by_backend_model=cost_by_backend_model,
        lane_split=lane_split,
        scope_note=scope_note,
    )

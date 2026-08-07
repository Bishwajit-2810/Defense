"""Redis usage counters — the single source `GET /v1/usage` reads.

Why this module exists
----------------------
These counters lived in ``services/workers/stage2_llm/worker.py`` and were
incremented there and nowhere else. Every other LLM caller in the system spent
tokens that reached no counter (PROJECT_ASSESSMENT §13.4):

    POST /v1/chat, POST /v1/chat/stream       — unbounded, user-driven
    reports._llm_narrative                    — 1 per grounded report
    reports._summarize_cluster                — up to 8 per grounded report
    stage1_nlp/llm_analyzer                   — per post, when STAGE1_LLM=true
    agents/runner                             — per agent turn

Meanwhile ``UsageResponse.total_tokens`` said *"Total tokens spent"* and
``scope_note`` — the field §5.8 added specifically to state what the numbers
cover — said *"All figures are system-wide."* They covered Stage 2.

So the counters moved **down** to `LLMClient`, which is the layer every one of
those callers already goes through. Tracking a call is now the default rather
than something each new call site has to remember, which is the only version of
this that stays true: §13.4 happened because five call sites were added over time
and none of their authors knew there was a counter to increment.

The dimensions (§5.8)
---------------------
The global counters cannot answer the question a cost-efficiency thesis asks, so
each call is also recorded per backend+model, per lane and per task:

    usage:tokens:{backend}:{model}  local's marginal token cost is ZERO and
        Groq's per-model prices differ by >1 order of magnitude, so one blended
        price is wrong for both, in opposite directions.
    usage:tokens:lane:{lane}        post vs comment vs interactive (§6.7).
    usage:calls:lane:{lane}
    usage:calls:task:{task}         cached and fresh alike — a cached task still
        ran. Only ``usage:llm_calls`` is fresh-only, because that is what
        ``cache_hit_rate`` divides by (§12.4b — do not "fix" this into
        consistency, the asymmetry is the point).

Add a dimension BEFORE a measurement run, or the run has to be repeated.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Lanes
# ---------------------------------------------------------------------------
#: Once per post: summary / post_type / insight.
LANE_POST = "post"
#: Many per post: comment stance, comment-thread summary.
LANE_COMMENT = "comment"
#: Everything a human waits on — chat, reports, agents. Kept out of the
#: post/comment split so §6.8's per-post cost model is not polluted by a
#: chatbot session, while still landing in the global totals and the per-model
#: spend, which is what `estimated_cost_usd` needs.
LANE_INTERACTIVE = "interactive"
#: Stage-1's own LLM path (STAGE1_LLM=true). Per post, and per comment when it
#: labels comments — §6.8 measured this at 96% of all calls in that mode, and
#: none of it was counted.
LANE_STAGE1 = "stage1"
#: Agent runs. Its own lane rather than part of `interactive` because the cost
#: shape is different: a tool-calling loop re-sends a growing transcript every
#: turn, so one agent question can cost many multiples of one chat turn.
LANE_AGENT = "agent"

ALL_LANES: tuple[str, ...] = (
    LANE_POST,
    LANE_COMMENT,
    LANE_STAGE1,
    LANE_INTERACTIVE,
    LANE_AGENT,
)

#: The lanes that make up the per-post pipeline cost model (§6.7/§6.8). Kept
#: separate from the interactive lanes so a chatbot session cannot inflate the
#: per-post cost figure the thesis quotes.
PIPELINE_LANES: tuple[str, ...] = (LANE_POST, LANE_COMMENT, LANE_STAGE1)

#: Set to disable counter writes entirely (offline evals, benchmarks).
_DISABLED = os.environ.get("LLM_USAGE_TRACKING_DISABLED", "").lower() in ("1", "true", "yes")

_shared_redis: Any = None
_shared_redis_failed = False


def _default_redis() -> Optional[Any]:
    """A lazily-built async Redis client for callers that have none.

    The API routers and the agent runner do not hold a Redis handle at their LLM
    call sites, and threading one through every one of them is exactly the
    per-call-site burden this module exists to remove. Built once, and never
    retried after a failure — usage tracking must never be the reason a request
    fails.
    """
    global _shared_redis, _shared_redis_failed
    if _DISABLED or _shared_redis_failed:
        return None
    if _shared_redis is None:
        try:
            import redis.asyncio as aioredis  # noqa: PLC0415

            url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
            _shared_redis = aioredis.from_url(url, decode_responses=False)
        except Exception as exc:
            _shared_redis_failed = True
            log.warning("usage tracking unavailable (no redis): %s", exc)
            return None
    return _shared_redis


async def track_usage(
    redis: Any = None,
    response: dict | None = None,
    cache_hit: bool = False,
    *,
    lane: str = LANE_POST,
    task: str = "unknown",
) -> None:
    """Increment the counters `GET /v1/usage` reads. Never raises.

    ``redis`` may be omitted, in which case a shared client is used. Tracking is
    best-effort by design: a cost counter must not be able to fail a request.
    """
    if _DISABLED:
        return
    client = redis if redis is not None else _default_redis()
    if client is None:
        return
    try:
        if cache_hit:
            await client.incr("usage:cache_hits")
            await client.incr(f"usage:cache_hits:lane:{lane}")
            # A cached task is still a task. This counter used to be incremented
            # on the fresh-call path only, so `usage:calls:task:{task}` measured
            # cache MISSES per task while reading as "how often each task runs" —
            # and the better the cache worked, the more it understated. The global
            # `usage:llm_calls` is deliberately left alone: it means "fresh API
            # calls", which is what /v1/usage's cache_hit_rate denominator needs.
            await client.incr(f"usage:calls:task:{task}")
            return
        await client.incr("usage:llm_calls")
        await client.incr(f"usage:calls:lane:{lane}")
        await client.incr(f"usage:calls:task:{task}")

        usage = (response or {}).get("usage") or {}
        total = int(usage.get("total_tokens") or 0)
        if not total:
            return
        await client.incrby("usage:tokens:total", total)
        await client.incrby(f"usage:tokens:lane:{lane}", total)
        backend = (response or {}).get("backend") or "unknown"
        model = (response or {}).get("model") or "unknown"
        await client.incrby(f"usage:tokens:{backend}:{model}", total)
        await client.sadd("usage:models", f"{backend}:{model}")
    except Exception as exc:
        log.warning("usage_tracking_failed: %s", exc)


__all__ = [
    "ALL_LANES",
    "LANE_AGENT",
    "LANE_COMMENT",
    "LANE_INTERACTIVE",
    "LANE_POST",
    "LANE_STAGE1",
    "PIPELINE_LANES",
    "track_usage",
]

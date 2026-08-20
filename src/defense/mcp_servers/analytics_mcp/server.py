"""
analytics-mcp  —  ClickHouse + Postgres analytics tools exposed over MCP.

Real MCP server (FastMCP, streamable-HTTP transport). The agent orchestrator
connects with an MCP client and discovers/calls these tools over the protocol;
there is no bespoke REST contract any more. Tool schemas are derived from the
typed function signatures below.

Environment variables
---------------------
ANALYTICS_MCP_STUB         Set to "true" to return synthetic data without a real DB (dev mode).
CLICKHOUSE_HOST            ClickHouse host (default: localhost)
CLICKHOUSE_PORT            ClickHouse native TCP port (default: 9000)
CLICKHOUSE_DB              Database name (default: defense)
CLICKHOUSE_USER            (default: default)
CLICKHOUSE_PASSWORD        (default: "")

The service is ClusterIP-only; it is never exposed outside the cluster.
"""

from __future__ import annotations

import math
import os
import re
import time
from defense.libs.common.config import get_settings
config = get_settings()
import random
from datetime import date, datetime, timedelta
from typing import Annotated, Any, Literal

import structlog
from fastmcp import FastMCP
from pydantic import Field
from starlette.requests import Request
from starlette.responses import JSONResponse

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

structlog.configure(
    processors=[
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer(),
    ]
)
log = structlog.get_logger("analytics-mcp")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

STUB_MODE: bool = config.analytics_mcp_stub

# These five are documented at the top of this module as the way to point the
# server at a ClickHouse — and were hardcoded, so none of them did anything.
# "clickhouse" only resolves inside the compose network, and the credentials
# were `default`/`""` while the deployment uses defense/defense: every non-stub
# run outside Docker failed to connect. That is what run_all.py's hardcoded
# ANALYTICS_MCP_STUB=true was hiding.
CH_HOST = os.getenv("CLICKHOUSE_HOST", "clickhouse")
CH_PORT = int(os.getenv("CLICKHOUSE_PORT", "9000"))
CH_DB = os.getenv("CLICKHOUSE_DB", "defense")
CH_USER = os.getenv("CLICKHOUSE_USER", "default")
CH_PASSWORD = os.getenv("CLICKHOUSE_PASSWORD", "")

# ---------------------------------------------------------------------------
# ClickHouse client (lazy, synchronous driver run in threadpool)
# ---------------------------------------------------------------------------

_ch_client = None


def _get_ch_client():
    """Return a shared clickhouse_driver.Client, creating it on first call."""
    global _ch_client
    if _ch_client is None:
        from clickhouse_driver import Client  # type: ignore

        _ch_client = Client(
            host=CH_HOST,
            port=CH_PORT,
            database=CH_DB,
            user=CH_USER,
            password=CH_PASSWORD,
            connect_timeout=5,
            send_receive_timeout=30,
        )
    return _ch_client


def _ch_query(sql: str, params: dict | None = None) -> list[dict]:
    """Execute a ClickHouse query and return rows as a list of dicts."""
    client = _get_ch_client()
    rows, columns_meta = client.execute(sql, params or {}, with_column_types=True)
    col_names = [col[0] for col in columns_meta]
    return [dict(zip(col_names, row)) for row in rows]


# ---------------------------------------------------------------------------
# Granularity helpers
# ---------------------------------------------------------------------------

_GRANULARITY_MAP = {
    "hour": "1 HOUR",
    "day": "1 DAY",
    "week": "1 WEEK",
}


def _interval(granularity: str) -> str:
    mapped = _GRANULARITY_MAP.get(granularity, "1 DAY")
    return mapped


_ALL_CAMPAIGNS = {"all", "all campaigns", "all_campaigns", "*", "any"}
_UNSPECIFIED = {"", "none", "null", "undefined", "n/a"}

# A campaign id is a CUID/UUID/short slug: no spaces, no punctuation beyond -
# and _. An argument that does not match is an unfilled schema placeholder
# ("CUID/UUID of the campaign to query."), not an id.
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def _clean_campaign_id(cid: Any) -> str | None:
    """Normalise an LLM-supplied campaign_id, or raise if it is a placeholder.

    ``None`` means "no campaign filter" — every aggregate below then runs over
    the whole corpus, so it is returned only when the caller left the argument
    out or explicitly asked for every campaign. Junk is rejected rather than
    coerced: silently widening a scoped query answers a different question than
    the one asked, and the answer still looks campaign-scoped.
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


def _log_scope(tool: str, cid: str | None) -> None:
    """Make a corpus-wide aggregate visible as one in the logs.

    A tool called without a campaign filter answers a different question than the
    same tool called with one, and the payloads look identical.
    """
    if cid is None:
        log.info("campaign_scope_all_campaigns", tool=tool)


# The stub corpus spans this many days back from today. A window outside it
# yields no periods — which is what a real corpus of recent posts does.
_STUB_CORPUS_DAYS = 90

# Corpus bounds are cached: they are only consulted when a caller omits the
# dates, but that is every well-behaved agent call now.
_CORPUS_TTL_SECONDS = 300
_corpus_cache: tuple[float, tuple[str, str] | None] | None = None


def _corpus_window() -> tuple[str, str]:
    """The window the corpus actually covers — the default when none is given.

    "Last 30 days" is the wrong default for an archive. This corpus spans
    2026-04-29..2026-05-19 while today is 2026-08-16, so a 30-day default
    returns nothing at all, and an agent told to omit its dates would get an
    empty answer for a question the data can answer.

    Falls back to the last 30 days if the bounds cannot be read: a default that
    might be wrong beats failing a query over it.
    """
    global _corpus_cache
    today = date.today()
    fallback = ((today - timedelta(days=30)).isoformat(), today.isoformat())

    if STUB_MODE:
        return ((today - timedelta(days=_STUB_CORPUS_DAYS)).isoformat(), today.isoformat())

    now = time.time()
    if _corpus_cache is not None and now - _corpus_cache[0] < _CORPUS_TTL_SECONDS:
        return _corpus_cache[1] or fallback

    bounds: tuple[str, str] | None = None
    try:
        rows = _ch_query(
            "SELECT min(created_at) AS first, max(created_at) AS last FROM analysis_events"
        )
        first, last = (rows[0].get("first"), rows[0].get("last")) if rows else (None, None)
        if first and last:
            bounds = (str(first)[:10], str(last)[:10])
    except Exception as exc:  # unreachable DB, empty table, driver error
        log.warning("corpus_bounds_unavailable", error=str(exc))

    _corpus_cache = (now, bounds)
    return bounds or fallback


def _clean_dates(from_d: Any, to_d: Any) -> tuple[str, str]:
    """Resolve a date window, defaulting to the window the corpus covers.

    Blanks and unfilled placeholders fall back to that default. Real dates are
    honoured — but only ones that could contain data.

    A model with no notion of the current date asks for the window it saw in
    training: a live Analyst run requested 2023-01-01..2023-12-31 and got back a
    fabricated year of stub numbers, because a syntactically valid date used to
    pass through untouched. Nothing downstream could tell the window was wrong.
    A future window is now rejected rather than silently emptied — the error
    carries today's date, so the model can correct itself; silence gets
    fabricated over.
    """
    today = date.today()
    default_to = today.isoformat()

    # Resolved lazily and at most once: a caller that supplied usable dates must
    # not trigger a bounds query it has no use for.
    memo: list[tuple[str, str]] = []

    def _default(which: int) -> str:
        if not memo:
            memo.append(_corpus_window())
        return memo[0][which]

    def _parse(raw: Any, which: int) -> str:
        s = str(raw or "").strip()
        if not s or "YYYY" in s or s.lower() in ("none", "null"):
            return _default(which)
        try:
            return date.fromisoformat(s[:10]).isoformat()
        except Exception:
            return _default(which)

    f_str = _parse(from_d, 0)
    t_str = _parse(to_d, 1)

    # ISO dates compare correctly as strings.
    if f_str > default_to:
        raise ValueError(
            f"from_date {f_str} is in the future — today is {default_to}. Pass a "
            "window on or before today, or omit the dates entirely to query the "
            f"window the corpus covers ({_default(0)}..{_default(1)})."
        )
    if f_str > t_str:
        raise ValueError(
            f"from_date {f_str} is after to_date {t_str} — today is {default_to}."
        )
    # No data can exist after today, so trim instead of rejecting: the window
    # still asks the same question, it just cannot run past the corpus.
    if t_str > default_to:
        t_str = default_to

    return f_str, t_str


# ---------------------------------------------------------------------------
# Stub data generators
# ---------------------------------------------------------------------------

def _stub_date_series(from_date: str | None, to_date: str | None, granularity: str) -> list[str]:
    """Generate ISO date strings for the stub data, inside the stub corpus only.

    The stub corpus is the last ``_STUB_CORPUS_DAYS`` days, so a window that
    falls outside it returns nothing — the same answer a real ClickHouse corpus
    of recent posts gives. This used to emit a row for every period in whatever
    window it was handed, which is how a request for 2023 came back as a
    fabricated year of sentiment counts that looked exactly like retrieved data.
    """
    f_d, t_d = _clean_dates(from_date, to_date)
    corpus_start = date.today() - timedelta(days=_STUB_CORPUS_DAYS)
    start = max(date.fromisoformat(f_d), corpus_start)
    end = min(date.fromisoformat(t_d), date.today())
    step = {"hour": timedelta(hours=1), "day": timedelta(days=1), "week": timedelta(weeks=1)}.get(
        granularity, timedelta(days=1)
    )
    result: list[str] = []
    current = start
    while current <= end:
        result.append(current.isoformat())
        current += step
        if len(result) >= 90:  # cap stub series
            break
    return result


def _stub_trend_query(campaign_id: str, from_date: str, to_date: str, granularity: str) -> list[dict]:
    cid = _clean_campaign_id(campaign_id)
    periods = _stub_date_series(from_date, to_date, granularity)
    random.seed(cid or "all")
    return [
        {
            "period": p,
            "count": random.randint(10, 500),
            "avg_sentiment": round(random.uniform(-1.0, 1.0), 4),
            "avg_toxicity": round(random.uniform(0.0, 1.0), 4),
        }
        for p in periods
    ]


def _stub_sentiment_over_time(campaign_id: str, from_date: str, to_date: str, granularity: str) -> list[dict]:
    cid = _clean_campaign_id(campaign_id)
    periods = _stub_date_series(from_date, to_date, granularity)
    random.seed((cid or "all") + "sent")
    return [
        {
            "period": p,
            "positive": random.randint(5, 200),
            "negative": random.randint(5, 150),
            "neutral": random.randint(10, 300),
            "mixed": random.randint(1, 50),
        }
        for p in periods
    ]


def _stub_top_posts(
    campaign_id: str,
    metric: str,
    limit: int,
    min_toxicity: float | None = None,
) -> list[dict]:
    cid = _clean_campaign_id(campaign_id)
    random.seed((cid or "all") + metric)
    rows = [
        {
            "post_id": f"stub-post-{i:04d}",
            "total_reactions": random.randint(0, 10000),
            "comment_count": random.randint(0, 2000),
            "toxicity_score": round(random.uniform(0.0, 1.0), 4),
            "hate_speech_score": round(random.uniform(0.0, 1.0), 4),
            "overall_sentiment": random.choice(["positive", "negative", "neutral", "mixed"]),
        }
        for i in range(1, limit + 1)
    ]
    if min_toxicity is not None:
        rows = [r for r in rows if r["toxicity_score"] >= float(min_toxicity)]
    return rows


def _stub_reaction_mix(campaign_id: str) -> dict:
    cid = _clean_campaign_id(campaign_id)
    random.seed((cid or "all") + "react")
    raw = {k: random.randint(10, 5000) for k in ["LIKE", "LOVE", "HAHA", "WOW", "SAD", "ANGRY", "CARE"]}
    total = sum(raw.values()) or 1
    return {
        "totals": raw,
        "percentages": {k: round(v / total * 100, 2) for k, v in raw.items()},
        "total_reactions": total,
    }


# ---------------------------------------------------------------------------
# Latest-row-per-post deduplication
# ---------------------------------------------------------------------------
# `analysis_events` is an append-only MergeTree, and re-analysis is a
# first-class operation (`POST /v1/analysis/run` re-normalizes from
# `posts.raw_payload` and replays the pipeline). So a post analysed twice has
# TWO rows, and every aggregate below used to count it twice, weight it twice
# in every average, and let it appear twice in a top-N list. The skew is not
# random — it pulls toward whichever posts happened to be re-run, which is
# exactly what a live "flip the backend and re-run" demo does.
#
# Postgres (`ON CONFLICT (post_id) DO UPDATE`) and object storage (deterministic
# key) are idempotent, and the sibling `comment_sentiments` table is a
# ReplacingMergeTree — so ClickHouse's post-level table was the one store where
# FEATURES.md §1's "re-analysis is a first-class operation" did not hold.
#
# Fixed on the READ side rather than by migrating the table. The table is named
# `analysis_events` and being append-only is defensible — the per-run history is
# real information. What was wrong is aggregating over it without collapsing to
# one row per post. `LIMIT 1 BY post_id` after `ORDER BY inserted_at DESC` keeps
# the newest row per post, which is the same "latest wins" rule the Postgres
# upsert applies. No schema change, so no migration on existing deployments.
#
# `run_all.py::reset_data` still truncates on reset; that is now a convenience
# rather than the thing correctness depends on.
_LATEST_PER_POST = "ORDER BY inserted_at DESC\n            LIMIT 1 BY post_id"


# ---------------------------------------------------------------------------
# Tool handlers (real ClickHouse path)
# ---------------------------------------------------------------------------

def _handle_trend_query(
    campaign_id: str,
    from_date: str | None,
    to_date: str | None,
    granularity: str = "day",
    metric: str = "post_count",
) -> list[dict]:
    cid = _clean_campaign_id(campaign_id)
    _log_scope("trend_query", cid)
    f_d, t_d = _clean_dates(from_date, to_date)
    if STUB_MODE:
        return _stub_trend_query(cid, f_d, t_d, granularity)

    interval = _interval(granularity)
    where_parts = ["created_at BETWEEN %(from_date)s AND %(to_date)s"]
    params: dict[str, Any] = {"from_date": f_d, "to_date": t_d}
    if cid is not None:
        where_parts.append("campaign_id = %(campaign_id)s")
        params["campaign_id"] = cid

    where_str = " AND ".join(where_parts)
    # Aggregates run over ONE row per post — see _LATEST_PER_POST.
    sql = f"""
        SELECT
            toStartOfInterval(created_at, INTERVAL {interval}) AS period,
            count()                                              AS count,
            avg(sentiment_score)                                 AS avg_sentiment,
            avg(toxicity_score)                                  AS avg_toxicity
        FROM (
            SELECT post_id, created_at, sentiment_score, toxicity_score
            FROM analysis_events
            WHERE {where_str}
            {_LATEST_PER_POST}
        )
        GROUP BY period
        ORDER BY period
    """
    rows = _ch_query(sql, params)
    # Serialize datetime objects returned by the driver
    for row in rows:
        if isinstance(row.get("period"), datetime):
            row["period"] = row["period"].isoformat()
    return rows


def _handle_sentiment_over_time(
    campaign_id: str,
    from_date: str | None,
    to_date: str | None,
    granularity: str = "day",
) -> list[dict]:
    cid = _clean_campaign_id(campaign_id)
    _log_scope("sentiment_over_time", cid)
    f_d, t_d = _clean_dates(from_date, to_date)
    if STUB_MODE:
        return _stub_sentiment_over_time(cid, f_d, t_d, granularity)

    interval = _interval(granularity)
    where_parts = ["created_at BETWEEN %(from_date)s AND %(to_date)s"]
    params: dict[str, Any] = {"from_date": f_d, "to_date": t_d}
    if cid is not None:
        where_parts.append("campaign_id = %(campaign_id)s")
        params["campaign_id"] = cid

    where_str = " AND ".join(where_parts)
    sql = f"""
        SELECT
            period,
            countIf(overall_sentiment = 'positive') AS positive,
            countIf(overall_sentiment = 'negative') AS negative,
            countIf(overall_sentiment = 'neutral')  AS neutral,
            countIf(overall_sentiment = 'mixed')    AS mixed
        FROM (
            SELECT
                toStartOfInterval(created_at, INTERVAL {interval}) AS period,
                post_id,
                overall_sentiment
            FROM analysis_events
            WHERE {where_str}
            {_LATEST_PER_POST}
        )
        GROUP BY period
        ORDER BY period
    """
    rows = _ch_query(sql, params)
    for row in rows:
        if isinstance(row.get("period"), datetime):
            row["period"] = row["period"].isoformat()
    return rows


def _handle_top_posts(
    campaign_id: str,
    metric: str = "total_reactions",
    limit: int = 10,
    from_date: str | None = None,
    to_date: str | None = None,
    min_toxicity: float | None = None,
) -> list[dict]:
    # Allowlist, because `metric` is interpolated into the ORDER BY. Rejecting is
    # the point: silently ranking by reactions when the caller asked for toxicity
    # answers a different question under the requested question's label, and the
    # agent runner hands a ValueError back to the model, which then retries.
    allowed_metrics = {"total_reactions", "comment_count", "toxicity_score", "hate_speech_score"}
    if metric not in allowed_metrics:
        raise ValueError(f"metric must be one of {sorted(allowed_metrics)}, got {metric!r}")

    limit = min(max(int(limit or 10), 1), 100)
    cid = _clean_campaign_id(campaign_id)
    _log_scope("top_posts", cid)

    if STUB_MODE:
        return _stub_top_posts(cid, metric, limit, min_toxicity)

    where_parts = ["1=1"]
    params: dict[str, Any] = {"limit": limit}
    if cid is not None:
        where_parts.append("campaign_id = %(campaign_id)s")
        params["campaign_id"] = cid

    if from_date and "YYYY" not in str(from_date) and to_date and "YYYY" not in str(to_date):
        f_d, t_d = _clean_dates(from_date, to_date)
        where_parts.append("created_at BETWEEN %(from_date)s AND %(to_date)s")
        params["from_date"] = f_d
        params["to_date"] = t_d

    if min_toxicity is not None:
        where_parts.append("toxicity_score >= %(min_toxicity)s")
        params["min_toxicity"] = float(min_toxicity)

    where_str = " AND ".join(where_parts)
    # Rank over ONE row per post, or a re-analysed post occupies several slots
    # in its own top-N list — see _LATEST_PER_POST.
    sql = f"""
        SELECT
            post_id,
            total_reactions,
            comment_count,
            toxicity_score,
            hate_speech_score,
            overall_sentiment
        FROM (
            SELECT
                post_id, total_reactions, comment_count,
                toxicity_score, hate_speech_score, overall_sentiment
            FROM analysis_events
            WHERE {where_str}
            {_LATEST_PER_POST}
        )
        ORDER BY {metric} DESC
        LIMIT %(limit)s
    """
    return _ch_query(sql, params)


def _handle_reaction_mix(
    campaign_id: str,
    from_date: str | None = None,
    to_date: str | None = None,
) -> dict:
    cid = _clean_campaign_id(campaign_id)
    _log_scope("reaction_mix", cid)
    if STUB_MODE:
        return _stub_reaction_mix(cid)

    where_parts = ["1=1"]
    params: dict[str, Any] = {}
    if cid is not None:
        where_parts.append("campaign_id = %(campaign_id)s")
        params["campaign_id"] = cid

    if from_date and "YYYY" not in str(from_date) and to_date and "YYYY" not in str(to_date):
        f_d, t_d = _clean_dates(from_date, to_date)
        where_parts.append("created_at BETWEEN %(from_date)s AND %(to_date)s")
        params["from_date"] = f_d
        params["to_date"] = t_d

    where_str = " AND ".join(where_parts)
    sql = f"""
        SELECT
            sum(like_count)    AS LIKE,
            sum(love_count)    AS LOVE,
            sum(haha_count)    AS HAHA,
            sum(wow_count)     AS WOW,
            sum(sad_count)     AS SAD,
            sum(angry_count)   AS ANGRY,
            sum(care_count)    AS CARE
        FROM (
            SELECT
                post_id, like_count, love_count, haha_count,
                wow_count, sad_count, angry_count, care_count
            FROM analysis_events
            WHERE {where_str}
            {_LATEST_PER_POST}
        )
    """
    rows = _ch_query(sql, params)
    if not rows:
        return {"totals": {}, "percentages": {}, "total_reactions": 0}

    totals: dict[str, int] = {k: int(v or 0) for k, v in rows[0].items()}
    total = sum(totals.values())
    # Divide by a guard, but REPORT the real total: `or 1` used to leak into the
    # payload and tell the agent a campaign with no reactions had one.
    denom = total or 1
    percentages = {k: round(v / denom * 100, 2) for k, v in totals.items()}
    return {"totals": totals, "percentages": percentages, "total_reactions": total}


# ---------------------------------------------------------------------------
# MCP server + tool definitions
# ---------------------------------------------------------------------------

mcp = FastMCP(
    name="analytics-mcp",
    instructions=(
        "ClickHouse + Postgres analytics over analyzed social-media posts. "
        "Corpus/campaign-level aggregations only — never per-post analysis."
    ),
)


@mcp.tool
def trend_query(
    campaign_id: Annotated[str, Field(description="CUID/UUID of the campaign to query.")],
    from_date: Annotated[str | None, Field(
        description="Inclusive start date (YYYY-MM-DD). OMIT unless the operator "
                    "named a window — omitting queries the range the corpus "
                    "actually covers. Must not be in the future."
    )] = None,
    to_date: Annotated[str | None, Field(
        description="Inclusive end date (YYYY-MM-DD). OMIT unless the operator "
                    "named a window — omitting queries the range the corpus "
                    "actually covers. Must not be in the future."
    )] = None,
    granularity: Literal["hour", "day", "week"] = "day",
    metric: Literal["post_count", "avg_sentiment", "avg_toxicity"] = "post_count",
) -> list[dict]:
    """Query analytics time-series for a campaign.

    Each row includes: period, count, avg_sentiment, avg_toxicity. All three
    measures come back on every call regardless of the `metric` argument, so
    one call covers all of them — do not call this once per metric."""
    log.info("tool_call", tool="trend_query", campaign_id=campaign_id, stub=STUB_MODE)
    return _handle_trend_query(campaign_id, from_date, to_date, granularity, metric)


@mcp.tool
def sentiment_over_time(
    campaign_id: Annotated[str, Field(description="CUID/UUID of the campaign to query.")],
    from_date: Annotated[str | None, Field(
        description="Inclusive start date (YYYY-MM-DD). OMIT unless the operator "
                    "named a window — omitting queries the range the corpus "
                    "actually covers. Must not be in the future."
    )] = None,
    to_date: Annotated[str | None, Field(
        description="Inclusive end date (YYYY-MM-DD). OMIT unless the operator "
                    "named a window — omitting queries the range the corpus "
                    "actually covers. Must not be in the future."
    )] = None,
    granularity: Literal["hour", "day", "week"] = "day",
) -> list[dict]:
    """Returns the sentiment breakdown per time period for a campaign.

    Each row includes: period, positive, negative, neutral, mixed. The four
    sentiment figures are POST COUNTS in that period, not percentages and not
    per-post scores — for sentiment attached to individual posts, use
    top_posts, whose rows carry overall_sentiment."""
    log.info("tool_call", tool="sentiment_over_time", campaign_id=campaign_id, stub=STUB_MODE)
    return _handle_sentiment_over_time(campaign_id, from_date, to_date, granularity)


@mcp.tool
def top_posts(
    campaign_id: Annotated[str, Field(description="CUID/UUID of the campaign to query.")],
    metric: Literal[
        "total_reactions", "comment_count", "toxicity_score", "hate_speech_score"
    ] = "total_reactions",
    limit: Annotated[int, Field(description="Number of posts to return (max 100).", ge=1, le=100)] = 10,
    from_date: Annotated[str | None, Field(description="Optional inclusive start date (YYYY-MM-DD).")] = None,
    to_date: Annotated[str | None, Field(description="Optional inclusive end date (YYYY-MM-DD).")] = None,
    min_toxicity: Annotated[
        float | None,
        Field(description="Optional filter: only posts whose toxicity score is >= this (0.0-1.0).", ge=0.0, le=1.0),
    ] = None,
) -> list[dict]:
    """Returns the top posts for a campaign ranked by the given metric (descending),
    optionally restricted to posts at or above a toxicity threshold.

    Each row includes: post_id, total_reactions, comment_count, toxicity_score,
    hate_speech_score, overall_sentiment. Ranking is limited to the four numeric
    metrics above, but every row carries overall_sentiment regardless of how it
    was ranked — to compare sentiment across top posts, rank by
    total_reactions and read overall_sentiment from the rows."""
    log.info(
        "tool_call", tool="top_posts", campaign_id=campaign_id, metric=metric,
        min_toxicity=min_toxicity, stub=STUB_MODE,
    )
    return _handle_top_posts(campaign_id, metric, limit, from_date, to_date, min_toxicity)


@mcp.tool
def reaction_mix(
    campaign_id: Annotated[str, Field(description="CUID/UUID of the campaign to query.")],
    from_date: Annotated[str | None, Field(description="Optional inclusive start date (YYYY-MM-DD).")] = None,
    to_date: Annotated[str | None, Field(description="Optional inclusive end date (YYYY-MM-DD).")] = None,
) -> dict:
    """Returns the aggregated reaction breakdown for a campaign.

    Returns one object (not rows): {"totals": {...}, "percentages": {...},
    "total_reactions": int}, where totals and percentages are both keyed by
    LIKE, LOVE, HAHA, WOW, SAD, ANGRY, CARE. Percentages are already computed —
    do not recompute them from the totals."""
    log.info("tool_call", tool="reaction_mix", campaign_id=campaign_id, stub=STUB_MODE)
    return _handle_reaction_mix(campaign_id, from_date, to_date)


def _stub_watchlist_timeline(
    campaign_id: str, from_date: str, to_date: str, granularity: str
) -> list[dict]:
    cid = _clean_campaign_id(campaign_id)
    out = []
    for i, period in enumerate(_stub_date_series(from_date, to_date, granularity)):
        posts = 8 + (i % 5)
        alerts = i % 4
        out.append({
            "period": period,
            "posts": posts,
            "alerts": alerts,
            "alert_share": round(alerts / posts, 4),
            "negative_posts": 2 + (i % 3),
            "mean_label_agreement": round(0.82 + (i % 5) * 0.02, 4),
        })
    return out


def _handle_watchlist_timeline(
    campaign_id: str, from_date: str | None, to_date: str | None, granularity: str = "day"
) -> list[dict]:
    cid = _clean_campaign_id(campaign_id)
    _log_scope("watchlist_timeline", cid)
    f_d, t_d = _clean_dates(from_date, to_date)
    if STUB_MODE:
        return _stub_watchlist_timeline(cid, f_d, t_d, granularity)

    interval = _interval(granularity)
    where_parts = ["created_at BETWEEN %(from_date)s AND %(to_date)s"]
    params: dict[str, Any] = {"from_date": f_d, "to_date": t_d}
    if cid is not None:
        where_parts.append("campaign_id = %(campaign_id)s")
        params["campaign_id"] = cid

    where_str = " AND ".join(where_parts)
    sql = f"""
        SELECT
            period,
            count() AS posts,
            countIf(watchlist_alert = 1) AS alerts,
            countIf(overall_sentiment = 'negative') AS negative_posts,
            round(avg(label_agreement), 4) AS mean_label_agreement
        FROM (
            SELECT
                toStartOfInterval(created_at, INTERVAL {interval}) AS period,
                post_id,
                watchlist_alert,
                overall_sentiment,
                label_agreement
            FROM analysis_events
            WHERE {where_str}
            {_LATEST_PER_POST}
        )
        GROUP BY period
        ORDER BY period
    """
    rows = _ch_query(sql, params)
    for row in rows:
        if isinstance(row.get("period"), datetime):
            row["period"] = row["period"].isoformat()
        posts = row.get("posts") or 0
        row["alert_share"] = round((row.get("alerts") or 0) / posts, 4) if posts else 0.0
    return rows


@mcp.tool
def watchlist_timeline(
    campaign_id: Annotated[str, Field(description="CUID/UUID of the campaign to query.")],
    from_date: Annotated[str | None, Field(
        description="Inclusive start date (YYYY-MM-DD). OMIT unless the operator "
                    "named a window — omitting queries the range the corpus "
                    "actually covers. Must not be in the future."
    )] = None,
    to_date: Annotated[str | None, Field(
        description="Inclusive end date (YYYY-MM-DD). OMIT unless the operator "
                    "named a window — omitting queries the range the corpus "
                    "actually covers. Must not be in the future."
    )] = None,
    granularity: Literal["hour", "day", "week"] = "day",
) -> list[dict]:
    """Watchlist alerts per time period — the early-warning series."""
    log.info("tool_call", tool="watchlist_timeline", campaign_id=campaign_id, stub=STUB_MODE)
    return _handle_watchlist_timeline(campaign_id, from_date, to_date, granularity)


# ---------------------------------------------------------------------------
# Tool: agreement_stats
#
# Reads `comment_sentiments` — the PER-COMMENT table — because that is where the
# ensemble's verdict actually lands (stage2_llm/worker.py writes label_agreement,
# label_source and method per comment). `analysis_events` carries one averaged
# agreement figure per post, from which an abstention or single-voter rate cannot
# be recovered.
#
# Every field below is derived from a column. Nothing here is a constant: an
# audit tool that fills its gaps with plausible numbers audits nothing.
#   - unread     : nobody read the comment -> sentiment='uncertain', agreement 0
#   - abstained  : voters disagreed too much to claim a label -> 'uncertain', >0
#   - unanimous  : method='ensemble' (>=2 voters) and agreement ~1.0
#   - single     : exactly one labeller spoke, so method is that labeller's name
#   - propagated : label copied from a near-duplicate's representative
# ---------------------------------------------------------------------------


def _handle_agreement_stats(campaign_id: str | None = None) -> dict:
    cid = _clean_campaign_id(campaign_id)
    _log_scope("agreement_stats", cid)
    if STUB_MODE:
        return _stub_agreement_stats(cid)

    where = "WHERE 1=1"
    params: dict[str, Any] = {}
    if cid is not None:
        where += " AND campaign_id = %(campaign_id)s"
        params["campaign_id"] = cid

    sql = f"""
        SELECT
            count()                                                          AS comments,
            round(avg(label_agreement), 4)                                   AS mean_agreement,
            countIf(method = 'ensemble' AND label_agreement >= 0.999)        AS unanimous,
            countIf(sentiment = 'uncertain' AND label_agreement > 0)         AS abstained,
            countIf(sentiment = 'uncertain' AND label_agreement = 0)         AS unread,
            countIf(method = 'propagated')                                   AS propagated,
            countIf(
                method NOT IN ('ensemble', 'propagated')
                AND NOT (sentiment = 'uncertain' AND label_agreement = 0)
            )                                                                AS single_voter
        FROM comment_sentiments
        {where}
    """
    rows = _ch_query(sql, params)
    method_rows = _ch_query(
        f"SELECT method, count() AS n FROM comment_sentiments {where} GROUP BY method ORDER BY n DESC",
        params,
    )

    r = rows[0] if rows else {}
    comments = int(r.get("comments") or 0)
    denom = comments or 1

    def _share(key: str) -> float:
        return round(int(r.get(key) or 0) / denom, 4)

    # ClickHouse avg() over zero rows is NaN, and NaN survives `or 0.0` (it is
    # truthy) all the way into json.dumps, which emits a bare `NaN` the model
    # then has to parse as JSON. An empty corpus scores 0.0.
    mean_agreement = float(r.get("mean_agreement") or 0.0)
    if not math.isfinite(mean_agreement):
        mean_agreement = 0.0
    method_breakdown = {str(m.get("method") or "unknown"): int(m.get("n") or 0) for m in method_rows}

    return {
        "campaign_id": cid or "all",
        "comments": comments,
        "mean_agreement": mean_agreement,
        "unanimous": int(r.get("unanimous") or 0),
        "unanimous_share": _share("unanimous"),
        "abstained": int(r.get("abstained") or 0),
        "abstained_share": _share("abstained"),
        "single_voter": int(r.get("single_voter") or 0),
        "single_voter_share": _share("single_voter"),
        "unread": int(r.get("unread") or 0),
        "unread_share": _share("unread"),
        "propagated": int(r.get("propagated") or 0),
        "propagated_share": _share("propagated"),
        # Which labeller produced each comment's label: 'ensemble' (>=2 voters),
        # 'llm', a cheap classifier, 'emoji'/'fast' heuristics, or 'propagated'.
        "method_breakdown": method_breakdown,
        "quality_verdict": (
            "no_labelled_comments" if not comments
            else "high_confidence" if mean_agreement >= 0.7
            else "contested_labels"
        ),
        # unanimous_share is only meaningful next to single_voter_share: a corpus
        # one labeller carried alone has nothing to agree with.
        "notes": [
            "unanimous_share counts comments with >=2 labellers that all agreed; "
            "read it together with single_voter_share.",
        ],
        "is_stub": False,
    }


def _stub_agreement_stats(campaign_id: str | None = None) -> dict:
    """Synthetic agreement figures for ANALYTICS_MCP_STUB=true (dev mode).

    Flagged with ``is_stub`` so an agent auditing data quality cannot report
    these as measurements — which is the one thing a quality audit must not do.
    """
    return {
        "campaign_id": _clean_campaign_id(campaign_id) or "all",
        "comments": 1420,
        "mean_agreement": 0.835,
        "unanimous": 966,
        "unanimous_share": 0.68,
        "abstained": 64,
        "abstained_share": 0.045,
        "single_voter": 199,
        "single_voter_share": 0.14,
        "unread": 0,
        "unread_share": 0.0,
        "propagated": 191,
        "propagated_share": 0.135,
        "method_breakdown": {"ensemble": 966, "llm": 199, "propagated": 191, "fast": 64},
        "quality_verdict": "high_confidence",
        "notes": ["SYNTHETIC DATA — analytics-mcp is running in stub mode; these are not measurements."],
        "is_stub": True,
    }


@mcp.tool
def agreement_stats(
    campaign_id: Annotated[str | None, Field(description="Campaign to scope to; 'all' for every campaign.")] = None,
) -> dict:
    """Audit ensemble voter agreement over the per-comment labels: mean agreement,
    unanimous / abstained / single-voter / unread / propagated shares, and the
    method breakdown showing which labeller produced each label."""
    log.info("tool_call", tool="agreement_stats", campaign_id=campaign_id, stub=STUB_MODE)
    return _handle_agreement_stats(campaign_id)


@mcp.custom_route("/health", methods=["GET"])
async def health(_request: Request) -> JSONResponse:
    """Liveness probe — always returns 200 when the process is running."""
    return JSONResponse({"status": "ok", "service": "analytics-mcp", "stub_mode": STUB_MODE})


# ASGI app served by uvicorn: streamable-HTTP MCP endpoint mounted at /mcp.
app = mcp.http_app(path="/mcp")


if __name__ == "__main__":
    import uvicorn

    port = config.analytics_mcp_port
    log.info("analytics_mcp_starting", port=port, stub=STUB_MODE)
    uvicorn.run(app, host="0.0.0.0", port=port)

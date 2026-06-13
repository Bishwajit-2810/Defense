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

import os
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

STUB_MODE: bool = os.getenv("ANALYTICS_MCP_STUB", "false").lower() in ("true", "1", "yes")

CH_HOST = os.getenv("CLICKHOUSE_HOST", "localhost")
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


# ---------------------------------------------------------------------------
# Stub data generators
# ---------------------------------------------------------------------------

def _stub_date_series(from_date: str, to_date: str, granularity: str) -> list[str]:
    """Generate a list of ISO date strings for the stub data."""
    start = date.fromisoformat(from_date)
    end = date.fromisoformat(to_date)
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
    periods = _stub_date_series(from_date, to_date, granularity)
    random.seed(campaign_id)
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
    periods = _stub_date_series(from_date, to_date, granularity)
    random.seed(campaign_id + "sent")
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


def _stub_top_posts(campaign_id: str, metric: str, limit: int) -> list[dict]:
    random.seed(campaign_id + metric)
    return [
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


def _stub_reaction_mix(campaign_id: str) -> dict:
    random.seed(campaign_id + "react")
    raw = {k: random.randint(10, 5000) for k in ["LIKE", "LOVE", "HAHA", "WOW", "SAD", "ANGRY", "CARE"]}
    total = sum(raw.values()) or 1
    return {
        "totals": raw,
        "percentages": {k: round(v / total * 100, 2) for k, v in raw.items()},
        "total_reactions": total,
    }


# ---------------------------------------------------------------------------
# Tool handlers (real ClickHouse path)
# ---------------------------------------------------------------------------

def _handle_trend_query(
    campaign_id: str,
    from_date: str,
    to_date: str,
    granularity: str = "day",
    metric: str = "post_count",
) -> list[dict]:
    if STUB_MODE:
        return _stub_trend_query(campaign_id, from_date, to_date, granularity)

    interval = _interval(granularity)
    sql = f"""
        SELECT
            toStartOfInterval(created_at, INTERVAL {interval}) AS period,
            count()                                              AS count,
            avg(sentiment_score)                                 AS avg_sentiment,
            avg(toxicity_score)                                  AS avg_toxicity
        FROM analysis_events
        WHERE
            campaign_id = %(campaign_id)s
            AND created_at BETWEEN %(from_date)s AND %(to_date)s
        GROUP BY period
        ORDER BY period
    """
    rows = _ch_query(sql, {"campaign_id": campaign_id, "from_date": from_date, "to_date": to_date})
    # Serialize datetime objects returned by the driver
    for row in rows:
        if isinstance(row.get("period"), datetime):
            row["period"] = row["period"].isoformat()
    return rows


def _handle_sentiment_over_time(
    campaign_id: str,
    from_date: str,
    to_date: str,
    granularity: str = "day",
) -> list[dict]:
    if STUB_MODE:
        return _stub_sentiment_over_time(campaign_id, from_date, to_date, granularity)

    interval = _interval(granularity)
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
                overall_sentiment
            FROM analysis_events
            WHERE
                campaign_id = %(campaign_id)s
                AND created_at BETWEEN %(from_date)s AND %(to_date)s
        )
        GROUP BY period
        ORDER BY period
    """
    rows = _ch_query(sql, {"campaign_id": campaign_id, "from_date": from_date, "to_date": to_date})
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
) -> list[dict]:
    # Validate metric against allowlist to prevent SQL injection
    allowed_metrics = {"total_reactions", "comment_count", "toxicity_score", "hate_speech_score"}
    if metric not in allowed_metrics:
        raise ValueError(f"metric must be one of {sorted(allowed_metrics)}, got {metric!r}")

    limit = min(int(limit), 100)

    if STUB_MODE:
        return _stub_top_posts(campaign_id, metric, limit)

    date_filter = ""
    params: dict[str, Any] = {"campaign_id": campaign_id, "limit": limit}
    if from_date and to_date:
        date_filter = "AND created_at BETWEEN %(from_date)s AND %(to_date)s"
        params["from_date"] = from_date
        params["to_date"] = to_date
    elif from_date:
        date_filter = "AND created_at >= %(from_date)s"
        params["from_date"] = from_date
    elif to_date:
        date_filter = "AND created_at <= %(to_date)s"
        params["to_date"] = to_date

    sql = f"""
        SELECT
            post_id,
            total_reactions,
            comment_count,
            toxicity_score,
            hate_speech_score,
            overall_sentiment
        FROM analysis_events
        WHERE campaign_id = %(campaign_id)s
        {date_filter}
        ORDER BY {metric} DESC
        LIMIT %(limit)s
    """
    return _ch_query(sql, params)


def _handle_reaction_mix(
    campaign_id: str,
    from_date: str | None = None,
    to_date: str | None = None,
) -> dict:
    if STUB_MODE:
        return _stub_reaction_mix(campaign_id)

    date_filter = ""
    params: dict[str, Any] = {"campaign_id": campaign_id}
    if from_date and to_date:
        date_filter = "AND created_at BETWEEN %(from_date)s AND %(to_date)s"
        params["from_date"] = from_date
        params["to_date"] = to_date
    elif from_date:
        date_filter = "AND created_at >= %(from_date)s"
        params["from_date"] = from_date
    elif to_date:
        date_filter = "AND created_at <= %(to_date)s"
        params["to_date"] = to_date

    sql = f"""
        SELECT
            sum(like_count)    AS LIKE,
            sum(love_count)    AS LOVE,
            sum(haha_count)    AS HAHA,
            sum(wow_count)     AS WOW,
            sum(sad_count)     AS SAD,
            sum(angry_count)   AS ANGRY,
            sum(care_count)    AS CARE
        FROM reaction_events
        WHERE campaign_id = %(campaign_id)s
        {date_filter}
    """
    rows = _ch_query(sql, params)
    if not rows:
        return {"totals": {}, "percentages": {}, "total_reactions": 0}

    totals: dict[str, int] = {k: int(v or 0) for k, v in rows[0].items()}
    total = sum(totals.values()) or 1
    percentages = {k: round(v / total * 100, 2) for k, v in totals.items()}
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
    from_date: Annotated[str, Field(description="Inclusive start date (YYYY-MM-DD).")],
    to_date: Annotated[str, Field(description="Inclusive end date (YYYY-MM-DD).")],
    granularity: Literal["hour", "day", "week"] = "day",
    metric: Literal["post_count", "avg_sentiment", "avg_toxicity"] = "post_count",
) -> list[dict]:
    """Query analytics time-series for a campaign. Returns period buckets with
    post count, avg sentiment, and avg toxicity (all three are always returned)."""
    log.info("tool_call", tool="trend_query", campaign_id=campaign_id, stub=STUB_MODE)
    return _handle_trend_query(campaign_id, from_date, to_date, granularity, metric)


@mcp.tool
def sentiment_over_time(
    campaign_id: Annotated[str, Field(description="CUID/UUID of the campaign to query.")],
    from_date: Annotated[str, Field(description="Inclusive start date (YYYY-MM-DD).")],
    to_date: Annotated[str, Field(description="Inclusive end date (YYYY-MM-DD).")],
    granularity: Literal["hour", "day", "week"] = "day",
) -> list[dict]:
    """Returns sentiment breakdown (positive/negative/neutral/mixed counts) per
    time period for a campaign."""
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
) -> list[dict]:
    """Returns the top posts for a campaign ranked by the given metric (descending)."""
    log.info("tool_call", tool="top_posts", campaign_id=campaign_id, metric=metric, stub=STUB_MODE)
    return _handle_top_posts(campaign_id, metric, limit, from_date, to_date)


@mcp.tool
def reaction_mix(
    campaign_id: Annotated[str, Field(description="CUID/UUID of the campaign to query.")],
    from_date: Annotated[str | None, Field(description="Optional inclusive start date (YYYY-MM-DD).")] = None,
    to_date: Annotated[str | None, Field(description="Optional inclusive end date (YYYY-MM-DD).")] = None,
) -> dict:
    """Returns the aggregated reaction breakdown (LIKE/LOVE/HAHA/WOW/SAD/ANGRY/CARE)
    totals and percentages for a campaign."""
    log.info("tool_call", tool="reaction_mix", campaign_id=campaign_id, stub=STUB_MODE)
    return _handle_reaction_mix(campaign_id, from_date, to_date)


@mcp.custom_route("/health", methods=["GET"])
async def health(_request: Request) -> JSONResponse:
    """Liveness probe — always returns 200 when the process is running."""
    return JSONResponse({"status": "ok", "service": "analytics-mcp", "stub_mode": STUB_MODE})


# ASGI app served by uvicorn: streamable-HTTP MCP endpoint mounted at /mcp.
app = mcp.http_app(path="/mcp")


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", 8100))
    log.info("analytics_mcp_starting", port=port, stub=STUB_MODE)
    uvicorn.run(app, host="0.0.0.0", port=port)

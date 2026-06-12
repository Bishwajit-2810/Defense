"""
analytics-mcp  —  ClickHouse + Postgres analytics tools exposed as MCP / REST.

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
from typing import Any

import structlog
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from .manifest import TOOL_MANIFEST

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
# Tool dispatch table
# ---------------------------------------------------------------------------

_TOOL_HANDLERS = {
    "trend_query": _handle_trend_query,
    "sentiment_over_time": _handle_sentiment_over_time,
    "top_posts": _handle_top_posts,
    "reaction_mix": _handle_reaction_mix,
}

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(
    title="analytics-mcp",
    description="ClickHouse + Postgres analytics tools exposed as MCP / REST (ClusterIP-only).",
    version="1.0.0",
)


class ToolCall(BaseModel):
    tool_name: str = Field(..., description="Name of the MCP tool to invoke.")
    arguments: dict[str, Any] = Field(default_factory=dict, description="Tool arguments as key-value pairs.")


class ToolResult(BaseModel):
    tool_name: str
    result: Any = None
    error: str | None = None


@app.get("/mcp/manifest", summary="MCP tool manifest")
def get_manifest() -> dict:
    """
    Returns the full OpenAI-compatible tool manifest listing all available
    analytics tools with their JSON Schema parameter definitions.
    Agent orchestrators poll this endpoint to discover tools.
    """
    return {"tools": TOOL_MANIFEST}


@app.post("/tools/call", response_model=ToolResult, summary="Invoke an MCP tool")
async def call_tool(call: ToolCall) -> ToolResult:
    """
    Route a tool call to the appropriate handler and return the result.
    On handler error the response still uses HTTP 200 with a populated
    `error` field so that agent orchestrators can surface the error without
    treating it as a transport failure.
    """
    handler = _TOOL_HANDLERS.get(call.tool_name)
    if handler is None:
        known = sorted(_TOOL_HANDLERS.keys())
        raise HTTPException(
            status_code=404,
            detail=f"Unknown tool {call.tool_name!r}. Known tools: {known}",
        )

    log.info("tool_call", tool=call.tool_name, args=call.arguments, stub=STUB_MODE)

    try:
        result = handler(**call.arguments)
        log.info("tool_call_ok", tool=call.tool_name)
        return ToolResult(tool_name=call.tool_name, result=result)
    except TypeError as exc:
        # Missing / unexpected arguments
        log.warning("tool_call_bad_args", tool=call.tool_name, error=str(exc))
        return ToolResult(tool_name=call.tool_name, error=f"Invalid arguments: {exc}")
    except ValueError as exc:
        log.warning("tool_call_value_error", tool=call.tool_name, error=str(exc))
        return ToolResult(tool_name=call.tool_name, error=str(exc))
    except Exception as exc:
        log.error("tool_call_error", tool=call.tool_name, error=str(exc))
        return ToolResult(tool_name=call.tool_name, error=f"Backend error: {exc}")


@app.get("/health", summary="Health check")
def health() -> dict:
    """Liveness probe — always returns 200 when the process is running."""
    return {"status": "ok", "service": "analytics-mcp", "stub_mode": STUB_MODE}

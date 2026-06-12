"""Reports endpoints — generate, list, and retrieve analysis reports."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from datetime import datetime, timezone

# Repo root on path so `libs.llm` imports when the API runs from services/api.
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import redis.asyncio as aioredis
import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from deps import get_current_user, get_db, get_redis
from models import ReportRequest, ReportResponse

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/v1/reports", tags=["reports"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _get_report_row(db: AsyncSession, report_id: str) -> dict | None:
    """Fetch a report job row by ID; returns None when not found."""
    row = (
        await db.execute(
            text(
                """
                SELECT id, selector, options, status, created_at, updated_at, error
                FROM jobs
                WHERE id = :id AND type = 'report'
                """
            ),
            {"id": report_id},
        )
    ).mappings().first()
    return dict(row) if row else None


def _row_to_report(row: dict) -> ReportResponse:
    selector: dict = row.get("selector") or {}
    options: dict = row.get("options") or {}
    content: dict = options.get("result") or {}
    return ReportResponse(
        id=row["id"],
        report_id=row["id"],
        campaign_id=selector.get("campaign_id", ""),
        type=options.get("type"),
        title=options.get("title"),
        status=row["status"],
        created_at=row.get("created_at"),
        updated_at=row.get("updated_at"),
        download_url=options.get("download_url"),
        period=content.get("period"),
        summary=content.get("summary"),
        summary_source=content.get("summary_source"),
        clusters=content.get("clusters"),
        metrics=content.get("metrics"),
    )


async def _generate_report_content(db: AsyncSession, campaign_id: str) -> dict:
    """Aggregate stored analysis results into report content.

    Synchronous-at-request-time generation: the corpus is aggregated with a few
    SQL queries (cheap at MVP scale), so reports complete immediately instead
    of waiting on a queue consumer.
    """
    scoped = campaign_id not in ("", "all")
    where = "WHERE ar.campaign_id = :cid" if scoped else ""
    params: dict = {"cid": campaign_id} if scoped else {}

    # --- Headline metrics -----------------------------------------------
    head = (
        await db.execute(
            text(
                f"""
                SELECT count(*)                          AS total_posts,
                       min(ar.created_at)                AS first_at,
                       max(ar.created_at)                AS last_at
                FROM analysis_results ar {where}
                """
            ),
            params,
        )
    ).mappings().first()

    sentiments = (
        await db.execute(
            text(
                f"""
                SELECT ar.result->>'overall_sentiment' AS s, count(*) AS n
                FROM analysis_results ar {where}
                GROUP BY 1 ORDER BY 2 DESC
                """
            ),
            params,
        )
    ).mappings().all()

    languages = (
        await db.execute(
            text(
                f"""
                SELECT ar.result->>'language' AS lang, count(*) AS n
                FROM analysis_results ar {where}
                GROUP BY 1 ORDER BY 2 DESC LIMIT 10
                """
            ),
            params,
        )
    ).mappings().all()

    # --- Topic clusters: count + dominant sentiment per topic -------------
    topic_rows = (
        await db.execute(
            text(
                f"""
                SELECT t.topic                            AS topic,
                       ar.result->>'overall_sentiment'    AS s,
                       count(*)                           AS n
                FROM analysis_results ar,
                     jsonb_array_elements_text(ar.result->'topics') AS t(topic)
                {where}
                GROUP BY 1, 2
                """
            ),
            params,
        )
    ).mappings().all()

    by_topic: dict[str, dict] = {}
    for r in topic_rows:
        entry = by_topic.setdefault(r["topic"], {"size": 0, "sentiments": {}})
        entry["size"] += r["n"]
        entry["sentiments"][r["s"] or "neutral"] = r["n"]
    clusters = [
        {
            "cluster_id": f"topic-{i}",
            "label": topic,
            "size": data["size"],
            "top_sentiment": max(data["sentiments"], key=data["sentiments"].get),
        }
        for i, (topic, data) in enumerate(
            sorted(by_topic.items(), key=lambda kv: kv[1]["size"], reverse=True)[:8]
        )
    ]

    total = head["total_posts"] or 0
    sent_map = {r["s"] or "neutral": r["n"] for r in sentiments}
    lang_map = {r["lang"] or "und": r["n"] for r in languages}
    period = None
    if head["first_at"] and head["last_at"]:
        period = f"{head['first_at']:%Y-%m-%d} → {head['last_at']:%Y-%m-%d}"

    scope_label = f"campaign {campaign_id}" if scoped else "all campaigns"
    sent_text = ", ".join(f"{k}: {v}" for k, v in sent_map.items()) or "no data"
    top_topics = ", ".join(c["label"] for c in clusters[:5]) or "none"
    summary = (
        f"Analyzed {total} posts across {scope_label}"
        + (f" ({period})" if period else "")
        + f". Sentiment breakdown — {sent_text}. Top topics: {top_topics}."
    )

    return {
        "period": period,
        "summary": summary,
        "clusters": clusters,
        "metrics": {
            "total_posts": total,
            "sentiment_breakdown": sent_map,
            "languages": lang_map,
        },
    }


async def _llm_narrative(
    content: dict,
    campaign_id: str,
    report_type: str | None,
    backend_override: str | None,
) -> str | None:
    """Ask LLM-B for a grounded executive summary of the aggregated metrics.

    Returns None on any failure (no LLM backend reachable, timeout, …) so the
    caller can keep the SQL-aggregate summary instead.
    """
    try:
        from libs.llm.client import LLMClient  # noqa: PLC0415 — lazy: only grounded reports need it

        prompt = (
            "You are an analyst writing the executive summary of a social-media "
            f"monitoring report (type: {report_type or 'trend'}, scope: "
            f"{'campaign ' + campaign_id if campaign_id not in ('', 'all') else 'all campaigns'}).\n"
            "Below are the aggregated metrics computed from the analyzed posts. "
            "Write 3-5 factual, neutral sentences: overall volume, dominant "
            "sentiment and what drives it, the main topic clusters, and any "
            "notable language or engagement pattern. No markdown, no bullet "
            "points, no preamble.\n\n"
            f"Metrics JSON:\n{json.dumps(content, ensure_ascii=False, default=str)}"
        )
        response = await asyncio.wait_for(
            LLMClient().chat(
                role="llm_b",
                messages=[{"role": "user", "content": prompt}],
                backend_override=backend_override,
                max_tokens=400,
                temperature=0.2,
            ),
            timeout=30.0,
        )
        narrative = (response.get("content") or "").strip()
        return narrative or None
    except Exception as exc:
        log.warning("report_llm_summary_failed", error=str(exc))
        return None


# ---------------------------------------------------------------------------
# GET /v1/reports
# ---------------------------------------------------------------------------


@router.get(
    "",
    response_model=list[ReportResponse],
    summary="List reports for the current user",
)
async def list_reports(
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> list[ReportResponse]:
    """Return all report jobs ordered by creation time (newest first)."""
    rows = (
        await db.execute(
            text(
                """
                SELECT id, selector, options, status, created_at, updated_at, error
                FROM jobs
                WHERE type = 'report'
                ORDER BY created_at DESC
                LIMIT 100
                """
            )
        )
    ).mappings().all()

    return [_row_to_report(dict(r)) for r in rows]


# ---------------------------------------------------------------------------
# POST /v1/reports
# ---------------------------------------------------------------------------


@router.post(
    "",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=ReportResponse,
    summary="Request an async report generation",
)
async def create_report(
    body: ReportRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
    current_user: dict = Depends(get_current_user),
) -> ReportResponse:
    """Enqueue report generation for *campaign_id*.

    The report is generated asynchronously by the report worker.  Poll
    ``GET /v1/reports/{id}`` to check for completion.
    """
    report_id = str(uuid.uuid4())
    now = datetime.now(tz=timezone.utc)

    # Corpus-wide reports (the dashboard) omit campaign_id → default to "all".
    campaign_id = body.campaign_id or "all"

    selector = {
        "campaign_id": campaign_id,
        "filters": body.filters.model_dump(exclude_none=True) if body.filters else {},
    }
    options = {
        "title": body.title,
        "type": body.type,
        "include_sentiment": body.include_sentiment,
        "include_engagement": body.include_engagement,
        "include_topics": body.include_topics,
        "requested_by": current_user.get("sub"),
        **(body.options or {}),
    }

    # Generate the report content inline — the aggregation is a handful of SQL
    # queries, so reports complete at request time (no queue consumer needed).
    try:
        content = await _generate_report_content(db, campaign_id)
        content["summary_source"] = "aggregate"

        # Grounded reports (default) get an LLM-B narrative on top of the
        # SQL aggregates; the aggregate one-liner is kept as a fallback and
        # for audit. Honour the runtime backend override the dashboard sets.
        if bool((body.options or {}).get("grounded", True)):
            backend_override = None
            try:
                raw_override = await redis.get("config:llm_backend")
                if raw_override in ("local", "groq"):
                    backend_override = raw_override
            except Exception:
                pass
            narrative = await _llm_narrative(content, campaign_id, body.type, backend_override)
            if narrative:
                content["aggregate_summary"] = content["summary"]
                content["summary"] = narrative
                content["summary_source"] = "llm"

        options["result"] = content
        await db.execute(
            text(
                """
                INSERT INTO jobs (id, type, status, selector, options, created_at, updated_at)
                VALUES (:id, :type, :status, CAST(:selector AS jsonb), CAST(:options AS jsonb), :created_at, :updated_at)
                """
            ),
            {
                "id": report_id,
                "type": "report",
                "status": "done",
                "selector": json.dumps(selector, default=str),
                "options": json.dumps(options, default=str),
                "created_at": now,
                "updated_at": now,
            },
        )
    except Exception as exc:
        log.error("report_create_failed", report_id=report_id, error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to generate report",
        ) from exc

    log.info("report_generated", report_id=report_id, campaign_id=campaign_id)
    return ReportResponse(
        id=report_id,
        report_id=report_id,
        campaign_id=campaign_id,
        type=body.type,
        title=body.title,
        status="done",
        created_at=now,
        updated_at=now,
        period=content.get("period"),
        summary=content.get("summary"),
        summary_source=content.get("summary_source"),
        clusters=content.get("clusters"),
        metrics=content.get("metrics"),
    )


# ---------------------------------------------------------------------------
# GET /v1/reports/{id}
# ---------------------------------------------------------------------------


@router.get(
    "/{report_id}",
    response_model=ReportResponse,
    summary="Get a report by ID",
)
async def get_report(
    report_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> ReportResponse:
    """Return the current status and metadata for *report_id*."""
    row = await _get_report_row(db, report_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Report '{report_id}' not found",
        )
    return _row_to_report(row)

"""Analysis endpoints — run analysis jobs and stream progress."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from typing import Any, AsyncGenerator

# Repo root on path so `services.ingestion.normalizer` imports when the API
# runs from services/api (no-op when PYTHONPATH already provides it).
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

import redis.asyncio as aioredis
from libs.labels import label_provenance
from libs.progress import publish_stage, replay_events
from deps import check_llm_backend_policy, get_current_user, get_db, get_redis, rate_limit
from models import (
    AnalysisDetailResponse,
    AnalysisResultResponse,
    AnalysisRunRequest,
    AnalysisRunResponse,
    CorpusCoverage,
    LabelCount,
    LlmPanel,
    OverviewResponse,
)

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/v1/analysis", tags=["analysis"])

# Job statuses that are final; anything else is reconcilable from the counters.
_TERMINAL_JOB_STATUSES = frozenset({"done", "failed"})

# Re-analysis jobs feed the same Stage-1 stream the ingestion service uses —
# there is no separate analysis worker; the normal pipeline does the work and
# the assembler flips the job to done via job_id.
_NLP_STAGE1_STREAM = "nlp:stage1:queue"


async def _create_analysis_job(
    db: AsyncSession,
    analysis_id: str,
    selector: dict,
) -> None:
    now = datetime.now(tz=timezone.utc)
    await db.execute(
        text(
            """
            INSERT INTO jobs (id, type, status, selector, options, created_at, updated_at)
            VALUES (:id, :type, :status, CAST(:selector AS jsonb), CAST(:options AS jsonb), :created_at, :updated_at)
            ON CONFLICT (id) DO NOTHING
            """
        ),
        {
            "id": analysis_id,
            "type": "analysis_run",
            "status": "queued",
            "selector": json.dumps(selector, default=str),
            "options": json.dumps({}),
            "created_at": now,
            "updated_at": now,
        },
    )


# ---------------------------------------------------------------------------
# POST /v1/analysis/run
# ---------------------------------------------------------------------------


@router.post(
    "/run",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=AnalysisRunResponse,
    summary="Enqueue an analysis job",
    dependencies=[Depends(rate_limit)],
)
async def analysis_run(
    body: AnalysisRunRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
    current_user: dict = Depends(get_current_user),
) -> AnalysisRunResponse:
    """Enqueue NLP/LLM (re-)analysis for the specified posts or campaign.

    Matched posts are re-normalized from their stored ``raw_payload`` and fed
    back into the Stage-1 stream with this job's id; the assembler marks the
    job done as results land. Poll ``GET /v1/analysis/{id}`` or connect to the
    SSE stream at ``GET /v1/analysis/{id}/stream`` for progress.
    """
    if not body.post_ids and not body.campaign_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Provide at least one of 'post_ids' or 'campaign_id'",
        )

    options: dict[str, Any] = body.options or {}
    # Privacy-locked tenants may not override the backend to groq (403).
    await check_llm_backend_policy(db, current_user, options)

    analysis_id = str(uuid.uuid4())

    selector: dict[str, Any] = {}
    if body.campaign_id:
        selector["campaign_id"] = body.campaign_id
    if body.post_ids:
        selector["post_ids"] = body.post_ids
    if body.filter:
        selector["filter"] = body.filter.model_dump(exclude_none=True)

    # --- Find the posts to (re-)analyze -------------------------------------
    where, params = [], {}
    if body.campaign_id:
        where.append("campaign_id = :campaign_id")
        params["campaign_id"] = body.campaign_id
    if body.post_ids:
        where.append("id = ANY(:post_ids)")
        params["post_ids"] = body.post_ids
    rows = (
        await db.execute(
            text(f"SELECT id, raw_payload FROM posts WHERE {' AND '.join(where)}"),
            params,
        )
    ).mappings().all()

    if not rows:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No posts matched the selector — upload posts first",
        )

    # Re-normalize from the stored raw payload (same path ingestion uses).
    from services.ingestion.normalizer import normalize_post  # noqa: PLC0415

    try:
        await _create_analysis_job(db, analysis_id, selector)
        enqueued = 0
        for row in rows:
            raw = row["raw_payload"] or {}
            try:
                normalized = normalize_post(raw)
            except Exception as exc:
                log.warning("analysis_run_normalize_failed", post_id=row["id"], error=str(exc))
                continue
            envelope = {
                "post_id": normalized["post_id"],
                "raw_post": raw,
                "normalized_post": normalized,
                "options": options,
                "job_id": analysis_id,
            }
            await redis.xadd(
                _NLP_STAGE1_STREAM,
                {"data": json.dumps(envelope, ensure_ascii=False, default=str)},
            )
            enqueued += 1

            # Opening frame for the dashboard's Trace tab. This path re-enqueues
            # directly rather than going through the ingestion worker, so it has
            # to report the ingest layer itself.
            eng = normalized.get("engagement") or {}
            await publish_stage(
                redis,
                "ingest",
                "done",
                job_id=analysis_id,
                post_id=normalized["post_id"],
                detail={
                    "source": "re-normalized from stored raw_payload",
                    "platform": normalized.get("platform"),
                    "media_type": normalized.get("media_type"),
                    "caption_chars": len(normalized.get("caption") or ""),
                    "photo_count": len(normalized.get("photo_urls") or []),
                    "comment_rows": len(normalized.get("comments") or []),
                    "comment_count": eng.get("comment_count"),
                    "coverage": eng.get("coverage"),
                    "content_hash": (normalized.get("content_hash") or "")[:16],
                    "baseline_sentiment": normalized.get("baseline_sentiment"),
                    "options": options,
                    "next_stream": _NLP_STAGE1_STREAM,
                },
                log=log,
            )
        if enqueued == 0:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="All matched posts failed normalization",
            )
    except HTTPException:
        raise
    except Exception as exc:
        log.error("analysis_run_enqueue_failed", analysis_id=analysis_id, error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to enqueue analysis job",
        ) from exc

    # Record the expected post count so the assembler can report progress and
    # mark the job done only when every post has landed.
    try:
        await redis.set(f"job:{analysis_id}:total", enqueued, ex=86_400)
    except Exception as exc:
        log.warning("analysis_run_total_record_failed", analysis_id=analysis_id, error=str(exc))

    # Report the router's OBSERVED routing rate. Before any traffic there is no
    # rate, so this stays None rather than repeating the 5% design target as if
    # it were a measurement (the docs asserting that target while the gate routed
    # 100% is exactly the defect PROJECT_ASSESSMENT.md §4 describes).
    estimated_share: float | None = None
    try:
        total_stat = int(await redis.get("stats:total_processed") or 0)
        llm_stat = int(await redis.get("stats:llm_routed") or 0)
        if total_stat > 0:
            estimated_share = round(llm_stat / total_stat, 4)
    except Exception:
        pass
    if options.get("want_summary"):
        estimated_share = 1.0  # summaries force every post through Stage-2

    log.info("analysis_run_enqueued", analysis_id=analysis_id, posts=enqueued)
    base = str(request.base_url).rstrip("/")
    return AnalysisRunResponse(
        analysis_id=analysis_id,
        status="queued",
        estimated_llm_share=estimated_share,
        status_url=f"{base}/v1/analysis/{analysis_id}",
    )


# ---------------------------------------------------------------------------
# GET /v1/analysis/{id}
# ---------------------------------------------------------------------------


@router.get(
    "",
    summary="List recent analysis/ingest jobs",
)
async def list_analysis_jobs(
    limit: int = Query(20, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> dict:
    """List recent jobs (newest first), excluding reports. Backs the Jobs tab."""
    rows = (
        await db.execute(
            text(
                """
                SELECT id, type, status, selector, created_at, updated_at
                FROM jobs
                WHERE type <> 'report'
                ORDER BY created_at DESC
                LIMIT :limit
                """
            ),
            {"limit": limit},
        )
    ).mappings().all()

    jobs = []
    for r in rows:
        selector = r["selector"] or {}
        post_ids = selector.get("post_ids") or []
        jobs.append(
            {
                "id": r["id"],
                "type": r["type"],
                "status": r["status"],
                "post_count": len(post_ids) or None,
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
            }
        )
    return {"jobs": jobs, "total": len(jobs)}


@router.get(
    "/overview",
    response_model=OverviewResponse,
    summary="Corpus-level aggregates for the Overview tab (all computed server-side)",
)
async def analysis_overview(
    campaign_id: str | None = Query(None, description="Optional campaign filter"),
    top: int = Query(8, ge=1, le=50, description="Max entries in the topic/language/emotion lists"),
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> OverviewResponse:
    """Aggregate the whole corpus in SQL so the dashboard only renders.

    Everything is derived from ``analysis_results`` JSONB (same source the
    Posts tab reads), so no client-side counting is needed. Declared before
    ``/{analysis_id}`` so the literal ``/overview`` path wins.
    """
    where = ""
    params: dict[str, Any] = {"top": top}
    if campaign_id:
        where = "WHERE campaign_id = :campaign_id"
        params["campaign_id"] = campaign_id

    async def rows(sql: str) -> list:
        return (await db.execute(text(sql), params)).mappings().all()

    # --- Totals + LLM panel (one pass) --------------------------------------
    panel_row = (
        await db.execute(
            text(
                f"""
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN result->'processing'->>'llm_used' = 'true'
                             OR result->>'llm_used' = 'true' THEN 1 ELSE 0 END) AS llm_used,
                    SUM(CASE WHEN COALESCE(result->>'post_summary', '') <> '' THEN 1 ELSE 0 END) AS with_summary
                FROM analysis_results
                {where}
                """
            ),
            params,
        )
    ).mappings().first()
    total_posts = int(panel_row["total"] or 0)

    # --- Sentiment distribution (fixed taxonomy) ----------------------------
    sentiment_distribution = {"positive": 0, "negative": 0, "neutral": 0, "mixed": 0}
    for r in await rows(
        f"SELECT result->>'overall_sentiment' AS k, COUNT(*) AS c "
        f"FROM analysis_results {where} GROUP BY k"
    ):
        key = r["k"] or "neutral"
        if key in sentiment_distribution:
            sentiment_distribution[key] += int(r["c"])

    # --- Language distribution ----------------------------------------------
    language_distribution = [
        LabelCount(label=r["k"] or "und", count=int(r["c"]))
        for r in await rows(
            f"SELECT result->>'language' AS k, COUNT(*) AS c "
            f"FROM analysis_results {where} GROUP BY k ORDER BY c DESC LIMIT :top"
        )
    ]

    # --- Top topics (unnest the topics array) -------------------------------
    # The comma-join puts analysis_results first, so the campaign filter must be
    # a fresh WHERE clause here (not appended to the scalar `where`).
    topic_filter = "WHERE campaign_id = :campaign_id" if campaign_id else ""
    top_topics = [
        LabelCount(label=r["k"], count=int(r["c"]))
        for r in await rows(
            f"SELECT topic AS k, COUNT(*) AS c "
            f"FROM analysis_results, jsonb_array_elements_text(result->'topics') AS topic "
            f"{topic_filter} GROUP BY topic ORDER BY c DESC LIMIT :top"
        )
    ]

    # --- Post-level dominant emotion ----------------------------------------
    emotion_distribution = [
        LabelCount(label=r["k"], count=int(r["c"]))
        for r in await rows(
            f"SELECT result->'emotion'->>'primary' AS k, COUNT(*) AS c "
            f"FROM analysis_results {where} "
            f"{'AND' if where else 'WHERE'} result->'emotion'->>'primary' IS NOT NULL "
            f"GROUP BY k ORDER BY c DESC LIMIT :top"
        )
    ]

    # --- Per-comment emotion summed across posts (new emotion_breakdown) -----
    comment_emotion_distribution = [
        LabelCount(label=r["k"], count=int(r["c"]))
        for r in await rows(
            f"SELECT key AS k, SUM(value::int) AS c "
            f"FROM analysis_results, "
            f"jsonb_each_text(result->'comment_analysis'->'emotion_breakdown') "
            f"{topic_filter} GROUP BY key ORDER BY c DESC"
        )
    ]

    # --- Backends seen ------------------------------------------------------
    backends_seen = [
        LabelCount(label=r["k"], count=int(r["c"]))
        for r in await rows(
            f"SELECT result->'processing'->>'llm_backend' AS k, COUNT(*) AS c "
            f"FROM analysis_results {where} "
            f"{'AND' if where else 'WHERE'} result->'processing'->>'llm_backend' IS NOT NULL "
            f"GROUP BY k ORDER BY c DESC"
        )
    ]

    # --- Corpus-level comment coverage --------------------------------------
    # The per-post coverage the dashboard shows has a median of 26.7%, but the
    # aggregate — 10,272 stored against 274,126 reported by the platform, i.e.
    # 3.75% — appeared nowhere. It is the number that bounds what any
    # thread-level sentiment claim can support, so it is reported here next to
    # the distributions it qualifies.
    cov_row = (
        await db.execute(
            text(
                f"""
                SELECT
                    SUM(COALESCE((result->'comment_analysis'->>'analyzed')::bigint, 0))
                        AS analyzed,
                    SUM(COALESCE((result->'engagement'->>'comment_count')::bigint, 0))
                        AS reported,
                    SUM(CASE WHEN result->'comment_analysis'->'coverage_anomaly'
                             IS NOT NULL THEN 1 ELSE 0 END) AS anomalies
                FROM analysis_results
                {where}
                """
            ),
            params,
        )
    ).mappings().first()
    analyzed_total = int((cov_row or {}).get("analyzed") or 0)
    reported_total = int((cov_row or {}).get("reported") or 0)
    corpus_coverage = CorpusCoverage(
        analyzed=analyzed_total,
        reported=reported_total,
        coverage=round(analyzed_total / reported_total, 4) if reported_total else 0.0,
        posts_with_anomaly=int((cov_row or {}).get("anomalies") or 0),
    )

    return OverviewResponse(
        total_posts=total_posts,
        campaign_id=campaign_id,
        corpus_coverage=corpus_coverage,
        sentiment_distribution=sentiment_distribution,
        language_distribution=language_distribution,
        top_topics=top_topics,
        emotion_distribution=emotion_distribution,
        comment_emotion_distribution=comment_emotion_distribution,
        llm_panel=LlmPanel(
            total_posts=total_posts,
            posts_with_llm=int(panel_row["llm_used"] or 0),
            posts_with_summaries=int(panel_row["with_summary"] or 0),
            backends_seen=backends_seen,
        ),
    )


@router.get(
    "/latest",
    response_model=AnalysisDetailResponse,
    summary="List the most recent analysis results across all jobs",
)
async def latest_analysis(
    limit: int = Query(50, ge=1, le=500),
    campaign_id: str | None = Query(None, description="Optional campaign filter"),
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> AnalysisDetailResponse:
    """Return the most recent analysis results (newest first). Backs the Posts tab.

    Always embeds results; the dashboard's ``?include=results`` is accepted and
    ignored. Declared before ``/{analysis_id}`` so the literal ``/latest`` wins.
    """
    params: dict[str, Any] = {"limit": limit}
    where = ""
    if campaign_id:
        where = "WHERE ar.campaign_id = :campaign_id"
        params["campaign_id"] = campaign_id

    rows = (
        await db.execute(
            text(
                f"""
                SELECT ar.id, ar.post_id, ar.campaign_id, ar.result,
                       ar.created_at, p.scraped_at
                FROM analysis_results ar
                JOIN posts p ON p.id = ar.post_id
                {where}
                ORDER BY ar.created_at DESC
                LIMIT :limit
                """
            ),
            params,
        )
    ).mappings().all()

    return AnalysisDetailResponse(
        analysis_id="latest",
        status="done",
        campaign_id=campaign_id,
        post_ids=[],
        results=[_row_to_result(r) for r in rows],
        created_at=rows[0]["created_at"] if rows else None,
    )


@router.get(
    "/{analysis_id}",
    response_model=AnalysisDetailResponse,
    summary="Get analysis job status and results",
)
async def get_analysis(
    analysis_id: str,
    include: str | None = Query(None, description="Comma-separated includes: results"),
    limit: int = Query(100, ge=1, le=1000),
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
    current_user: dict = Depends(get_current_user),
) -> AnalysisDetailResponse:
    """Return the status of an analysis job.

    Pass ``?include=results`` to embed analysis result rows in the response.
    Use ``?limit=N`` to cap the number of result rows returned.
    """
    job_row = (
        await db.execute(
            text("SELECT id, status, selector, created_at FROM jobs WHERE id = :id"),
            {"id": analysis_id},
        )
    ).mappings().first()

    if job_row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Analysis job '{analysis_id}' not found",
        )

    selector: dict = job_row["selector"] or {}
    campaign_id: str | None = selector.get("campaign_id")
    post_ids: list[str] = selector.get("post_ids") or []

    # Per-job progress counters maintained by the assembler + the dead-letter
    # path (libs/dlq counts a DLQ'd post against the job, which is what lets a
    # job whose posts died before the assembler still finish).
    job_status: str = job_row["status"]
    progress: dict[str, Any] | None = None
    try:
        raw_total = await redis.get(f"job:{analysis_id}:total")
        completed = int(await redis.get(f"job:{analysis_id}:completed") or 0)
        failed = int(await redis.get(f"job:{analysis_id}:failed") or 0)
        if raw_total is not None or completed or failed:
            total = int(raw_total) if raw_total is not None else None
            progress = {
                "total": total,
                "completed": completed,
                "failed": failed,
            }
            # Reconcile a stale row: only the assembler writes the terminal
            # status, so a job whose LAST post was dead-lettered leaves the row
            # sitting at "running" even though the counters are complete.
            if (
                total is not None
                and (completed + failed) >= total
                and job_status not in _TERMINAL_JOB_STATUSES
            ):
                job_status = "done" if completed > 0 else "failed"
                await db.execute(
                    text(
                        "UPDATE jobs SET status = :s, updated_at = NOW() "
                        "WHERE id = :id AND status NOT IN ('done', 'failed')"
                    ),
                    {"s": job_status, "id": analysis_id},
                )
                log.info(
                    "job_status_reconciled_from_counters",
                    analysis_id=analysis_id,
                    status=job_status,
                    completed=completed,
                    failed=failed,
                    total=total,
                )
    except Exception as exc:
        log.warning("job_progress_read_failed", analysis_id=analysis_id, error=str(exc))

    results: list[AnalysisResultResponse] | None = None
    wants_results = include and "results" in include.split(",")

    if wants_results:
        # Build a query against analysis_results joined with posts
        if campaign_id:
            rows = (
                await db.execute(
                    text(
                        """
                        SELECT ar.id, ar.post_id, ar.campaign_id, ar.result,
                               ar.created_at, p.scraped_at
                        FROM analysis_results ar
                        JOIN posts p ON p.id = ar.post_id
                        WHERE ar.campaign_id = :campaign_id
                        ORDER BY ar.created_at DESC
                        LIMIT :limit
                        """
                    ),
                    {"campaign_id": campaign_id, "limit": limit},
                )
            ).mappings().all()
        elif post_ids:
            rows = (
                await db.execute(
                    text(
                        """
                        SELECT ar.id, ar.post_id, ar.campaign_id, ar.result,
                               ar.created_at, p.scraped_at
                        FROM analysis_results ar
                        JOIN posts p ON p.id = ar.post_id
                        WHERE ar.post_id = ANY(:post_ids)
                        ORDER BY ar.created_at DESC
                        LIMIT :limit
                        """
                    ),
                    {"post_ids": post_ids, "limit": limit},
                )
            ).mappings().all()
        else:
            rows = []

        results = [_row_to_result(r) for r in rows]

    return AnalysisDetailResponse(
        analysis_id=analysis_id,
        # Reconciled above when the counters are complete but the row is stale.
        status=job_status,
        campaign_id=campaign_id,
        post_ids=post_ids,
        results=results,
        created_at=job_row["created_at"],
        progress=progress,
    )


@router.get(
    "/post/{post_id}/comments",
    summary="Paginated per-comment sentiment for one post (full coverage)",
)
async def get_post_comments(
    post_id: str,
    limit: int = Query(200, ge=1, le=2000),
    offset: int = Query(0, ge=0),
    sentiment: str = Query("all", description="all | positive | negative | neutral"),
    emotion: str = Query("all", description="all | anger | sadness | joy | fear | disgust | surprise | neutral"),
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> dict:
    """Return every analysed comment for a post, paginated.

    Reads the per-comment list from the canonical result (analysis_results JSONB)
    so no extra datastore is needed. ``sentiment`` and ``emotion`` filter the
    returned slice; the breakdown counts always reflect the full set.
    """
    row = (
        await db.execute(
            text("SELECT result FROM analysis_results WHERE post_id = :pid"),
            {"pid": post_id},
        )
    ).mappings().first()

    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No analysis result for post '{post_id}'",
        )

    ca: dict = (row["result"] or {}).get("comment_analysis") or {}
    all_comments: list[dict] = ca.get("comments") or []

    filtered = all_comments
    if sentiment != "all":
        filtered = [c for c in filtered if (c.get("sentiment") or "neutral") == sentiment]
    if emotion != "all":
        filtered = [c for c in filtered if (c.get("emotion") or "neutral") == emotion]

    page = filtered[offset : offset + limit]

    # ---- Aggregates over the FULL comment set (not just the page) ----------
    # 10 buckets across the sentiment-score range [-1, 1].
    histogram = [0] * 10
    author_stats: dict[str, dict] = {}
    score_sum = 0.0
    for c in all_comments:
        score = float(c.get("sentiment_score") or 0.0)
        score_sum += score
        idx = min(9, max(0, int((score + 1.0) / 2.0 * 10)))
        histogram[idx] += 1
        author = c.get("author") or "—"
        a = author_stats.setdefault(author, {"author": author, "likes": 0, "count": 0})
        a["likes"] += int(c.get("likes") or 0)
        a["count"] += 1

    top_authors = sorted(
        author_stats.values(), key=lambda a: (-a["likes"], -a["count"])
    )[:8]
    top_liked = sorted(
        all_comments, key=lambda c: int(c.get("likes") or 0), reverse=True
    )[:6]
    avg_score = round(score_sum / len(all_comments), 3) if all_comments else 0.0

    return {
        "post_id": post_id,
        "total": len(all_comments),
        "filtered_total": len(filtered),
        "offset": offset,
        "returned": len(page),
        "summary": ca.get("summary"),
        "summary_source": ca.get("summary_source"),
        "sentiment_breakdown": ca.get("sentiment_breakdown", {}),
        "sentiment_breakdown_substantive": ca.get("sentiment_breakdown_substantive", {}),
        "reaction_only": ca.get("reaction_only", 0),
        "emotion_breakdown": ca.get("emotion_breakdown", {}),
        "method_breakdown": ca.get("method_breakdown", {}),
        # Recomputed from the stored breakdown rather than trusting a persisted
        # `provenance` block, so rows written before the field existed still
        # report an honest mix instead of an empty one.
        "provenance": label_provenance(ca.get("method_breakdown") or {}),
        "coverage": ca.get("coverage", 0.0),
        "coverage_label": _coverage_label(ca, (row["result"] or {}).get("engagement") or {}),
        "avg_sentiment_score": avg_score,
        "score_histogram": histogram,
        "top_authors": top_authors,
        "top_liked": top_liked,
        "comments": page,
    }


def _coverage_label(ca: dict, engagement: dict) -> str:
    """Human-readable comment-coverage string, computed server-side.

    Mirrors what the dashboard used to build in JS: every stored comment is
    classified, so "✓ all" means we analysed everything the upstream shipped;
    the "% of N" tail is the honest fraction of the platform's reported total
    that we ever received.
    """
    analyzed = int(ca.get("analyzed") or 0)
    stored = int((engagement or {}).get("stored_comments") or 0)
    total = int((engagement or {}).get("comment_count") or 0)
    if not analyzed and not stored and not total:
        return "—"
    full = analyzed >= stored if stored > 0 else True
    head = ("✓ all " if full else "") + f"{analyzed} analyzed"
    if total > 0 and total > analyzed:
        head += f" · {round(analyzed / total * 100)}% of {total}"
    elif total > 0 and analyzed > total:
        # Storing more comments than the platform says exist is an upstream
        # inconsistency. The old label just omitted the tail, which read as
        # "we have everything" — say what actually happened instead.
        head += f" · more than the {total} reported (upstream mismatch)"
    return head


def _comment_analysis_summary(r: dict) -> dict:
    """comment_analysis for list/detail responses: drop the bulky per-comment
    list and attach the server-computed coverage_label."""
    ca = dict(
        r.get(
            "comment_analysis",
            {"analyzed": 0, "coverage": 0.0, "sentiment_breakdown": {}},
        )
    )
    ca.pop("comments", None)
    ca["coverage_label"] = _coverage_label(ca, r.get("engagement") or {})
    return ca


def _row_to_result(row: Any) -> AnalysisResultResponse:
    """Map a DB row (with embedded JSONB result) to AnalysisResultResponse."""
    r: dict = row["result"]
    return AnalysisResultResponse(
        id=row["id"],
        post_id=row["post_id"],
        campaign_id=row["campaign_id"] or r.get("campaign_id", ""),
        platform=r.get("platform", ""),
        platform_post_id=r.get("platform_post_id", ""),
        media_type=r.get("media_type", "TEXT"),
        language=r.get("language", ""),
        post_text=r.get("post_text"),
        post_type=r.get("post_type"),
        post_summary=r.get("post_summary"),
        post_summary_lang=r.get("post_summary_lang"),
        post_summary_source=r.get("post_summary_source"),
        post_summary_grounding=r.get("post_summary_grounding"),
        post_summary_truncated=r.get("post_summary_truncated"),
        overall_sentiment=r.get("overall_sentiment", "neutral"),
        sentiment_score=r.get("sentiment_score", 0.0),
        text_sentiment=r.get("text_sentiment"),
        image_sentiment=r.get("image_sentiment"),
        baseline_sentiment=r.get("baseline_sentiment"),
        emotion=r.get("emotion"),
        intents=r.get("intents", []),
        topics=r.get("topics", []),
        entities=r.get("entities", []),
        brand_mentions=r.get("brand_mentions", []),
        keywords=r.get("keywords", []),
        toxicity_score=r.get("toxicity_score"),
        hate_speech_score=r.get("hate_speech_score"),
        engagement=r.get("engagement", {}),
        reaction_breakdown=r.get("reaction_breakdown"),
        image_analysis=r.get("image_analysis"),
        # Strip the (potentially large) per-comment list here — list and job
        # responses stay lean. The Details modal lazy-loads the full per-comment
        # sentiment via GET /v1/analysis/post/{post_id}/comments. coverage_label
        # is computed server-side so the frontend just displays it.
        comment_analysis=_comment_analysis_summary(r),
        confidence=r.get(
            "confidence",
            {"overall": 0.0, "sentiment": 0.0, "language": 0.0, "topics": 0.0},
        ),
        processing=r.get("processing", {"llm_used": False, "schema_version": "1.0"}),
        created_at=row["created_at"],
        scraped_at=row.get("scraped_at") or row["created_at"],
    )


# ---------------------------------------------------------------------------
# GET /v1/analysis/{id}/stream  — SSE
# ---------------------------------------------------------------------------


async def _sse_generator(
    analysis_id: str,
    redis: aioredis.Redis,
) -> AsyncGenerator[str, None]:
    """Yield Server-Sent Events from the Redis pub/sub channel for the job."""
    channel = f"analysis:progress:{analysis_id}"
    pubsub = redis.pubsub()
    await pubsub.subscribe(channel)

    try:
        # Send an initial "connected" event so the client knows the stream is live
        yield f"event: connected\ndata: {json.dumps({'analysis_id': analysis_id})}\n\n"

        # Replay any stage events already published for this job. The first one
        # (ingestion) is emitted while POST /v1/analysis/run is still running, so
        # a client can never subscribe in time to see it live. Replaying after
        # the pubsub subscribe above means no frame can fall between the two.
        # Each frame carries a `seq`, so a client can drop a duplicate.
        for buffered in await replay_events(redis, analysis_id):
            buffered["replay"] = True
            yield f"event: {buffered.get('event', 'stage')}\ndata: {json.dumps(buffered, ensure_ascii=False)}\n\n"

        timeout_seconds = 300  # 5-minute max stream duration
        deadline = asyncio.get_event_loop().time() + timeout_seconds

        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                yield f"event: timeout\ndata: {json.dumps({'analysis_id': analysis_id})}\n\n"
                break

            message = await asyncio.wait_for(
                pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0),
                timeout=min(2.0, remaining),
            )

            if message and message.get("type") == "message":
                raw = message.get("data", "")
                try:
                    payload = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    payload = {"raw": raw}

                event_type = payload.get("event", "progress")
                yield f"event: {event_type}\ndata: {json.dumps(payload)}\n\n"

                if event_type in ("done", "error"):
                    break
            else:
                # Keepalive comment so the client connection stays open
                yield ": keepalive\n\n"

    except asyncio.TimeoutError:
        yield f"event: timeout\ndata: {json.dumps({'analysis_id': analysis_id})}\n\n"
    finally:
        await pubsub.unsubscribe(channel)
        await pubsub.close()


@router.get(
    "/{analysis_id}/stream",
    summary="Stream analysis progress via SSE",
    response_class=StreamingResponse,
)
async def analysis_stream(
    analysis_id: str,
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
    current_user: dict = Depends(get_current_user),
) -> StreamingResponse:
    """Subscribe to Server-Sent Events for *analysis_id*.

    Events are published by the worker onto the Redis channel
    ``analysis:progress:{analysis_id}``.  The stream closes automatically
    on a ``done`` or ``error`` event, or after 5 minutes.
    """
    # Verify job exists before opening the stream
    job_row = (
        await db.execute(
            text("SELECT id FROM jobs WHERE id = :id"),
            {"id": analysis_id},
        )
    ).first()

    if job_row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Analysis job '{analysis_id}' not found",
        )

    return StreamingResponse(
        _sse_generator(analysis_id, redis),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )

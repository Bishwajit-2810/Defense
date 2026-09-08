"""Analysis endpoints — run analysis jobs and stream progress."""

from __future__ import annotations

import asyncio
import io
import json
import uuid
import zipfile
from datetime import datetime, timezone
from html import escape as html_escape
from typing import Any, AsyncGenerator

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

import redis.asyncio as aioredis
from defense.libs.labels import label_provenance
from defense.libs.jobs import clear_cancelled, mark_cancelled, purge_job_keys
from defense.libs.progress import buffer_key as progress_buffer_key, publish_stage, replay_events
from defense.services.api.deps import get_current_user, get_db, get_redis, rate_limit, resolve_llm_backend
from defense.services.api.models import (
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

try:  # WeasyPrint is optional — without it /export ships JSON instead of PDFs.
    from weasyprint import HTML  # type: ignore
except Exception:  # pragma: no cover - depends on system libs, not just the wheel
    HTML = None

router = APIRouter(prefix="/v1/analysis", tags=["analysis"])

# Job statuses that are final; anything else is reconcilable from the counters.
_TERMINAL_JOB_STATUSES = frozenset({"done", "failed", "cancelled"})

# The same set, rendered for the `status NOT IN (...)` guards below, so a SQL
# guard can never fall out of step with the Python check.
_TERMINAL_STATUS_SQL = ", ".join(f"'{s}'" for s in sorted(_TERMINAL_JOB_STATUSES))

# Re-analysis jobs feed the same Stage-1 stream the ingestion service uses —
# there is no separate analysis worker; the normal pipeline does the work and
# the assembler flips the job to done via job_id.
_NLP_STAGE1_STREAM = "nlp:stage1:queue"

# How long a non-terminal job must sit without a progress write before resume
# treats it as dead rather than busy.
#
# `jobs.updated_at` is the heartbeat: the assembler writes "running" to the row
# as every post lands, so a job that is genuinely moving touches it continuously.
# Nothing else in the system can tell a power-cut job from a slow one — a killed
# process leaves no marker — so resume needs a threshold, and it has to clear the
# slowest single post (a full comment ensemble plus summary on a long thread),
# not the average one. Below this a resume is refused and the operator is told to
# stop the job first, which is unambiguous.
_STALE_JOB_SECONDS = 300


async def _create_analysis_job(
    db: AsyncSession,
    analysis_id: str,
    selector: dict,
    tenant_id: str = "default",
    options: dict | None = None,
) -> None:
    """Insert the jobs row for an analysis run.

    ``options`` is persisted because resume has to re-enqueue the job's
    remaining posts with the SAME per-request options it started with — a job
    run with ``want_summary`` and resumed without it would finish with half its
    posts summarised and nothing saying why. This column used to be written as a
    literal ``{}``, so that request detail was thrown away at enqueue time.

    ``llm_backend`` is stored with the rest but deliberately NOT trusted on
    resume: it is re-resolved against the tenant's policy, which may have been
    privacy-locked in the meantime (§13.5).
    """
    now = datetime.now(tz=timezone.utc)
    selector["tenant_id"] = tenant_id
    await db.execute(
        text(
            """
            INSERT INTO jobs (id, tenant_id, type, status, selector, options, created_at, updated_at)
            VALUES (:id, :tenant_id, :type, :status, CAST(:selector AS jsonb), CAST(:options AS jsonb), :created_at, :updated_at)
            ON CONFLICT (id) DO NOTHING
            """
        ),
        {
            "id": analysis_id,
            "tenant_id": tenant_id,
            "type": "analysis_run",
            "status": "queued",
            "selector": json.dumps(selector, default=str),
            "options": json.dumps(options or {}, default=str),
            "created_at": now,
            "updated_at": now,
        },
    )


async def _posts_for_selector(
    db: AsyncSession,
    tenant_id: str,
    campaign_id: str | None,
    post_ids: list[str] | None,
) -> list[Any]:
    """Resolve a job selector to its ``(id, raw_payload)`` rows, tenant-scoped.

    NOTE: the selector's optional ``filter`` block (platform / posted_from /
    posted_to / sentiment bounds) is stored on the job but has never been
    applied here — a filtered run analyses the whole campaign. That is
    pre-existing behaviour, left alone deliberately so resume selects exactly
    the set its original run did; fixing it would change which posts a run
    covers, which is a separate decision.
    """
    where, params = ["tenant_id = :tenant_id"], {"tenant_id": tenant_id}
    if campaign_id:
        where.append("campaign_id = :campaign_id")
        params["campaign_id"] = campaign_id
    if post_ids:
        where.append("id = ANY(:post_ids)")
        params["post_ids"] = post_ids
    return (
        await db.execute(
            text(f"SELECT id, raw_payload FROM posts WHERE {' AND '.join(where)}"),
            params,
        )
    ).mappings().all()


async def _enqueue_for_analysis(
    redis: aioredis.Redis,
    rows: list[Any],
    *,
    analysis_id: str,
    options: dict[str, Any],
    tenant_id: str,
) -> int:
    """Re-normalize each post and push it onto Stage 1 under *analysis_id*.

    Returns how many were enqueued. Shared by the initial run and by resume so
    the two cannot drift in what they put on the stream — a resume that built a
    slightly different envelope would produce results the first half of the job
    is not comparable with.
    """
    from defense.services.ingestion.normalizer import normalize_post  # noqa: PLC0415

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
            "tenant_id": tenant_id,
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
    return enqueued


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
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Provide at least one of 'post_ids' or 'campaign_id'",
        )

    tenant_id = current_user.get("tenant_id", "default")
    options: dict[str, Any] = dict(body.options or {})
    options["tenant_id"] = tenant_id
    options["llm_backend"] = await resolve_llm_backend(db, redis, current_user, options)

    analysis_id = str(uuid.uuid4())

    selector: dict[str, Any] = {"tenant_id": tenant_id}
    if body.campaign_id:
        selector["campaign_id"] = body.campaign_id
    if body.post_ids:
        selector["post_ids"] = body.post_ids
    if body.filter:
        selector["filter"] = body.filter.model_dump(exclude_none=True)

    # --- Find the posts to (re-)analyze (tenant-scoped) --------------------
    rows = await _posts_for_selector(db, tenant_id, body.campaign_id, body.post_ids)

    if not rows:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No posts matched the selector — upload posts first",
        )

    # Re-normalize from the stored raw payload (same path ingestion uses).
    try:
        await _create_analysis_job(
            db, analysis_id, selector, tenant_id=tenant_id, options=options
        )
        enqueued = await _enqueue_for_analysis(
            redis, rows, analysis_id=analysis_id, options=options, tenant_id=tenant_id
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
    redis: aioredis.Redis = Depends(get_redis),
    current_user: dict = Depends(get_current_user),
) -> dict:
    """List recent jobs (newest first), excluding reports. Backs the Jobs tab."""
    tenant_id = current_user.get("tenant_id", "default")
    rows = (
        await db.execute(
            text(
                """
                SELECT id, type, status, selector, created_at, updated_at
                FROM jobs
                WHERE type <> 'report' AND (tenant_id = :tid OR selector->>'tenant_id' = :tid)
                ORDER BY created_at DESC
                LIMIT :limit
                """
            ),
            {"limit": limit, "tid": tenant_id},
        )
    ).mappings().all()

    jobs = []
    if rows:
        total_keys = [f"job:{r['id']}:total" for r in rows]
        completed_keys = [f"job:{r['id']}:completed" for r in rows]
        
        totals = await redis.mget(total_keys)
        completions = await redis.mget(completed_keys)
        
        for i, r in enumerate(rows):
            selector = r["selector"] or {}
            post_ids = selector.get("post_ids") or []
            
            # Both are None when the key is gone, never 0. These counters are
            # Redis keys with a 24 h TTL and they expire INDEPENDENTLY — a job
            # can be left holding `:total` without `:completed` or the reverse,
            # which is exactly what two live jobs looked like on 29 Aug 2026.
            # Coercing the missing one to 0 turns "no counter" into "none of them
            # landed", and a finished job then reports 0 %.
            total_val = int(totals[i]) if totals[i] is not None else None
            completed_val = int(completions[i]) if completions[i] is not None else None
            
            jobs.append(
                {
                    "id": r["id"],
                    "type": r["type"],
                    "status": r["status"],
                    "post_count": len(post_ids) or None,
                    "campaign_id": selector.get("campaign_id"),
                    "post_ids": post_ids,
                    "total": total_val,
                    "completed": completed_val,
                    "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                    "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
                }
            )
    return {"jobs": jobs, "total": len(jobs)}


# ---------------------------------------------------------------------------
# POST /v1/analysis/{analysis_id}/cancel  —  DELETE /v1/analysis/{analysis_id}
# ---------------------------------------------------------------------------


async def _job_row_for_tenant(db: AsyncSession, analysis_id: str, tenant_id: str) -> Any:
    """Fetch a job scoped to the caller's tenant, or 404.

    Both selectors are checked because `jobs.tenant_id` was added after the
    fact (init-db.sql backfills it to 'default'); older rows carry the tenant
    only inside `selector`, and the Jobs list reads them the same way.
    """
    row = (
        await db.execute(
            text(
                "SELECT id, status, selector, options, created_at, updated_at FROM jobs "
                "WHERE id = :id AND (tenant_id = :tid OR selector->>'tenant_id' = :tid)"
            ),
            {"id": analysis_id, "tid": tenant_id},
        )
    ).mappings().first()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Analysis job '{analysis_id}' not found",
        )
    return row


_JOB_COUNTER_TTL = 86_400  # matches the assembler's _PROGRESS_TTL


async def _seed_job_counters(
    redis: aioredis.Redis, analysis_id: str, *, total: int, completed: int
) -> None:
    """Write the counters for a job whose completion was established elsewhere.

    The assembler owns these keys in the normal case. When a job is declared
    finished by some other route — the resume endpoint's "already complete"
    shortcut — nothing writes them, and the Jobs tab then reads `completed: 0`
    against a `done` row and draws a confident **0%**. Progress is allowed to be
    unknown; it is not allowed to contradict the status next to it.

    Best-effort: a Redis failure must not turn a successful reconciliation into
    an error response.
    """
    try:
        await redis.set(f"job:{analysis_id}:total", total, ex=_JOB_COUNTER_TTL)
        await redis.set(f"job:{analysis_id}:completed", completed, ex=_JOB_COUNTER_TTL)
    except Exception as exc:  # noqa: BLE001 — reporting must not fail the call
        log.warning(
            "job_counter_seed_failed", analysis_id=analysis_id, error=str(exc)
        )


async def _seconds_since_last_stage_event(
    redis: aioredis.Redis, analysis_id: str, *, now: datetime
) -> float | None:
    """Seconds since this job's most recent stage frame, or None if there is none.

    Reads only the newest entry of the replay buffer. Returns None — meaning "no
    evidence", never "idle" — when the buffer is empty, expired (1 h TTL), or
    predates timestamped frames, so a caller must treat the absence as unknown
    rather than as proof the job is dead.
    """
    try:
        tail = await redis.lrange(progress_buffer_key(analysis_id), -1, -1)
    except Exception:
        return None
    if not tail:
        return None
    raw = tail[0]
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode()
    try:
        ts = json.loads(raw).get("ts")
    except (json.JSONDecodeError, TypeError, AttributeError):
        return None
    if not isinstance(ts, (int, float)):
        return None
    return max(0.0, now.timestamp() - float(ts))


async def _progress_snapshot(redis: aioredis.Redis, analysis_id: str) -> dict[str, Any]:
    """Best-effort {total, completed, failed} for a job, for stop/delete replies."""
    try:
        raw_total = await redis.get(f"job:{analysis_id}:total")
        return {
            "total": int(raw_total) if raw_total is not None else None,
            "completed": int(await redis.get(f"job:{analysis_id}:completed") or 0),
            "failed": int(await redis.get(f"job:{analysis_id}:failed") or 0),
        }
    except Exception:
        return {"total": None, "completed": 0, "failed": 0}


@router.post(
    "/{analysis_id}/cancel",
    summary="Stop a queued or running analysis job",
)
async def cancel_analysis(
    analysis_id: str,
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
    current_user: dict = Depends(get_current_user),
) -> dict:
    """Stop *analysis_id*: no further post of this job is analysed.

    Cancellation is cooperative — see ``libs/jobs.py``. A job is N envelopes
    spread across four Redis streams, so there is nothing to kill; this raises a
    flag that Stage 1, the router and Stage 2 each check as they pick a message
    up. Posts already mid-flight in a stage finish and are persisted (the LLM
    spend on them is already paid); everything behind them is dropped, so the
    stop costs at most one post per stage rather than the rest of the batch.

    The job row goes to ``cancelled``, which the assembler cannot overwrite, and
    a terminating ``cancelled`` frame closes any open SSE stream.
    """
    tenant_id = current_user.get("tenant_id", "default")
    job_row = await _job_row_for_tenant(db, analysis_id, tenant_id)

    job_status = job_row["status"]
    if job_status in _TERMINAL_JOB_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Job '{analysis_id}' is already in a final state (status: {job_status}) — nothing to stop",
        )

    # The flag IS the mechanism. If it did not land, the workers were never
    # told, so report the failure rather than a job row that lies about it.
    if not await mark_cancelled(redis, analysis_id, reason="stopped via API"):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Could not reach Redis to signal the workers — job not stopped",
        )

    await db.execute(
        text(
            "UPDATE jobs SET status = 'cancelled', updated_at = NOW(), "
            "error = 'cancelled by operator' "
            f"WHERE id = :id AND status NOT IN ({_TERMINAL_STATUS_SQL})"
        ),
        {"id": analysis_id},
    )
    await db.commit()

    progress = await _progress_snapshot(redis, analysis_id)

    # Terminating SSE frame so a watching dashboard stops immediately instead of
    # sitting on the stream until its 5-minute timeout. `_sse_generator` breaks
    # on this event name.
    try:
        await redis.publish(
            f"analysis:progress:{analysis_id}",
            json.dumps(
                {
                    "event": "cancelled",
                    "job_id": analysis_id,
                    "status": "cancelled",
                    "reason": "stopped by operator",
                    **progress,
                },
                ensure_ascii=False,
            ),
        )
    except Exception as exc:
        log.warning("analysis_cancel_publish_failed", analysis_id=analysis_id, error=str(exc))

    log.info(
        "analysis_cancelled",
        analysis_id=analysis_id,
        tenant_id=tenant_id,
        previous_status=job_status,
        **progress,
    )
    return {
        "analysis_id": analysis_id,
        "status": "cancelled",
        "previous_status": job_status,
        "progress": progress,
    }


@router.post(
    "/{analysis_id}/resume",
    summary="Re-enqueue only the posts an interrupted job never finished",
)
async def resume_analysis(
    analysis_id: str,
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
    current_user: dict = Depends(get_current_user),
) -> dict:
    """Continue *analysis_id* from where it stopped, under the same job id.

    Written for the case the pipeline cannot detect on its own: the machine lost
    power (or the workers were killed) with 30 of 300 posts analysed. Nothing
    marks that job as dead — its row still reads ``running``, its Redis counters
    may be gone entirely, and the envelopes it had in flight were lost with the
    streams. A plain re-run would re-analyse all 300 and pay for the 30 again.

    So "what is left" is derived from **Postgres alone**, the only thing that
    survives the power cut: the job's selector gives the full post set, and a
    post counts as done when its ``analysis_results`` row was written at or
    after the job started. ``analysis_results`` is keyed ``UNIQUE (post_id)``
    with no job column, so that timestamp is the only available discriminator;
    it means a post another job re-analysed in the meantime also counts as done,
    which is correct — a fresh result exists either way.

    The Redis counters are then rebuilt to match (``total`` = the whole set,
    ``completed`` = what is already done, ``failed`` reset), which is what lets
    the assembler finish the job when the remaining posts land instead of
    flipping it done on the first one. Progress therefore continues from 30/300
    rather than restarting at 0.

    Refused with 409 while the job still looks alive — stop it first, then
    resume — and there is no per-post partial resume: a post that was mid-flight
    is redone from Stage 1.
    """
    tenant_id = current_user.get("tenant_id", "default")
    job_row = await _job_row_for_tenant(db, analysis_id, tenant_id)

    job_status: str = job_row["status"]
    selector: dict = job_row["selector"] or {}
    campaign_id: str | None = selector.get("campaign_id")
    post_ids: list[str] = selector.get("post_ids") or []

    if not campaign_id and not post_ids:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                f"Job '{analysis_id}' has no campaign_id or post_ids in its selector, "
                "so there is no post set to resume — start a new run instead"
            ),
        )

    if job_status == "done":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Job '{analysis_id}' already completed — nothing to resume",
        )

    # A job that is still writing progress is not interrupted, it is busy.
    # Re-enqueueing under it would duplicate work and corrupt its counters.
    if job_status not in _TERMINAL_JOB_STATUSES:
        updated_at = job_row["updated_at"] or job_row["created_at"]
        idle_for: float | None = None
        now = datetime.now(tz=timezone.utc)
        if updated_at is not None:
            if updated_at.tzinfo is None:
                updated_at = updated_at.replace(tzinfo=timezone.utc)
            idle_for = (now - updated_at).total_seconds()

        # `jobs.updated_at` is written by the ASSEMBLER and by nobody else, so it
        # sits at the creation time for the whole time a post spends in Stage 1
        # and Stage 2 — minutes, on a local LLM. Judging liveness by it alone
        # declares a perfectly healthy job stalled and hands it to resume, which
        # then re-enqueues a post that is still being worked on. The stage-event
        # buffer is the per-post proof of life the jobs row cannot be.
        stage_idle = await _seconds_since_last_stage_event(redis, analysis_id, now=now)
        if stage_idle is not None and (idle_for is None or stage_idle < idle_for):
            idle_for = stage_idle

        if idle_for is not None and idle_for < _STALE_JOB_SECONDS:
            where = "the pipeline" if stage_idle is not None and stage_idle <= idle_for else "the job row"
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"Job '{analysis_id}' is still making progress ({where} last "
                    f"reported {int(idle_for)}s ago) — stop it first, then resume"
                ),
            )

    # --- What is left, from Postgres only ---------------------------------
    rows = await _posts_for_selector(db, tenant_id, campaign_id, post_ids)
    if not rows:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No posts matched the job's selector — they may have been deleted",
        )

    all_ids = [r["id"] for r in rows]

    # Which of this job's posts already have a result — and, critically, whether
    # THIS job produced it.
    #
    # `analysis_results` is upserted `ON CONFLICT (post_id)`: a second run of the
    # same post overwrites the row and moves `updated_at`. This check used to be
    # `updated_at >= job.created_at` alone, which asks "has anything touched this
    # post since the job started?" — a question a *concurrent re-run of the same
    # post* answers yes to. That is how a job whose only post was still inside
    # Stage 2 got marked `done`: an earlier run of the same post landed 20
    # seconds before this job's post even reached the router, and its `updated_at`
    # satisfied the test.
    #
    # `processing.job_id` (stamped by the assembler) answers the real question.
    # The timestamp arm is kept only for rows written before stamping existed —
    # `? 'job_id'` tells the two apart, so an unstamped row is never silently
    # treated as proof.
    result_rows = (
        await db.execute(
            text(
                """
                SELECT post_id,
                       result->'processing'->>'job_id'  AS produced_by,
                       (result->'processing' ? 'job_id') AS has_provenance
                FROM analysis_results
                WHERE tenant_id = :tid AND post_id = ANY(:ids)
                  AND updated_at >= :since
                """
            ),
            {"tid": tenant_id, "ids": all_ids, "since": job_row["created_at"]},
        )
    ).mappings().all()

    done_ids = {r["post_id"] for r in result_rows if r["produced_by"] == analysis_id}
    # A row that carries provenance naming a *different* job is positive evidence
    # that this job's own post has not landed — not merely absent evidence.
    foreign_ids = {
        r["post_id"]
        for r in result_rows
        if r["has_provenance"] and r["produced_by"] != analysis_id
    }
    legacy_ids = {r["post_id"] for r in result_rows if not r["has_provenance"]}
    # Unstamped rows can only be read the old, ambiguous way. They count, but the
    # answer says so rather than presenting a guess as a fact.
    done_ids |= legacy_ids

    remaining = [r for r in rows if r["id"] not in done_ids]

    if not remaining:
        # Every post has a result attributable to this job (or a pre-provenance
        # row we cannot attribute at all); the row was simply never flipped
        # because the assembler died before the last one landed.
        await db.execute(
            text(
                "UPDATE jobs SET status = 'done', updated_at = NOW(), error = NULL "
                "WHERE id = :id"
            ),
            {"id": analysis_id},
        )
        await db.commit()
        # Bring the Redis counters in line with the status we just wrote.
        # Without this the Jobs tab reads `completed: 0` against `total: N` for a
        # job whose row says `done` and renders a confident **0%** — the status
        # and the progress bar disagreeing is worse than either being missing.
        await _seed_job_counters(redis, analysis_id, total=len(all_ids), completed=len(done_ids))
        log.info(
            "analysis_resume_already_complete",
            analysis_id=analysis_id,
            total=len(all_ids),
            attributed=len(all_ids) - len(legacy_ids),
            unattributable=len(legacy_ids),
        )
        return {
            "analysis_id": analysis_id,
            "resumed": False,
            "status": "done",
            "reason": (
                "every post in the selector already has a result from this job"
                if not legacy_ids
                else (
                    f"every post in the selector has a result; {len(legacy_ids)} of them "
                    "predate job provenance, so they are matched by timestamp only"
                )
            ),
            "progress": {"total": len(all_ids), "completed": len(done_ids), "remaining": 0},
        }

    if foreign_ids:
        log.info(
            "analysis_resume_foreign_results_ignored",
            analysis_id=analysis_id,
            posts=len(foreign_ids),
        )

    # --- Options: reuse the request's, re-resolve the backend --------------
    # Stored so a resumed job asks for the same tasks the original did; the
    # backend is re-resolved because the tenant may have been privacy-locked
    # since (§13.5), and a stored 'groq' must not survive that.
    options: dict[str, Any] = dict(job_row["options"] or {})
    options["tenant_id"] = tenant_id
    options["llm_backend"] = await resolve_llm_backend(db, redis, current_user, options)

    # Lower the stop flag BEFORE enqueueing — the workers drop anything carrying
    # it, so the order is the difference between a resume and a no-op.
    if not await clear_cancelled(redis, analysis_id):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Could not reach Redis to clear the job's stop flag — not resumed",
        )

    # Rebuild the counters the power cut took with it. `completed` is seeded with
    # the work already done, which is what makes the assembler finish the job on
    # the last remaining post instead of on the first one (with no `total` it
    # falls back to "first landing wins"), and what makes the dashboard resume
    # the bar at 30/300 rather than 0/270.
    try:
        await redis.set(f"job:{analysis_id}:total", len(all_ids), ex=86_400)
        await redis.set(f"job:{analysis_id}:completed", len(done_ids), ex=86_400)
        # Old failures are being retried in `remaining`, so counting them again
        # would push completed+failed past total and finish the job early.
        await redis.delete(f"job:{analysis_id}:failed")
    except Exception as exc:
        log.warning("analysis_resume_counter_reset_failed", analysis_id=analysis_id, error=str(exc))

    enqueued = await _enqueue_for_analysis(
        redis, remaining, analysis_id=analysis_id, options=options, tenant_id=tenant_id
    )
    if enqueued == 0:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="All remaining posts failed normalization",
        )

    await db.execute(
        text(
            "UPDATE jobs SET status = 'running', updated_at = NOW(), error = NULL "
            "WHERE id = :id"
        ),
        {"id": analysis_id},
    )
    await db.commit()

    log.info(
        "analysis_resumed",
        analysis_id=analysis_id,
        previous_status=job_status,
        total=len(all_ids),
        already_done=len(done_ids),
        re_enqueued=enqueued,
    )
    return {
        "analysis_id": analysis_id,
        "resumed": True,
        "status": "running",
        "previous_status": job_status,
        "progress": {
            "total": len(all_ids),
            "completed": len(done_ids),
            "remaining": enqueued,
        },
    }


@router.delete(
    "/{analysis_id}",
    summary="Delete an analysis job record (stops it first if still running)",
)
async def delete_analysis(
    analysis_id: str,
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
    current_user: dict = Depends(get_current_user),
) -> dict:
    """Remove the job row for *analysis_id* plus its progress counters and trace.

    A still-running job is stopped first — deleting the row without raising the
    stop flag would leave the batch churning through the LLM against a job id
    that no longer exists.

    This deletes the JOB, not the analysis of the posts. ``analysis_results``
    rows are keyed by post and campaign, not by job, and a post's latest result
    is what every other tab reads — several jobs (and the original ingest) write
    the same rows, so removing them here would silently blank posts that another
    job analysed. Use ``DELETE /v1/posts/{post_id}`` to drop a post's data.
    """
    tenant_id = current_user.get("tenant_id", "default")
    job_row = await _job_row_for_tenant(db, analysis_id, tenant_id)

    job_status = job_row["status"]
    was_running = job_status not in _TERMINAL_JOB_STATUSES
    cancelled = False
    if was_running:
        cancelled = await mark_cancelled(redis, analysis_id, reason="job deleted")
        if not cancelled:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=(
                    "Could not reach Redis to stop the running job — refusing to "
                    "delete a job that would keep processing"
                ),
            )

    result = await db.execute(
        text("DELETE FROM jobs WHERE id = :id AND (tenant_id = :tid OR selector->>'tenant_id' = :tid)"),
        {"id": analysis_id, "tid": tenant_id},
    )
    await db.commit()

    # Progress counters and the Trace tab's replay buffer. The cancel flag is
    # deliberately left behind to expire on its own TTL — it is the only thing
    # still stopping this job's in-flight envelopes.
    purged = await purge_job_keys(redis, analysis_id)

    log.info(
        "analysis_job_deleted",
        analysis_id=analysis_id,
        tenant_id=tenant_id,
        previous_status=job_status,
        stopped=cancelled,
        redis_keys_purged=purged,
    )
    return {
        "analysis_id": analysis_id,
        "deleted": {"jobs": result.rowcount or 0, "redis_keys": purged},
        "stopped": cancelled,
        "previous_status": job_status,
    }


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
    tenant_id = current_user.get("tenant_id", "default")
    where = "WHERE tenant_id = :tid"
    params: dict[str, Any] = {"top": top, "tid": tenant_id}
    if campaign_id:
        where += " AND campaign_id = :campaign_id"
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
    # The comma-join puts analysis_results first, so the tenant/campaign filter must be
    # a fresh WHERE clause here (not appended to the scalar `where`).
    topic_filter = "WHERE tenant_id = :tid" + (" AND campaign_id = :campaign_id" if campaign_id else "")
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
            f"AND result->'emotion'->>'primary' IS NOT NULL "
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
            f"AND result->'processing'->>'llm_backend' IS NOT NULL "
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


def _esc(value: object) -> str:
    """HTML-escape anything on its way into the PDF.

    Every interpolated value goes through this. Half of them used to be escaped
    by hand and half not — and the unescaped half (post_type, emotion, platform)
    is LLM output, i.e. the one source that can contain angle brackets nobody
    reviewed.
    """
    return html_escape("" if value is None else str(value), quote=True)


def _ensemble_note(res: dict) -> str:
    """One line describing how this post's comment labels were produced."""
    ens = (res.get("comment_analysis") or {}).get("ensemble") or {}
    if not ens:
        return "single labeller (no ensemble recorded)"
    voters = ", ".join(ens.get("voters") or []) or "none"
    return (
        f"{ens.get('comments', 0)} comments · voters: {voters} · "
        f"{round((ens.get('unanimous_share') or 0) * 100)}% unanimous · "
        f"{round((ens.get('escalated_share') or 0) * 100)}% escalated to the LLM · "
        f"{ens.get('abstained', 0)} abstained"
    )


def _result_to_html(res: dict) -> str:
    if isinstance(res, str):
        try:
            res = json.loads(res)
        except Exception:
            res = {}
    if not isinstance(res, dict):
        res = {}
    # Safely get variables
    post_id = _esc(res.get("post_id", "Unknown"))
    alert = res.get("watchlist_alert", False)
    alert_reason = _esc(res.get("watchlist_alert_reason") or "")

    # Overview metrics
    overall_sentiment = str(res.get("overall_sentiment", "neutral")).lower()
    sentiment_score = res.get("sentiment_score") or 0.0
    conf = res.get("confidence") or {}
    conf_overall = conf.get("overall", 0.0)
    platform = _esc(res.get("platform", "Unknown"))
    lang = _esc(res.get("language", "und"))
    post_type = _esc(res.get("post_type") or "")
    primary_emotion = _esc((res.get("emotion") or {}).get("primary", ""))
    tox = res.get("toxicity_score") or 0.0
    hate = res.get("hate_speech_score") or 0.0

    # Content
    post_text = _esc(res.get("post_text") or res.get("text", ""))
    summary = _esc(res.get("post_summary", ""))
    insight = _esc(res.get("insight", ""))

    # Sentiments. A missing component is reported as missing — NOT as neutral.
    # "Image: neutral 0.000" on a text-only post is the exact claim this project
    # retracted for the pipeline; a PDF is not allowed to reintroduce it.
    _text_s = res.get("text_sentiment") or {}
    _img_s = res.get("image_sentiment") or {}
    text_sent = _text_s.get("label")
    text_score = _text_s.get("score")
    img_sent = _img_s.get("label")
    img_score = _img_s.get("score")
    baseline_sent = res.get("baseline_sentiment") or 0.0

    # Helper for formatting
    def pct(val): return f"{round((val or 0) * 100)}%"
    def get_sev_color(s):
        if s > 0.5: return "#ef4444"
        if s > 0.2: return "#f59e0b"
        return "#10b981"
    def get_sent_color(s):
        if s == 'positive': return "color: #059669; background: #d1fae5; border-color: #a7f3d0;"
        if s == 'negative': return "color: #e11d48; background: #ffe4e6; border-color: #fecdd3;"
        return "color: #334155; background: #f1f5f9; border-color: #e2e8f0;"
    
    def get_sent_class(s):
        return 'chip-pos' if s == 'positive' else 'chip-neg' if s == 'negative' else 'chip-neutral'

    # Build Chips
    chips_html = ""
    chips_html += f'<span class="chip {get_sent_class(overall_sentiment)}">sentiment {overall_sentiment} {sentiment_score:.2f}</span>'
    if conf_overall is not None:
        chips_html += f'<span class="chip chip-brand">confidence {pct(conf_overall)}</span>'
    if platform:
        chips_html += f'<span class="chip chip-neutral">{platform} &bull; lang: {lang}</span>'
    if post_type:
        chips_html += f'<span class="chip chip-indigo">type {post_type}</span>'
    if primary_emotion:
        chips_html += f'<span class="chip chip-purple">emotion {primary_emotion}</span>'
    if tox is not None:
        chips_html += f'<span class="chip chip-neutral">toxicity {pct(tox)}</span>'
    if hate is not None:
        chips_html += f'<span class="chip chip-neutral">hate {pct(hate)}</span>'

    # Alert HTML. The reason is printed, not just the verdict: an `always`
    # target alerts on a plain mention, which is NOT hostility, and a report
    # that describes every alert as an attack misreads its own watchlist.
    alert_html = ""
    if alert:
        alert_html = f"""
        <div class="alert-box">
            <h3 class="alert-title">⚠️ Watchlist alert</h3>
            <p class="alert-text">{alert_reason or "A watchlist target was matched in this post or its comments."}</p>
        </div>
        """

    # Summary & Insight HTML
    summary_html = ""
    if summary or insight:
        summary_html += '<div class="grid-2">'
        if summary:
            summary_html += f"""
            <div>
                <div class="section-title-sm">Post Summary</div>
                <div class="box box-brand">{summary}</div>
            </div>
            """
        if insight:
            summary_html += f"""
            <div>
                <div class="section-title-sm">Insight (Stage 2)</div>
                <div class="box box-amber">{insight}</div>
            </div>
            """
        summary_html += '</div>'

    def render_sent_item(label, val, score=None, absent_note="not measured"):
        """One sentiment tile. An absent component says so, and shows no score.

        `val or "N/A"` with a 0.000 underneath it read as a measured neutral —
        the failure this project already fixed in the pipeline (README: "a
        failed image fetch now reports as a failure instead of as a neutral
        verdict"). Absence is rendered as absence.
        """
        if not val:
            return f"""
        <div class="sent-item">
            <div class="sent-label">{_esc(label)}</div>
            <div class="sent-val" style="color: #94a3b8;">—</div>
            <div class="sent-score">{_esc(absent_note)}</div>
        </div>
        """
        val_str = _esc(str(val).lower())
        color = "#059669" if val_str == "positive" else "#e11d48" if val_str == "negative" else "#64748b"
        score_html = ""
        if score is not None:
            try:
                score_html = f'<div class="sent-score">{float(score):.3f}</div>'
            except (TypeError, ValueError):
                score_html = ""
        return f"""
        <div class="sent-item">
            <div class="sent-label">{_esc(label)}</div>
            <div class="sent-val" style="color: {color};">{val_str}</div>
            {score_html}
        </div>
        """
        
    def render_bar(label, val, color):
        val = val or 0.0
        return f"""
        <div class="bar-wrap">
            <div class="bar-label"><span>{label}</span><span>{pct(val)}</span></div>
            <div class="bar-track"><div class="bar-fill" style="background-color: {color}; width: {pct(val)};"></div></div>
        </div>
        """

    # Signals
    conf = res.get("confidence") or {}
    signals_html = f"""
    <div class="grid-3" style="margin-bottom: 24px; padding: 16px; background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 8px;">
        <div>
            <h4 class="sig-title">Emotion</h4>
            <span class="chip chip-purple" style="display:inline-block;">{primary_emotion.upper() if primary_emotion else "NONE"}</span>
            <h4 class="sig-title" style="margin-top:12px;">Comment labels</h4>
            <div style="font-size:11px;color:#475569;">{_esc(_ensemble_note(res))}</div>
        </div>
        <div>
            <h4 class="sig-title">Confidence</h4>
            {render_bar('Overall', conf.get('overall', 0), '#3b82f6')}
            {render_bar('Sentiment', conf.get('sentiment', 0), '#3b82f6')}
            {render_bar('Language', conf.get('language', 0), '#3b82f6')}
            {render_bar('Topics', conf.get('topics', 0), '#3b82f6')}
        </div>
        <div>
            <h4 class="sig-title">Safety</h4>
            {render_bar('Toxicity', tox, get_sev_color(tox))}
            {render_bar('Hate speech', hate, get_sev_color(hate))}
        </div>
    </div>
    """

    # Comments — analysed ones (voted by models / stage2_selected / highest reactions) come first
    raw_comments = (res.get("comment_analysis") or {}).get("comments", [])
    comments = sorted(
        raw_comments,
        key=lambda c: (
            0 if (c.get("label_voters", 0) > 0 or c.get("stage2_selected") is True or bool(c.get("parallel_labels"))) else 1,
            -int(c.get("likes") or 0),
        ),
    )
    comments_html = ""
    for c in comments:
        c_text = _esc(c.get("text", c.get("comment_text", "")))
        c_sent = str(c.get("sentiment") or "neutral").lower()
        c_score = c.get("sentiment_score")
        c_emo = c.get("emotion", "")
        agreement = c.get("label_agreement")

        tags = []
        if c_emo:
            tags.append(f'<span class="c-tag">E: {_esc(c_emo)}</span>')
        if c_score is not None:
            try:
                tags.append(f'<span class="c-tag">SCORE: {float(c_score):.2f}</span>')
            except (TypeError, ValueError):
                pass
        # How many labellers backed this call, and whether it was copied from a
        # near-duplicate — a report that prints a label owes the reader both.
        if agreement is not None:
            try:
                tags.append(f'<span class="c-tag">AGREEMENT: {round(float(agreement) * 100)}%</span>')
            except (TypeError, ValueError):
                pass
        if c.get("label_source") == "propagated":
            tags.append('<span class="c-tag">PROPAGATED</span>')
        tags_str = " ".join(tags)
        
        comments_html += f"""
        <table style="width: 100%; border-bottom: 1px solid #e2e8f0; margin-bottom: 12px; padding-bottom: 12px; page-break-inside: avoid;">
            <tr>
                <td style="width: 100px; vertical-align: top; padding-right: 12px;">
                    <div class="c-pill" style="{get_sent_color(c_sent)}">{c_sent.upper()}</div>
                </td>
                <td style="vertical-align: top; font-size: 13px; color: #1e293b; line-height: 1.5;">
                    {c_text}
                </td>
            </tr>
            <tr>
                <td></td>
                <td style="padding-top: 6px;">{tags_str}</td>
            </tr>
        </table>
        """
        
    if not comments:
        comments_html = "<div style='color:#64748b; font-size:13px;'>No comments found for this post.</div>"

    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <style>
            @page {{
                size: A4;
                margin: 20mm;
                @bottom-center {{
                    content: "Page " counter(page) " of " counter(pages);
                    font-size: 9pt;
                    color: #94a3b8;
                    font-family: sans-serif;
                }}
            }}
            body {{
                font-family: system-ui, -apple-system, sans-serif;
                color: #0f172a;
                font-size: 13px;
                line-height: 1.5;
                margin: 0;
            }}
            .header {{
                border-bottom: 1px solid #e2e8f0;
                padding-bottom: 16px;
                margin-bottom: 24px;
                background: #f8fafc;
                padding: 16px;
                border-radius: 8px;
            }}
            .header h3 {{ margin: 0 0 4px 0; font-size: 20px; font-weight: 700; }}
            .header .meta {{ font-size: 12px; color: #64748b; font-family: monospace; }}
            
            .alert-box {{
                background: #fff1f2;
                border: 1px solid #fecdd3;
                border-radius: 8px;
                padding: 16px;
                margin-bottom: 24px;
            }}
            .alert-title {{ margin: 0 0 4px 0; color: #be123c; font-size: 14px; font-weight: 700; }}
            .alert-text {{ margin: 0; color: #e11d48; font-size: 13px; }}
            
            .chips-container {{ margin-bottom: 24px; line-height: 2.2; }}
            .chip {{
                display: inline-block;
                padding: 3px 10px;
                margin: 0 6px 6px 0;
                border-radius: 999px;
                font-size: 11px;
                font-weight: 600;
                text-transform: uppercase;
                border: 1px solid;
            }}
            .chip-pos {{ background: #dcfce7; border-color: #bbf7d0; color: #15803d; }}
            .chip-neg {{ background: #ffe4e6; border-color: #fecdd3; color: #be123c; }}
            .chip-neutral {{ background: #f1f5f9; border-color: #e2e8f0; color: #475569; }}
            .chip-brand {{ background: #eff6ff; border-color: #bfdbfe; color: #1d4ed8; }}
            .chip-indigo {{ background: #eef2ff; border-color: #c7d2fe; color: #4338ca; }}
            .chip-purple {{ background: #faf5ff; border-color: #e9d5ff; color: #7e22ce; }}
            
            .section-title {{
                font-size: 12px;
                font-weight: 700;
                text-transform: uppercase;
                letter-spacing: 0.05em;
                color: #94a3b8;
                border-bottom: 1px solid #e2e8f0;
                padding-bottom: 8px;
                margin: 0 0 12px 0;
            }}
            .section-title-sm {{
                font-size: 11px;
                font-weight: 700;
                text-transform: uppercase;
                color: #94a3b8;
                border-bottom: 1px solid #e2e8f0;
                padding-bottom: 6px;
                margin: 0 0 10px 0;
            }}
            
            .box {{
                background: #f8fafc;
                border: 1px solid #e2e8f0;
                border-radius: 8px;
                padding: 16px;
                white-space: pre-wrap;
                margin-bottom: 24px;
            }}
            .box-brand {{ background: #f0fdfa; border-color: #ccfbf1; color: #115e59; }}
            .box-amber {{ background: #fffbeb; border-color: #fef3c7; color: #92400e; }}
            
            .grid-2 {{ display: table; width: 100%; table-layout: fixed; margin-bottom: 24px; }}
            .grid-2 > div {{ display: table-cell; width: 50%; vertical-align: top; }}
            .grid-2 > div:first-child {{ padding-right: 12px; }}
            .grid-2 > div:last-child {{ padding-left: 12px; }}
            
            .grid-3 {{ display: table; width: 100%; table-layout: fixed; margin-bottom: 24px; }}
            .grid-3 > div {{ display: table-cell; width: 33.33%; vertical-align: top; padding: 0 8px; }}
            
            .grid-4 {{ display: table; width: 100%; table-layout: fixed; margin-bottom: 24px; }}
            .grid-4 > div {{ display: table-cell; width: 25%; vertical-align: top; padding: 0 6px; }}
            
            .sent-item {{
                background: #f8fafc;
                border: 1px solid #e2e8f0;
                border-radius: 8px;
                padding: 12px;
                text-align: center;
            }}
            .sent-label {{ font-size: 10px; font-weight: 700; text-transform: uppercase; color: #64748b; margin-bottom: 4px; }}
            .sent-val {{ font-size: 14px; font-weight: 600; text-transform: capitalize; margin-bottom: 2px; }}
            .sent-score {{ font-size: 11px; color: #94a3b8; }}
            
            .sig-title {{ font-size: 13px; font-weight: 600; margin: 0 0 12px 0; color: #0f172a; }}
            .bar-wrap {{ margin-bottom: 10px; }}
            .bar-label {{ display: block; font-size: 11px; font-weight: 600; color: #475569; margin-bottom: 4px; }}
            .bar-label span:last-child {{ float: right; }}
            .bar-track {{ height: 6px; background: #e2e8f0; border-radius: 3px; overflow: hidden; clear: both; }}
            .bar-fill {{ height: 100%; border-radius: 3px; }}
            
            .c-pill {{ display: inline-block; padding: 2px 6px; border-radius: 4px; font-size: 10px; font-weight: 700; border: 1px solid; }}
            .c-tag {{ display: inline-block; font-size: 10px; font-weight: 600; color: #64748b; background: #f1f5f9; padding: 2px 6px; border-radius: 4px; border: 1px solid #e2e8f0; margin-right: 6px; text-transform: uppercase; }}
        </style>
    </head>
    <body>
        <div class="header">
            <h3>Post Detail</h3>
            <div class="meta">{post_id} &bull; Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}</div>
        </div>

        {alert_html}

        <div class="chips-container">
            {chips_html}
        </div>

        <div class="section-title">Original Post</div>
        <div class="box">{post_text}</div>

        {summary_html}

        <div class="section-title">Sentiment Breakdown</div>
        <div class="grid-4">
            <div>{render_sent_item('Text', text_sent, text_score, 'no caption')}</div>
            <div>{render_sent_item('Image', img_sent, img_score, 'no image analysed')}</div>
            <div>{render_sent_item('Overall', overall_sentiment, sentiment_score)}</div>
            <div>{render_sent_item('Baseline (Upstream)', 'positive' if baseline_sent > 0.1 else 'negative' if baseline_sent < -0.1 else 'neutral', baseline_sent)}</div>
        </div>

        <div class="section-title">Signals</div>
        {signals_html}

        <div class="section-title">Every Comment</div>
        <div style="background: white; border: 1px solid #e2e8f0; border-radius: 8px; padding: 16px;">
            {comments_html}
        </div>
    </body>
    </html>
    """

#: Hard ceiling on one export. Rendering is ~50-150 ms of CPU and a few MB of
#: peak memory per post, single-threaded inside one request: 5,000 posts is
#: minutes of blocked worker and gigabytes of ZIP held in RAM, repeatable by any
#: authenticated caller. Ask for a narrower slice, or page through with `offset`.
EXPORT_MAX_POSTS = 200


def _generate_pdfs(rows) -> tuple[io.BytesIO, int]:
    """Render already-filtered rows into an in-memory ZIP. Returns (buffer, n)."""
    zip_buffer = io.BytesIO()
    count = 0
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
        for row in rows:
            res = row["result"] or {}
            post_id = str(row["post_id"])
            # A single bad row must not lose the other 199.
            try:
                if HTML is not None:
                    zip_file.writestr(
                        f"{post_id}.pdf", HTML(string=_result_to_html(res)).write_pdf()
                    )
                else:
                    zip_file.writestr(f"{post_id}.json", json.dumps(res, indent=2, ensure_ascii=False))
                count += 1
            except Exception as exc:
                log.error("export_render_failed", post_id=post_id, error=str(exc))
                zip_file.writestr(
                    f"{post_id}.ERROR.txt",
                    f"Rendering this post failed: {exc}\n"
                    "The rest of the export is unaffected.",
                )
        if count == 0:
            # Say which it was. An empty ZIP that means "no alerts" and one that
            # means "nothing analysed yet" are different answers.
            zip_file.writestr(
                "empty.txt",
                "No results matched this export. Either nothing has been analysed "
                "yet, or no post in the selected range raised a watchlist alert.",
            )
    zip_buffer.seek(0)
    return zip_buffer, count


@router.get(
    "/export",
    summary="Download analysis results as a ZIP of PDF files",
)
async def export_analysis(
    only_warnings: bool = Query(False, description="Only download posts with watchlist alerts"),
    limit: int = Query(50, ge=1, le=EXPORT_MAX_POSTS, description="Posts in this ZIP"),
    offset: int = Query(0, ge=0, description="Skip this many posts — page through larger sets"),
    campaign_id: str | None = Query(None, description="Optional campaign filter"),
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """Export analysed posts as a ZIP of one PDF each.

    Bounded on purpose. `only_warnings` filters in SQL rather than after a
    fixed 5,000-row fetch — filtering afterwards silently dropped every alert
    older than the newest 5,000 posts and reported the result as complete.
    """
    tenant_id = current_user.get("tenant_id", "default")
    where = ["ar.tenant_id = :tid"]
    params: dict[str, Any] = {"limit": limit, "offset": offset, "tid": tenant_id}
    if only_warnings:
        # JSONB containment: the alert lives inside the canonical result.
        where.append("ar.result @> '{\"watchlist_alert\": true}'::jsonb")
    if campaign_id:
        where.append("ar.campaign_id = :campaign_id")
        params["campaign_id"] = campaign_id

    rows = (
        await db.execute(
            text(
                f"""
                SELECT ar.post_id, ar.result
                FROM analysis_results ar
                WHERE {' AND '.join(where)}
                ORDER BY ar.created_at DESC
                LIMIT :limit OFFSET :offset
                """
            ),
            params,
        )
    ).mappings().all()

    zip_buffer, count = await asyncio.to_thread(_generate_pdfs, rows)

    prefix = "warnings" if only_warnings else "all_analyses"
    filename = f"defense_{prefix}_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.zip"

    log.info(
        "analysis_exported",
        posts=count, only_warnings=only_warnings, limit=limit, offset=offset,
        renderer="weasyprint" if HTML is not None else "json",
    )
    return StreamingResponse(
        zip_buffer,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            # So a caller can tell a truncated page from a complete export.
            "X-Export-Count": str(count),
            "X-Export-Limit": str(limit),
            "X-Export-Offset": str(offset),
        },
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
    tenant_id = current_user.get("tenant_id", "default")
    params: dict[str, Any] = {"limit": limit, "tid": tenant_id}
    where = "WHERE ar.tenant_id = :tid"
    if campaign_id:
        where += " AND ar.campaign_id = :campaign_id"
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
    tenant_id = current_user.get("tenant_id", "default")
    job_row = (
        await db.execute(
            text(
                "SELECT id, status, selector, created_at FROM jobs "
                "WHERE id = :id AND (tenant_id = :tid OR selector->>'tenant_id' = :tid)"
            ),
            {"id": analysis_id, "tid": tenant_id},
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
        raw_completed = await redis.get(f"job:{analysis_id}:completed")
        raw_failed = await redis.get(f"job:{analysis_id}:failed")
        # Ints for the reconciliation arithmetic below, None for what is
        # reported: "the counter is gone" and "the counter says zero" are
        # different facts and the caller has to be able to tell them apart.
        completed = int(raw_completed) if raw_completed is not None else 0
        failed = int(raw_failed) if raw_failed is not None else 0
        if raw_total is not None or completed or failed:
            total = int(raw_total) if raw_total is not None else None
            progress = {
                "total": total,
                "completed": completed if raw_completed is not None else None,
                "failed": failed if raw_failed is not None else None,
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
                        f"WHERE id = :id AND status NOT IN ({_TERMINAL_STATUS_SQL})"
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
        # The reconciliation UPDATE above runs on the request's session, so if it
        # is the thing that failed, the transaction is aborted and every query
        # below (`include=results`) would raise "current transaction is aborted".
        # Progress is best-effort; the results are not.
        try:
            await db.rollback()
        except Exception:
            pass

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
                        WHERE ar.tenant_id = :tid AND ar.campaign_id = :campaign_id
                        ORDER BY ar.created_at DESC
                        LIMIT :limit
                        """
                    ),
                    {"campaign_id": campaign_id, "limit": limit, "tid": tenant_id},
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
                        WHERE ar.tenant_id = :tid AND ar.post_id = ANY(:post_ids)
                        ORDER BY ar.created_at DESC
                        LIMIT :limit
                        """
                    ),
                    {"post_ids": post_ids, "limit": limit, "tid": tenant_id},
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
    sentiment: str = Query(
        "all",
        description="all | positive | negative | neutral | uncertain | disagreed",
    ),
    emotion: str = Query("all", description="all | anger | sadness | joy | fear | disgust | surprise | neutral"),
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> dict:
    """Return every analysed comment for a post, paginated.

    Reads the per-comment list from the canonical result (analysis_results JSONB)
    so no extra datastore is needed. ``sentiment`` and ``emotion`` filter the
    returned slice; the breakdown counts always reflect the full set.
    """
    tenant_id = current_user.get("tenant_id", "default")
    row = (
        await db.execute(
            text("SELECT result FROM analysis_results WHERE post_id = :pid AND tenant_id = :tid"),
            {"pid": post_id, "tid": tenant_id},
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
    if sentiment == "disagreed":
        # The review queue: every comment the labellers did not agree on. This
        # is the set worth a human's time, and the set a gold standard should
        # be built from.
        filtered = [
            c for c in filtered
            if (c.get("label_agreement") is not None and c["label_agreement"] < 1.0)
        ]
    elif sentiment != "all":
        filtered = [c for c in filtered if (c.get("sentiment") or "neutral") == sentiment]
    if emotion != "all":
        filtered = [c for c in filtered if (c.get("emotion") or "neutral") == emotion]

    # Priority sort: analysed comments (voted by models / stage2_selected / highest reactions) come first
    filtered = sorted(
        filtered,
        key=lambda c: (
            0 if (c.get("label_voters", 0) > 0 or c.get("stage2_selected") is True or bool(c.get("parallel_labels"))) else 1,
            -int(c.get("likes") or 0),
        ),
    )

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
        # How the labels were produced and how much the labellers agreed —
        # reported next to the counts, like coverage and provenance.
        "ensemble": ca.get("ensemble", {}),
        # Which comments the router put in front of the models (top-N by reaction
        # count). Surfaced on its own as well as inside `ensemble`, so a row
        # written by a run whose comment lane failed still says how much of the
        # thread was ever meant to be analysed.
        "stage2_selection": ca.get("stage2_selection", {}),
        "target_stances": ca.get("target_stances", {}),
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


def _analysed_by_models(ca: dict) -> int | None:
    """How many of a post's comments a Stage-2 model actually read.

    NOT ``comment_analysis.analyzed`` — that is the number of comment rows Stage 1
    stored, which equals the whole thread and so cannot say what the ensemble
    covered. The honest count is ``ensemble.analysed`` (the set every voter read),
    falling back to the router's ``stage2_selection.selected``.

    Returns None when neither block exists (rows written before the ensemble
    recorded itself) so callers can omit the number instead of substituting the
    stored-row count, which would overstate coverage — the case this function
    exists to prevent: 87 rows stored, 31 read, previously reported as 87.
    """
    ens = ca.get("ensemble") or {}
    sel = ca.get("stage2_selection") or {}
    for value in (ens.get("analysed"), sel.get("selected")):
        if value is not None:
            return int(value)
    return None


def _coverage_label(ca: dict, engagement: dict) -> str:
    """Human-readable comment-coverage string, computed server-side.

    Distinguishes comments analysed by the model ensemble, comments stored in the DB,
    and total comments reported on the upstream platform.
    """
    sel = ca.get("stage2_selection") or {}
    ens = ca.get("ensemble") or {}
    stored = int((engagement or {}).get("stored_comments") or ca.get("analyzed") or 0)
    total = int((engagement or {}).get("comment_count") or 0)

    sel_skipped = sel.get("skipped", 0)
    analysed = _analysed_by_models(ca)

    if not stored and not total:
        return "—"

    # Did a model read every stored comment, or only some of them? Both a top-N
    # cap and the dedup / min-words filters cut the set, and the second pair used
    # to be invisible here: one post stored 87 rows, had 43 near-duplicates and 13
    # textless/too-short, and reported "87 stored analyzed" for 31 comments read.
    if analysed is not None and analysed < stored:
        prefix = "top " if sel_skipped and sel_skipped > 0 else ""
        head = f"{prefix}{analysed:,} of {stored:,} stored analyzed"
    elif total > 0 and stored >= total:
        head = f"✓ all {stored:,} analyzed (100%)"
    else:
        head = f"{stored:,} stored analyzed"

    if total > 0 and total > stored:
        head += f" · {round(stored / total * 100)}% of {total:,} total"
    elif total > 0 and stored > total:
        head += f" · more than the {total:,} reported (upstream mismatch)"

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
    # The list response is filtered by CommentAnalysisResult, which carries neither
    # `ensemble` nor `stage2_selection` — so the count behind the label has to be
    # exposed explicitly or the table can only ever show stored rows.
    ca["analysed_by_models"] = _analysed_by_models(ca)
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
        language_method=r.get("language_method"),
        watchlist_alert=r.get("watchlist_alert", False),
        watchlist_alert_reason=r.get("watchlist_alert_reason"),
        overall_sentiment=r.get("overall_sentiment", "neutral"),
        sentiment_score=r.get("sentiment_score", 0.0),
        text_sentiment=r.get("text_sentiment"),
        image_sentiment=r.get("image_sentiment"),
        baseline_sentiment=r.get("baseline_sentiment"),
        emotion=r.get("emotion"),
        intents=r.get("intents", []),
        topics=r.get("topics", []),
        insight=r.get("insight"),
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

                # "cancelled" terminates too — a stopped job publishes no
                # further frames, so without it the client would sit on the
                # stream until the 5-minute timeout.
                if event_type in ("done", "error", "cancelled"):
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
    tenant_id = current_user.get("tenant_id", "default")
    # Verify job exists before opening the stream
    job_row = (
        await db.execute(
            text("SELECT id FROM jobs WHERE id = :id AND (tenant_id = :tid OR selector->>'tenant_id' = :tid)"),
            {"id": analysis_id, "tid": tenant_id},
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

"""Ingest endpoints — trigger upstream sync or upload a batch of posts."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

import redis.asyncio as aioredis
from defense.services.api.deps import get_current_user, get_db, get_redis, resolve_llm_backend
from defense.services.api.models import IngestSyncRequest, JobResponse, UploadJobResponse, UploadRequest

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/v1", tags=["ingest"])

# Redis stream key that workers consume
_INGESTION_STREAM = "ingestion:queue"


async def _create_job(
    db: AsyncSession,
    job_id: str,
    job_type: str,
    selector: dict,
    options: dict | None = None,
    tenant_id: str = "default",
) -> None:
    """Insert a job record into the jobs table."""
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
            "id": job_id,
            "tenant_id": tenant_id,
            "type": job_type,
            "status": "pending",
            "selector": json.dumps(selector),
            "options": json.dumps(options or {}),
            "created_at": now,
            "updated_at": now,
        },
    )


async def _enqueue(redis: aioredis.Redis, payload: dict) -> str:
    """Push payload onto the Redis stream; returns the stream entry ID."""
    entry_id = await redis.xadd(
        _INGESTION_STREAM,
        {"data": json.dumps(payload, default=str)},
    )
    return entry_id


def _status_url(request: Request, job_id: str) -> str:
    base = str(request.base_url).rstrip("/")
    return f"{base}/v1/jobs/{job_id}"


# ---------------------------------------------------------------------------
# POST /v1/ingest/sync
# ---------------------------------------------------------------------------


@router.post(
    "/ingest/sync",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=JobResponse,
    summary="Trigger upstream sync for a campaign date range",
)
async def ingest_sync(
    body: IngestSyncRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
    current_user: dict = Depends(get_current_user),
) -> JobResponse:
    """Pull posts from the upstream API for *campaign_id* between
    *posted_from* and *posted_to*.  The job is persisted to Postgres and
    pushed onto the Redis stream for the ingestion worker to pick up.
    """
    job_id = str(uuid.uuid4())
    tenant_id = current_user.get("tenant_id", "default")

    selector: dict[str, Any] = {
        "campaign_id": body.campaign_id,
        "posted_from": body.posted_from.isoformat(),
        "posted_to": body.posted_to.isoformat(),
        "tenant_id": tenant_id,
    }

    try:
        await _create_job(db, job_id, "ingest_sync", selector, tenant_id=tenant_id)
        await _enqueue(
            redis,
            {
                "job_id": job_id,
                "job_type": "ingest_sync",
                "selector": selector,
                "tenant_id": tenant_id,
                "requested_by": current_user.get("sub"),
            },
        )
    except Exception as exc:
        log.error("ingest_sync_enqueue_failed", job_id=job_id, error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to enqueue ingestion job",
        ) from exc

    log.info("ingest_sync_enqueued", job_id=job_id, campaign_id=body.campaign_id)
    return JobResponse(
        job_id=job_id,
        status="pending",
        status_url=_status_url(request, job_id),
    )


# ---------------------------------------------------------------------------
# POST /v1/posts/upload
# ---------------------------------------------------------------------------


@router.post(
    "/posts/upload",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=UploadJobResponse,
    summary="Upload an inline batch of posts or reference an S3 object",
)
async def posts_upload(
    body: UploadRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
    current_user: dict = Depends(get_current_user),
) -> UploadJobResponse:
    """Accept a batch of PostWithDetails objects (inline JSON) or an S3 URI.

    The payload is validated, a job record is created in Postgres, and the
    job is pushed onto the ``ingestion:queue`` Redis stream.
    """
    job_id = str(uuid.uuid4())
    tenant_id = current_user.get("tenant_id", "default")

    options: dict[str, Any] = dict(body.options or {})
    options["tenant_id"] = tenant_id
    # Resolve the backend this job will ACTUALLY run on and enforce the tenant's
    # privacy lock against it, then stamp the decision into `options` so it
    # travels with the envelope. The workers have no database, so this is the
    # only layer that can make it (PROJECT_ASSESSMENT §13.5).
    options["llm_backend"] = await resolve_llm_backend(db, redis, current_user, options)

    # Count inline posts if provided
    post_count: int | None = len(body.posts) if body.posts is not None else None

    selector: dict[str, Any] = {"tenant_id": tenant_id}
    if body.object_uri:
        selector["object_uri"] = body.object_uri
    if body.posts is not None:
        selector["inline_count"] = post_count
        # Record the post ids so GET /v1/analysis/{job_id}?include=results
        # can join back to analysis_results for this upload.
        selector["post_ids"] = [p.get("id") for p in body.posts if p.get("id")]

    # Inline posts can be multi-MB in aggregate, so they are streamed straight
    # onto the ingestion queue rather than persisted into the job row.
    try:
        await _create_job(db, job_id, "posts_upload", selector, {"options": options}, tenant_id=tenant_id)
        if body.posts is not None:
            # One post per message, wrapped so job_id/options survive the queue
            # and the assembler can track this job's progress to completion.
            await redis.set(f"job:{job_id}:total", len(body.posts), ex=86_400)
            for post in body.posts:
                await _enqueue(
                    redis, {"job_id": job_id, "options": options, "post": post, "tenant_id": tenant_id}
                )
        elif body.object_uri:
            # Large batch: hand the worker an object reference to fetch.
            await _enqueue(
                redis,
                {
                    "job_id": job_id,
                    "job_type": "posts_upload",
                    "object_uri": body.object_uri,
                    "tenant_id": tenant_id,
                    "requested_by": current_user.get("sub"),
                },
            )
    except Exception as exc:
        log.error("posts_upload_enqueue_failed", job_id=job_id, error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to enqueue upload job",
        ) from exc

    log.info(
        "posts_upload_enqueued",
        job_id=job_id,
        count=post_count,
        object_uri=body.object_uri,
    )
    return UploadJobResponse(
        job_id=job_id,
        status="pending",
        count=post_count,
        status_url=_status_url(request, job_id),
    )


# ---------------------------------------------------------------------------
# DELETE /v1/posts/{post_id}
# ---------------------------------------------------------------------------


@router.delete(
    "/posts/{post_id}",
    summary="Delete a post and all derived data (analysis results, comments)",
)
async def delete_post(
    post_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> dict:
    """Hard-delete *post_id* and everything derived from it (api_design.md §5).

    Removes the post row, its flattened comments, and its analysis results.
    ClickHouse analytics events are append-only and age out via TTL/partition
    drops, matching the data-retention design.
    """
    # --- Tenant scoping (§P7.8): verify the post belongs to the caller's
    # tenant before touching anything.  Without this, any authenticated user
    # could delete another tenant's posts by ID.
    tenant_id = current_user.get("tenant_id", "default")
    ownership = (
        await db.execute(
            text(
                "SELECT 1 FROM posts p "
                "LEFT JOIN campaigns c ON p.campaign_id = c.id "
                "WHERE p.id = :pid AND (p.tenant_id = :tid OR c.tenant_id = :tid)"
            ),
            {"pid": post_id, "tid": tenant_id},
        )
    ).first()
    if ownership is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Post '{post_id}' not found",
        )

    deleted: dict[str, int] = {}
    
    # Delete comments for post
    res_comments = await db.execute(
        text("DELETE FROM comments WHERE post_id = :pid"),
        {"pid": post_id},
    )
    deleted["comments"] = res_comments.rowcount or 0

    # Delete analysis_results scoped to tenant
    res_analysis = await db.execute(
        text("DELETE FROM analysis_results WHERE post_id = :pid AND tenant_id = :tid"),
        {"pid": post_id, "tid": tenant_id},
    )
    deleted["analysis_results"] = res_analysis.rowcount or 0

    # Delete post scoped to tenant
    res_posts = await db.execute(
        text("DELETE FROM posts WHERE id = :pid AND tenant_id = :tid"),
        {"pid": post_id, "tid": tenant_id},
    )
    deleted["posts"] = res_posts.rowcount or 0

    await db.commit()
    log.info("post_deleted", post_id=post_id, tenant_id=tenant_id, **deleted)
    return {"post_id": post_id, "deleted": deleted}



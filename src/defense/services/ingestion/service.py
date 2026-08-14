"""
Ingestion service — async Redis stream consumer.

Reads from "ingestion:queue", normalizes each PostWithDetails payload, dedupes
via content-hash, writes the canonical record to Postgres, then enqueues the
normalized post to "nlp:stage1:queue" for the NLP pipeline.

Architectural notes
-------------------
- Golden rule #4: no write-back to the upstream system.
- Golden rule #5: content-hash dedup via Redis SET NX.
- The service never crashes on a single bad message; errors are logged and the
  consumer moves on.  The original message is ACK'd even on validation errors
  so a permanently-bad message does not block the stream.
- The NLP queue message contains the full normalized post as JSON, keyed by
  "payload", so downstream workers can XREAD without further unpacking.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from typing import Any

import structlog
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy import text

# Ensure libs directory is importable when running as a package or directly.

from defense.libs.common.config import get_settings
from defense.libs import streams
from defense.libs.common import content_hash
from defense.libs.dlq import record_failure
from defense.libs.embeddings import embed_text, to_pgvector_literal
from defense.libs.progress import publish_stage
from defense.libs.repos.posts import PostRepository
from .normalizer import normalize_post

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

from defense.libs.common.logging import setup_logging  # noqa: E402

setup_logging("ingestion")
log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# From libs/streams.py — the single source of truth the KEDA manifests are
# checked against (§5.1 / §5.5).
INGESTION_STREAM = streams.INGESTION.name
NLP_STREAM = streams.STAGE1_NLP.name
CONSUMER_GROUP = streams.INGESTION.group
# Near-duplicate reuse hands the post straight to the assembler, skipping
# Stage 1 and Stage 2 — see _enqueue_reuse (§13.3).
ASSEMBLER_STREAM = streams.ASSEMBLER.name
CONSUMER_NAME = "ingestion-service-1"

# XREAD block timeout in milliseconds (1 second).  This lets the loop wake up
# periodically even when the stream is idle so shutdown signals can be handled.
XREAD_BLOCK_MS = 1000

# How many messages to fetch in a single XREAD call.
XREAD_COUNT = 10

# Redis TTL for dedup keys (7 days in seconds).
DEDUP_TTL_SECONDS = 7 * 24 * 3600

# Near-duplicate reuse (architecture §3.2): after the exact-hash miss, embed the
# caption and reuse a prior analysis whose embedding is within cosine threshold,
# skipping Stage-1/2 entirely. High threshold so only true near-dups trigger.
_settings = get_settings()
NEAR_DUP_ENABLED = str(getattr(_settings, "near_dup_dedup", "false")).lower() == "true"
NEAR_DUP_THRESHOLD = float(getattr(_settings, "near_dup_threshold", 0.95))

# Bounded retry before a failed message is dead-lettered to ingestion:queue:dlq (§8).
INGESTION_MAX_RETRIES = int(_settings.ingestion_max_retries)

# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def _parse_dt(value: Any) -> datetime | None:
    """Coerce an upstream ISO-8601 timestamp string into a datetime.

    asyncpg requires a ``datetime`` instance (not a ``str``) for TIMESTAMPTZ
    columns. Returns ``None`` for missing/unparseable values.
    """
    if value is None or isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None

async def _upsert_post(
    session: AsyncSession,
    normalized: dict,
    raw_payload: dict,
    tenant_id: str = "default",
) -> None:
    """
    Upsert the normalized post and its comments into the Postgres posts table using PostRepository.
    """
    repo = PostRepository(session)
    await repo.upsert_post(normalized, raw_payload, tenant_id=tenant_id)


# ---------------------------------------------------------------------------
# Dedup helpers
# ---------------------------------------------------------------------------

async def _is_duplicate(redis: Redis, hash_value: str) -> bool:
    """
    Attempt to claim a dedup key via SET NX.

    Returns True  if the key already existed (duplicate — skip).
    Returns False if we set the key (first time we've seen this hash).

    The key expires after DEDUP_TTL_SECONDS so the dedup window is bounded.
    """
    # SET key value NX EX ttl returns True when the key was newly set.
    was_set = await redis.set(
        f"dedup:{hash_value}",
        "1",
        nx=True,
        ex=DEDUP_TTL_SECONDS,
    )
    return was_set is None  # None means key already existed → duplicate


async def _find_near_duplicate(
    session: AsyncSession,
    caption: str,
    campaign_id: str | None,
    tenant_id: str = "default",
) -> tuple[str, float] | None:
    """Return (source_post_id, score) of a prior analysis within cosine
    threshold of this caption's embedding, or None. Scoped to the campaign and tenant."""
    qvec = to_pgvector_literal(embed_text(caption))
    params: dict[str, Any] = {"qvec": qvec, "tid": tenant_id}
    
    if campaign_id:
        params["cid"] = campaign_id
        sql = text(
            """
            SELECT ar.post_id                                    AS post_id,
                   1 - (ar.embedding <=> CAST(:qvec AS vector))  AS score
            FROM analysis_results ar
            WHERE ar.embedding IS NOT NULL AND ar.campaign_id = :cid AND ar.tenant_id = :tid
            ORDER BY ar.embedding <=> CAST(:qvec AS vector)
            LIMIT 1
            """
        )
    else:
        sql = text(
            """
            SELECT ar.post_id                                    AS post_id,
                   1 - (ar.embedding <=> CAST(:qvec AS vector))  AS score
            FROM analysis_results ar
            WHERE ar.embedding IS NOT NULL AND ar.tenant_id = :tid
            ORDER BY ar.embedding <=> CAST(:qvec AS vector)
            LIMIT 1
            """
        )

    row = (await session.execute(sql, params)).mappings().first()
    if row and row["score"] is not None and float(row["score"]) >= NEAR_DUP_THRESHOLD:
        return row["post_id"], float(row["score"])
    return None


async def _fetch_source_analysis(session: AsyncSession, src_post_id: str) -> dict | None:
    """The prior analysis to reuse, plus the provenance of its vector.

    Returns ``{"result": <canonical>, "embedding": [...], "embedding_is_stub": bool}``
    or None. ``embedding_is_stub`` is read explicitly because dropping it is
    exactly what the old row-copy did — the copy inherited the column DEFAULT
    ``FALSE`` and so claimed a hash stub was a real semantic vector (§13.3).
    """
    row = (
        await session.execute(
            text(
                """
                SELECT ar.result                              AS result,
                       ar.embedding::text                     AS emb,
                       COALESCE(ar.embedding_is_stub, FALSE)  AS is_stub
                FROM analysis_results ar
                WHERE ar.post_id = :src
                ORDER BY ar.created_at DESC
                LIMIT 1
                """
            ),
            {"src": src_post_id},
        )
    ).mappings().first()
    if not row or not row["result"]:
        return None

    from defense.libs.clustering import parse_pgvector  # noqa: PLC0415

    return {
        "result": row["result"],
        "embedding": parse_pgvector(row["emb"]),
        "embedding_is_stub": bool(row["is_stub"]),
    }


async def _enqueue_reuse(
    redis: Redis,
    normalized: dict,
    source: dict,
    src_post_id: str,
    score: float,
    job_id: str | None,
    tenant_id: str = "default",
) -> None:
    """Hand a near-duplicate to the ASSEMBLER rather than copying its DB row.

    Reuse used to be an ``INSERT … SELECT`` straight into ``analysis_results``,
    which wrote **Postgres only**: no ClickHouse row and no object-storage blob.
    Reused posts were therefore absent from every analytics aggregate (trend,
    top posts, reaction mix, sentiment-over-time) while still counting in the
    Postgres-backed reports — the two stores disagreed by construction (§13.3).

    The assembler already owns the three-store fan-out, schema validation and
    job accounting, so routing through it fixes the store gap and the identity
    rewrite in one move, without duplicating any of that here. Stage 1 and
    Stage 2 are still skipped, which is the whole point of the feature.
    """
    envelope = {
        "post_id": normalized["post_id"],
        "normalized_post": normalized,
        # No stage1_result: the assembler composes the document from the source
        # analysis plus THIS post's facts (builder.build_reused_result).
        "stage1_result": {},
        "stage2_result": None,
        "reused_result": source["result"],
        "reused_from": {"source_post_id": src_post_id, "score": round(float(score), 4)},
        "embedding": source.get("embedding"),
        "embedding_is_stub": source.get("embedding_is_stub", True),
        "job_id": job_id,
        "tenant_id": tenant_id,
    }
    await redis.xadd(
        ASSEMBLER_STREAM,
        {"data": json.dumps(envelope, ensure_ascii=False, default=str)},
    )


# ---------------------------------------------------------------------------
# NLP enqueue helper
# ---------------------------------------------------------------------------

async def _enqueue_nlp(
    redis: Redis,
    normalized: dict,
    raw: dict,
    job_id: str | None = None,
    options: dict | None = None,
    tenant_id: str = "default",
) -> None:
    """
    Push the post onto the NLP stage-1 stream as the canonical envelope.

    Every inter-stage message is a single ``data`` field holding a JSON
    envelope. Stage-1 consumes ``raw_post`` (upstream keys); ``normalized_post``
    is carried through untouched for the assembler/builder. ``job_id`` ties the
    post back to its upload/analysis job so the assembler can track progress;
    ``options`` carries per-request flags the router reads (want_summary, …).
    """
    envelope = {
        "post_id": normalized["post_id"],
        "raw_post": raw,
        "normalized_post": normalized,
        "options": options or {},
        "job_id": job_id,
        "tenant_id": tenant_id,
    }
    await redis.xadd(
        NLP_STREAM,
        {"data": json.dumps(envelope, ensure_ascii=False)},
    )


    # Opening frame of the dashboard's per-post trace: what normalization
    # derived, before any model has seen the post.
    engagement = normalized.get("engagement") or {}
    await publish_stage(
        redis,
        "ingest",
        "done",
        job_id=job_id,
        post_id=normalized["post_id"],
        detail={
            "platform": normalized.get("platform"),
            "media_type": normalized.get("media_type"),
            "caption_chars": len(normalized.get("caption") or ""),
            "photo_count": len(normalized.get("photo_urls") or []),
            "comment_rows": len(normalized.get("comments") or []),
            "comment_count": engagement.get("comment_count"),
            "coverage": engagement.get("coverage"),
            "content_hash": (normalized.get("content_hash") or "")[:16],
            "baseline_sentiment": normalized.get("baseline_sentiment"),
            "next_stream": NLP_STREAM,
        },
        log=log,
    )


async def _count_skipped_for_job(
    redis: Redis,
    session_factory: async_sessionmaker,
    job_id: str,
    post_id: str,
    reason: str,
) -> None:
    """Count a post that will never reach the assembler (e.g. dedup-skipped)
    toward its job's completion so the job can still finish."""
    try:
        await redis.incr(f"job:{job_id}:completed")
        await redis.expire(f"job:{job_id}:completed", 86_400)
        completed = int(await redis.get(f"job:{job_id}:completed") or 0)
        failed = int(await redis.get(f"job:{job_id}:failed") or 0)
        raw_total = await redis.get(f"job:{job_id}:total")
        total = int(raw_total) if raw_total else None
        event = {
            "event": "done" if total is not None and completed + failed >= total else "progress",
            "job_id": job_id,
            "post_id": post_id,
            "completed": completed,
            "failed": failed,
            "total": total,
            "note": reason,
        }
        await redis.publish(f"analysis:progress:{job_id}", json.dumps(event))
        if total is not None and completed + failed >= total:
            await _set_job_status(session_factory, job_id, "done")
    except Exception as exc:
        log.warning("job_skip_count_failed", job_id=job_id, error=str(exc))


# ---------------------------------------------------------------------------
# Control messages (ingest_sync pull jobs)
# ---------------------------------------------------------------------------

async def _set_job_status(
    session_factory: async_sessionmaker,
    job_id: str,
    status: str,
    error: str | None = None,
) -> None:
    try:
        async with session_factory() as session:
            await session.execute(
                text(
                    "UPDATE jobs SET status = :status, error = :error, updated_at = NOW() "
                    "WHERE id = :id"
                ),
                {"id": job_id, "status": status, "error": error},
            )
            await session.commit()
    except Exception as exc:
        log.warning("job_status_update_failed", job_id=job_id, error=str(exc))


async def _handle_control_message(
    redis: Redis,
    session_factory: async_sessionmaker,
    msg: dict,
) -> None:
    """Handle a non-post control message from the ingestion queue.

    ``ingest_sync`` (POST /v1/ingest/sync): pull post-with-details payloads
    from the upstream platform API for a campaign/date-range, then feed each
    post back through this queue (wrapped with the job_id) so the normal
    dedup/normalize/enqueue path applies. Requires UPSTREAM_API_URL; without
    it the job fails fast with an actionable error.
    """
    job_id: str | None = msg.get("job_id")
    job_type: str = msg.get("job_type", "")

    if job_type != "ingest_sync":
        log.warning("ingestion.unknown_control_message", job_type=job_type, job_id=job_id)
        return

    settings = get_settings()
    if not settings.upstream_api_url:
        log.error("ingest_sync_unconfigured", job_id=job_id)
        if job_id:
            await _set_job_status(
                session_factory, job_id, "failed",
                "UPSTREAM_API_URL is not configured — set it (and UPSTREAM_API_KEY) "
                "to enable upstream pulls, or use POST /v1/posts/upload instead.",
            )
        return

    selector: dict = msg.get("selector") or {}
    params = {
        "campaignId": selector.get("campaign_id"),
        "postedFrom": selector.get("posted_from"),
        "postedTo": selector.get("posted_to"),
    }
    params = {k: v for k, v in params.items() if v}

    import httpx  # local import: only the pull path needs it

    headers = {}
    if settings.upstream_api_key:
        headers["Authorization"] = f"Bearer {settings.upstream_api_key}"

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.get(
                settings.upstream_api_url.rstrip("/") + "/posts-with-details",
                params=params,
                headers=headers,
            )
            resp.raise_for_status()
            payload = resp.json()
    except Exception as exc:
        log.error("ingest_sync_pull_failed", job_id=job_id, error=str(exc))
        if job_id:
            await _set_job_status(
                session_factory, job_id, "failed", f"Upstream pull failed: {exc}"
            )
        return

    posts = payload.get("posts") if isinstance(payload, dict) else payload
    if not isinstance(posts, list):
        if job_id:
            await _set_job_status(
                session_factory, job_id, "failed",
                "Upstream response did not contain a post list",
            )
        return

    if job_id:
        await redis.set(f"job:{job_id}:total", len(posts), ex=86_400)
        await _set_job_status(session_factory, job_id, "running")
    for post in posts:
        await redis.xadd(
            INGESTION_STREAM,
            {"data": json.dumps({"job_id": job_id, "options": {}, "post": post}, ensure_ascii=False)},
        )
    log.info("ingest_sync_pulled", job_id=job_id, posts=len(posts))


# ---------------------------------------------------------------------------
# Message processing
# ---------------------------------------------------------------------------

async def _process_message(
    redis: Redis,
    session_factory: async_sessionmaker,
    stream_name: str,
    message_id: bytes,
    fields: dict[bytes, bytes],
) -> None:
    """
    Process a single stream message end-to-end.

    Steps:
        1. Decode and parse the raw JSON payload.
        2. Compute content hash from raw payload.
        3. Dedup check via Redis SET NX.
        4. Normalize via normalize_post().
        5. Upsert into Postgres (id + raw_payload).
        6. Enqueue to NLP stream.
        7. ACK the message.
    """
    msg_id_str = message_id.decode() if isinstance(message_id, bytes) else message_id

    # Step 1 — decode raw JSON.
    raw_json: str | None = None
    for candidate_key in (b"payload", b"data", b"post"):
        if candidate_key in fields:
            raw_json_bytes = fields[candidate_key]
            raw_json = (
                raw_json_bytes.decode("utf-8")
                if isinstance(raw_json_bytes, bytes)
                else raw_json_bytes
            )
            break

    if raw_json is None:
        # Try the first value if none of the expected keys matched.
        if fields:
            first_val = next(iter(fields.values()))
            raw_json = (
                first_val.decode("utf-8")
                if isinstance(first_val, bytes)
                else first_val
            )

    if not raw_json:
        log.warning("ingestion.empty_message", message_id=msg_id_str)
        # ACK so the empty message doesn't sit in the PEL forever (§P7.9).
        await redis.xack(stream_name, CONSUMER_GROUP, message_id)
        return

    raw: dict[str, Any] = json.loads(raw_json)

    # Control messages (ingest_sync pull jobs from POST /v1/ingest/sync) carry a
    # job_type instead of post fields. The upstream pull needs UPSTREAM_API_URL;
    # without it the job is failed fast with a clear error instead of being
    # mis-parsed as a post.
    if isinstance(raw, dict) and raw.get("job_type"):
        await _handle_control_message(redis, session_factory, raw)
        await redis.xack(stream_name, CONSUMER_GROUP, message_id)
        return

    # Upload envelopes wrap the raw post so job_id/options survive the queue:
    #   {"job_id": ..., "options": {...}, "post": {<raw post>}}
    # Bare raw posts (legacy producers) still work.
    job_id: str | None = None
    options: dict[str, Any] = {}
    if isinstance(raw, dict) and "post" in raw and ("job_id" in raw or "options" in raw):
        job_id = raw.get("job_id")
        options = raw.get("options") or {}
        raw = raw["post"]

    post_id: str = raw.get("id", "<unknown>")

    # One line per ingestion step below, so a post that never reaches Stage 1 can
    # be pinned to the step that stopped it (dedup, normalization, near-dup reuse)
    # instead of just going quiet.
    log.info(
        "ingestion.received",
        post_id=post_id,
        job_id=job_id,
        post_type=raw.get("postType"),
        caption_chars=len(raw.get("caption") or ""),
        comments=len(raw.get("comments") or []),
        photos=len(raw.get("photoUrls") or []),
        message_id=msg_id_str,
    )

    # Step 2 — compute content hash from the raw (upstream) payload.
    hash_value: str = content_hash(raw)

    # Step 3 — dedup check.
    if await _is_duplicate(redis, hash_value):
        log.info(
            "ingestion.duplicate_skipped",
            post_id=post_id,
            content_hash=hash_value,
            message_id=msg_id_str,
        )
        # The post never reaches the assembler, so count it toward the job's
        # completion here — otherwise a re-upload would leave the job pending.
        if job_id:
            await _count_skipped_for_job(
                redis, session_factory, job_id, post_id, "duplicate_skipped"
            )
        # ACK so we don't re-process this message.
        await redis.xack(stream_name, CONSUMER_GROUP, message_id)
        return

    # Step 4 — normalize.
    normalized: dict = normalize_post(raw)
    log.info(
        "ingestion.normalized",
        post_id=post_id,
        platform=normalized.get("platform"),
        media_type=normalized.get("media_type"),
        caption_chars=len(normalized.get("caption") or ""),
        comment_rows=len(normalized.get("comments") or []),
        coverage=(normalized.get("engagement") or {}).get("coverage"),
        content_hash=hash_value[:16],
    )

    # Step 5 — upsert into Postgres.
    tenant_id: str = options.get("tenant_id") or raw.get("tenant_id") or raw.get("tenantId") or "default"
    async with session_factory() as session:
        await _upsert_post(session, normalized, raw, tenant_id=tenant_id)
    log.debug("ingestion.upserted", post_id=post_id)

    # Step 5b — near-duplicate reuse (§3.2): if this caption is within cosine
    # threshold of an already-analyzed post, copy that result and skip Stage-1/2
    # entirely. Best-effort — any failure falls back to normal processing.
    caption = normalized.get("caption")
    if NEAR_DUP_ENABLED and caption:
        try:
            async with session_factory() as session:
                dup = await _find_near_duplicate(session, caption, raw.get("campaignId"), tenant_id=tenant_id)
                source = await _fetch_source_analysis(session, dup[0]) if dup else None
                if source:
                    # Hand it to the assembler rather than copying the row here.
                    # The assembler owns schema validation and the three-store
                    # fan-out; the old row-copy wrote Postgres only, so reused
                    # posts were missing from every ClickHouse aggregate (§13.3).
                    await _enqueue_reuse(redis, normalized, source, dup[0], dup[1], job_id, tenant_id=tenant_id)
                    log.info(
                        "ingestion.near_duplicate_reused",
                        post_id=post_id,
                        source_post_id=dup[0],
                        score=round(dup[1], 4),
                        message_id=msg_id_str,
                        note="post-level analysis only; comment thread not reused",
                    )
                    # NOT counted as skipped: unlike the dedup path, this post
                    # DOES reach the assembler, which does its own job
                    # accounting. Counting it here as well would complete the
                    # job one post early.
                    await redis.xack(stream_name, CONSUMER_GROUP, message_id)
                    return
        except Exception as exc:
            log.warning("ingestion.near_dup_check_failed", post_id=post_id, error=str(exc))

    # Step 6 — enqueue to NLP stage-1 stream.
    await _enqueue_nlp(redis, normalized, raw, job_id=job_id, options=options, tenant_id=tenant_id)


    # Step 7 — ACK the original message.
    await redis.xack(stream_name, CONSUMER_GROUP, message_id)

    log.info(
        "ingestion.success",
        post_id=post_id,
        platform=normalized["platform"],
        content_hash=hash_value,
        comment_count=normalized["engagement"].get("comment_count", 0),
        message_id=msg_id_str,
    )


# ---------------------------------------------------------------------------
# Consumer loop
# ---------------------------------------------------------------------------

async def _ensure_consumer_group(redis: Redis) -> None:
    """
    Create the consumer group if it does not already exist.

    Uses **"0"** as the start ID, so a group created on a stream that already has
    entries picks up that backlog rather than skipping it — which is what a
    restart after a queued upload needs.

    (This docstring used to claim ``"$"``, i.e. the opposite behaviour, and
    offered ``"0"`` as the change to make for reprocessing — advice describing
    the state the code was already in. PROJECT_ASSESSMENT §13.7c.)
    """
    try:
        await redis.xgroup_create(
            INGESTION_STREAM,
            CONSUMER_GROUP,
            id="0",
            mkstream=True,
        )
        log.info(
            "ingestion.group_created",
            stream=INGESTION_STREAM,
            group=CONSUMER_GROUP,
        )
    except Exception as exc:
        # BUSYGROUP means the group already exists — that is fine.
        if "BUSYGROUP" in str(exc):
            log.debug(
                "ingestion.group_exists",
                stream=INGESTION_STREAM,
                group=CONSUMER_GROUP,
            )
        else:
            raise


async def run_service() -> None:
    """
    Main service loop.

    Connects to Redis and Postgres, then continuously reads from the ingestion
    stream, processing each message until the process is interrupted.
    """
    settings = get_settings()

    log.info(
        "ingestion.starting",
        redis_url=settings.redis_url,
        stream=INGESTION_STREAM,
    )

    # Build async Redis client.
    redis = Redis.from_url(settings.redis_url, decode_responses=False)

    # Build async SQLAlchemy engine (asyncpg driver).
    db_url = settings.database_url
    # SQLAlchemy asyncpg requires postgresql+asyncpg:// scheme.
    if db_url.startswith("postgresql://") or db_url.startswith("postgres://"):
        db_url = db_url.replace("postgresql://", "postgresql+asyncpg://", 1)
        db_url = db_url.replace("postgres://", "postgresql+asyncpg://", 1)

    engine = create_async_engine(db_url, echo=False, pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    await _ensure_consumer_group(redis)

    log.info("ingestion.ready", stream=INGESTION_STREAM, consumer=CONSUMER_NAME)

    try:
        while True:
            try:
                # XREADGROUP reads pending (not yet ACK'd) messages first when
                # using ">" as the ID, plus new messages in the stream.
                results = await redis.xreadgroup(
                    CONSUMER_GROUP,
                    CONSUMER_NAME,
                    {INGESTION_STREAM: ">"},
                    count=XREAD_COUNT,
                    block=XREAD_BLOCK_MS,
                )
            except Exception as exc:
                log.error(
                    "ingestion.xreadgroup_error",
                    error=str(exc),
                    exc_info=True,
                )
                if "NOGROUP" in str(exc):
                    # Stream/group wiped at runtime (e.g. FLUSHALL) — re-create
                    # the group instead of error-looping forever.
                    try:
                        await _ensure_consumer_group(redis)
                    except Exception as group_exc:
                        log.error("ingestion.group_recreate_failed", error=str(group_exc))
                # Brief back-off before retrying to avoid a tight error loop.
                await asyncio.sleep(2)
                continue

            if not results:
                # Timeout — no new messages; loop and wait again.
                continue

            for stream_name, messages in results:
                stream_name_str = (
                    stream_name.decode()
                    if isinstance(stream_name, bytes)
                    else stream_name
                )
                for message_id, fields in messages:
                    try:
                        await _process_message(
                            redis,
                            session_factory,
                            stream_name_str,
                            message_id,
                            fields,
                        )
                    except Exception as exc:
                        # Log and continue — never crash the worker.
                        msg_id_str = (
                            message_id.decode()
                            if isinstance(message_id, bytes)
                            else str(message_id)
                        )
                        log.error(
                            "ingestion.message_error",
                            message_id=msg_id_str,
                            error=str(exc),
                            exc_info=True,
                        )
                        # Bounded retry, then dead-letter to
                        # ingestion:queue:dlq (§8) — never silently drop a bad
                        # message (record_failure ACKs the original).
                        try:
                            outcome = await record_failure(
                                redis,
                                stream=stream_name_str,
                                group=CONSUMER_GROUP,
                                msg_id=message_id,
                                fields=fields,
                                error=exc,
                                max_retries=INGESTION_MAX_RETRIES,
                            )
                            log.warning(
                                "ingestion.failure_handled",
                                message_id=msg_id_str,
                                outcome=outcome,
                            )
                        except Exception as dlq_exc:
                            log.error(
                                "ingestion.dlq_error",
                                message_id=msg_id_str,
                                error=str(dlq_exc),
                            )

    except (KeyboardInterrupt, asyncio.CancelledError):
        log.info("ingestion.shutdown")
    finally:
        await redis.aclose()
        await engine.dispose()
        log.info("ingestion.stopped")

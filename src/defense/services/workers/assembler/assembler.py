"""
Async Redis stream consumer — the Result Assembler.

Reads from the "assembler:queue" stream, builds the canonical result,
validates it, persists to all four storage backends in parallel, and
publishes a completion event.

Message format expected on the stream
--------------------------------------
Each Redis stream message must contain the following fields (all JSON-encoded
strings):

    post_id          – string, the post CUID
    normalized_post  – JSON object (the normalized upstream post)
    stage1_result    – JSON object (Stage-1 NLP output)
    stage2_result    – JSON object or "null" / absent (Stage-2 LLM output)
    job_id           – string or absent (the Postgres jobs.id to update)
"""

from __future__ import annotations

import asyncio
import json
import os
from defense.libs.common.config import get_settings
config = get_settings()
import sys
import time
from typing import Any

import structlog

# Allow the libs package to be imported when running as a standalone service.

from defense.services.workers.assembler.builder import build_canonical_result, build_reused_result
from defense.services.workers.assembler.persistence import (
    persist_clickhouse,
    persist_minio,
    persist_postgres,
)

# ---------------------------------------------------------------------------
# Logging — everything funnels into loguru (see libs/common/logging.py)
# ---------------------------------------------------------------------------
from defense.libs.common.logging import setup_logging  # noqa: E402
from defense.libs.dlq import record_failure  # noqa: E402
from defense.libs.progress import publish_stage  # noqa: E402

from defense.libs import streams  # noqa: E402

setup_logging("assembler")
log: structlog.BoundLogger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Redis stream constants
# ---------------------------------------------------------------------------
# From libs/streams.py — the single source of truth the KEDA manifests are
# checked against (§5.1 / §5.5).
STREAM_KEY = streams.ASSEMBLER.name
CONSUMER_GROUP = streams.ASSEMBLER.group

# Bounded retry before a failed message is dead-lettered to assembler:queue:dlq.
ASSEMBLER_MAX_RETRIES = int(config.assembler_max_retries)
CONSUMER_NAME = f"assembler-{os.getpid()}"
BLOCK_MS = 5_000          # block this long when the stream is empty
BATCH_SIZE = 10           # messages per XREADGROUP call
ACK_BATCH: list[str] = []  # pending ACK ids, flushed after each message loop

# ---------------------------------------------------------------------------
# Postgres job status constants
# ---------------------------------------------------------------------------
_JOB_STATUS_DONE = "done"
_JOB_STATUS_FAILED = "failed"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _decode_message(fields: dict[bytes, bytes]) -> dict[str, Any]:
    """Decode raw Redis stream fields (bytes) into a Python dict."""
    decoded: dict[str, Any] = {}
    for k, v in fields.items():
        key = k.decode("utf-8") if isinstance(k, bytes) else k
        val = v.decode("utf-8") if isinstance(v, bytes) else v
        decoded[key] = val
    return decoded


def _embedding_is_stub(result: dict, stage1_result: dict) -> bool:
    """Whether the Stage-1 vector for this post is the deterministic hash stub.

    **Read the producer's answer; do not infer one.** Stage 1 reports
    ``embedding_is_stub`` beside ``embedding`` (``text_analyzer.
    _embed_with_provenance``) because it is the only layer that knows — the stub
    is EMBEDDING_DIM-sized exactly like a real vector, so nothing downstream can
    tell them apart. Two separate places used to guess, and disagreed:
    persistence classified by dimension (recording every stub as real), while
    this module read ``processing.stub_mode``. PROJECT_ASSESSMENT §13.2.

    The two fallbacks below are for messages produced before Stage 1 carried the
    flag, in the same order of trustworthiness: the run-wide stub_mode marker,
    then "there is no vector at all", which means persistence will substitute its
    own post_id-seeded stub.
    """
    explicit = stage1_result.get("embedding_is_stub")
    if explicit is not None:
        return bool(explicit)
    return bool(
        (result.get("processing") or {}).get("stub_mode")
        or not stage1_result.get("embedding")
    )


def _parse_message(raw: dict[str, Any]) -> tuple[str, dict, dict, dict | None, str | None, dict]:
    """Parse and JSON-decode the fields of a single stream message.

    Returns
    -------
    (post_id, normalized_post, stage1_result, stage2_result, job_id, env)

    The whole envelope comes back as the last element because the
    near-duplicate reuse path carries extra keys (``reused_result``,
    ``reused_from``, ``embedding``) that only that path reads.
    """
    # Every inter-stage message is a single ``data`` field holding a JSON
    # envelope: {post_id, normalized_post, stage1_result, stage2_result, ...}.
    env: dict = json.loads(raw["data"])

    post_id: str = env["post_id"]
    normalized_post: dict = env["normalized_post"]
    stage1_result: dict = env.get("stage1_result") or {}
    stage2_result: dict | None = env.get("stage2_result")
    job_id: str | None = env.get("job_id") or None

    return post_id, normalized_post, stage1_result, stage2_result, job_id, env


async def _update_job_status(
    engine,
    job_id: str,
    status: str,
    error: str | None = None,
) -> None:
    """Update the jobs table row for job_id with the given status.

    A cancelled job is never re-opened. Stopping a job cannot recall the posts
    already inside a stage, and those still land here and are persisted (their
    LLM spend is already paid) — but if this UPDATE were unguarded, the last
    in-flight post would write the row straight back to "running", or to "done"
    once the counters happened to line up, and the Jobs tab would show a job the
    operator stopped as one that completed normally.
    """
    from sqlalchemy import text

    sql = text(
        """
        UPDATE jobs
        SET    status     = :status,
               updated_at = NOW(),
               error      = :error
        WHERE  id = :job_id
          AND  status <> 'cancelled'
        """
    )
    try:
        async with engine.begin() as conn:
            await conn.execute(
                sql,
                {"status": status, "job_id": job_id, "error": error},
            )
    except Exception as exc:
        log.warning("job_status_update_failed", job_id=job_id, exc=str(exc))


_PROGRESS_TTL = 86_400  # job counters expire after a day


async def _track_job_progress(
    redis_client,
    engine,
    job_id: str,
    *,
    post_id: str,
    failed: bool = False,
    error: str | None = None,
    sentiment: str | None = None,
) -> None:
    """Update per-job Redis counters, publish an SSE progress event on
    ``analysis:progress:{job_id}``, and flip the jobs row to its final status
    once every enqueued post has landed (the producer records the expected
    count in ``job:{job_id}:total``)."""
    field = "failed" if failed else "completed"
    try:
        await redis_client.incr(f"job:{job_id}:{field}")
        await redis_client.expire(f"job:{job_id}:{field}", _PROGRESS_TTL)
        completed = int(await redis_client.get(f"job:{job_id}:completed") or 0)
        failed_n = int(await redis_client.get(f"job:{job_id}:failed") or 0)
        raw_total = await redis_client.get(f"job:{job_id}:total")
        total = int(raw_total) if raw_total else None
    except Exception as exc:
        log.warning("job_progress_counter_failed", job_id=job_id, exc=str(exc))
        # Counters unavailable — fall back to the legacy first-post behaviour.
        await _update_job_status(
            engine, job_id, _JOB_STATUS_FAILED if failed else _JOB_STATUS_DONE, error
        )
        return

    finished = total is not None and (completed + failed_n) >= total

    event: dict[str, Any] = {
        "event": "done" if finished else "progress",
        "job_id": job_id,
        "post_id": post_id,
        "completed": completed,
        "failed": failed_n,
        "total": total,
    }
    if sentiment:
        event["overall_sentiment"] = sentiment
    if error:
        event["error"] = error
    try:
        await redis_client.publish(
            f"analysis:progress:{job_id}", json.dumps(event, ensure_ascii=False)
        )
    except Exception as exc:
        log.warning("job_progress_publish_failed", job_id=job_id, exc=str(exc))

    if total is None:
        # Producer recorded no total (legacy path) — first landing flips the job.
        await _update_job_status(
            engine, job_id, _JOB_STATUS_FAILED if failed else _JOB_STATUS_DONE, error
        )
    elif finished:
        final = _JOB_STATUS_DONE if completed > 0 else _JOB_STATUS_FAILED
        await _update_job_status(engine, job_id, final, error if completed == 0 else None)
    else:
        await _update_job_status(engine, job_id, "running")


async def _ensure_consumer_group(redis_client) -> None:
    """Create the consumer group if it does not already exist."""
    try:
        await redis_client.xgroup_create(
            STREAM_KEY, CONSUMER_GROUP, id="0", mkstream=True
        )
        log.info("redis: consumer group created", group=CONSUMER_GROUP, stream=STREAM_KEY)
    except Exception as exc:
        # BUSYGROUP means the group already exists — that is fine.
        if "BUSYGROUP" in str(exc):
            log.debug("redis: consumer group already exists", group=CONSUMER_GROUP)
        else:
            raise


# ---------------------------------------------------------------------------
# Single-message processing
# ---------------------------------------------------------------------------

async def _process_message(
    msg_id: str,
    fields: dict[bytes, bytes],
    *,
    engine,
    ch_client,
    s3_client,
    bucket: str,
    redis_client,
) -> None:
    """Process one stream message end-to-end.

    Steps
    -----
    1. Parse the message payload.
    2. Build and validate the canonical result.
    3. Persist to all storage backends in parallel.
    4. Publish to pubsub channel ``analysis:done:{post_id}``.
    5. Update job status in Postgres (if job_id present).
    6. ACK the message.
    7. Emit a structured log line.
    """
    t0 = time.monotonic()

    raw = _decode_message(fields)
    post_id, normalized_post, stage1_result, stage2_result, job_id, env = _parse_message(raw)

    bound_log = log.bind(post_id=post_id, msg_id=msg_id)

    # --- Build & validate ---------------------------------------------------
    # A near-duplicate carries a prior analysis instead of Stage-1/2 output.
    # It is composed rather than copied: the post's own identity, engagement and
    # reactions come from THIS post, and its comment thread is reported
    # unanalysed rather than inheriting the source's labels (§13.3).
    reused_result = env.get("reused_result")
    try:
        if reused_result:
            reuse_meta = env.get("reused_from") or {}
            result = build_reused_result(
                normalized_post,
                reused_result,
                reuse_meta.get("source_post_id", "unknown"),
                reuse_meta.get("score", 0.0),
            )
        else:
            result = build_canonical_result(
                normalized_post, stage1_result, stage2_result, job_id=job_id
            )
        result["tenant_id"] = env.get("tenant_id") or normalized_post.get("tenant_id") or "default"

    except (ValueError, KeyError) as exc:
        bound_log.error("build_failed", error=str(exc))
        if job_id:
            await _track_job_progress(
                redis_client, engine, job_id,
                post_id=post_id, failed=True, error=str(exc),
            )
        # Route to DLQ instead of silently dropping (§P7.10).  record_failure
        # handles the ACK so the message leaves the PEL.
        try:
            await record_failure(
                redis_client,
                stream=STREAM_KEY,
                group=CONSUMER_GROUP,
                msg_id=msg_id,
                fields=fields,
                error=exc,
                max_retries=ASSEMBLER_MAX_RETRIES,
            )
        except Exception as dlq_exc:
            bound_log.error("build_failed_dlq_error", error=str(dlq_exc))
        return

    # --- Persist (fan-out, parallel) ----------------------------------------
    # The Stage-1 document embedding rides alongside the canonical result (it
    # is not part of the output schema) and lands in the pgvector column.
    # On the reuse path the vector comes from the source row, carried in the
    # envelope along with its honest stub flag — dropping that flag is what made
    # a copied stub claim to be a real semantic vector (§13.3).
    stage1_embedding = (
        env.get("embedding") if reused_result else stage1_result.get("embedding")
    )

    # ...and so does its provenance. Stage 1 now reports whether the vector is
    # the deterministic hash stub, because it is the only layer that knows: the
    # stub is EMBEDDING_DIM-sized exactly like a real vector, so persistence's
    # dimension check recorded every stub in the default configuration as real
    # (§13.2). ONE value is computed here and used for both the trace frame and
    # the database column, so the two cannot disagree again — which is how the
    # §9.11 fix left half the defect standing.
    stage1_embedding_is_stub = (
        bool(env.get("embedding_is_stub", True))
        if reused_result
        else _embedding_is_stub(result, stage1_result)
    )

    # Time each backend separately. The fan-out is a gather, so a single
    # "persist took 4s" line cannot tell you which store was slow — and these
    # three fail for completely different reasons.
    async def _timed(name: str, coro):
        t = time.monotonic()
        try:
            await coro
        except Exception as exc:
            bound_log.error(
                "persist_target_failed", target=name, error=str(exc),
                ms=round((time.monotonic() - t) * 1000, 1),
            )
            raise
        bound_log.info(
            "persist_target_ok", target=name,
            ms=round((time.monotonic() - t) * 1000, 1),
        )

    n_comments = len(((result.get("comment_analysis") or {}).get("comments")) or [])
    bound_log.info(
        "persist_fanout_start",
        targets=["postgres", "clickhouse", "object_storage"],
        embedding_dims=len(stage1_embedding or []),
        embedding_is_stub=stage1_embedding_is_stub,
        comment_rows=n_comments,
    )
    t_fan = time.monotonic()
    try:
        await asyncio.gather(
            _timed("postgres", persist_postgres(
                result, engine,
                embedding=stage1_embedding,
                embedding_is_stub=stage1_embedding_is_stub,
            )),
            # persist_clickhouse writes both the analytics row AND the per-comment
            # rows on its single connection (sequentially) — they must NOT be
            # separate gather tasks or clickhouse-driver rejects the concurrent use.
            _timed("clickhouse", persist_clickhouse(result, ch_client)),
            _timed("object_storage", persist_minio(result, s3_client, bucket)),
        )
    except Exception as exc:
        bound_log.error("persist_failed", error=str(exc),
                        ms=round((time.monotonic() - t_fan) * 1000, 1))
        if job_id:
            await _update_job_status(engine, job_id, _JOB_STATUS_FAILED, str(exc))
        # Do NOT ACK — leave the message for retry
        raise
    bound_log.info(
        "persist_fanout_done",
        ms=round((time.monotonic() - t_fan) * 1000, 1),
    )

    # --- Publish completion event -------------------------------------------
    pubsub_channel = f"analysis:done:{post_id}"
    completion_payload = json.dumps(
        {
            "post_id": post_id,
            "overall_sentiment": result.get("overall_sentiment"),
            "campaign_id": result.get("campaign_id"),
        },
        ensure_ascii=False,
    )
    try:
        await redis_client.publish(pubsub_channel, completion_payload)
    except Exception as exc:
        # Non-fatal — log and continue
        bound_log.warning("pubsub_publish_failed", channel=pubsub_channel, error=str(exc))

    # --- Update job progress / status ----------------------------------------
    # The assembler frame goes out before _track_job_progress, because that call
    # may publish the terminal `done` event which closes the job's SSE stream.
    conf = result.get("confidence")
    proc_now = result.get("processing") or {}
    await publish_stage(
        redis_client, "assembler", "done",
        job_id=job_id, post_id=post_id,
        ms=(time.monotonic() - t0) * 1000,
        detail={
            "schema_version": proc_now.get("schema_version"),
            "schema_valid": True,  # build_canonical_result raises otherwise
            "top_level_keys": len(result),
            "confidence": conf.get("overall") if isinstance(conf, dict) else conf,
            "overall_sentiment": result.get("overall_sentiment"),
            "post_type": result.get("post_type"),
            "llm_used": proc_now.get("llm_used"),
            "stage1_ms": proc_now.get("stage1_ms"),
            "stage2_ms": proc_now.get("stage2_ms"),
            "embedding_stored": bool(stage1_embedding),
            # `embedding_stored: true` + `embedding_dims: 768` both read as
            # success even when the vector is the hash stub, which is why §5.9
            # went unnoticed. Say which it is.
            #
            # This is the SAME value handed to persist_postgres above, not a
            # second computation of it. Two independent computations is exactly
            # what §13.2 was: this frame said `true` while the column it is
            # supposed to describe said `false`.
            "embedding_is_stub": stage1_embedding_is_stub,
            "writes": ["postgres+pgvector", "clickhouse", "object storage"],
        },
        log=bound_log,
    )

    if job_id:
        await _track_job_progress(
            redis_client, engine, job_id,
            post_id=post_id, sentiment=result.get("overall_sentiment"),
        )

    # --- ACK ----------------------------------------------------------------
    await redis_client.xack(STREAM_KEY, CONSUMER_GROUP, msg_id)

    # --- Structured log summary ---------------------------------------------
    elapsed_ms = (time.monotonic() - t0) * 1000
    proc = result.get("processing", {})
    bound_log.info(
        "assembled",
        overall_sentiment=result.get("overall_sentiment"),
        llm_used=proc.get("llm_used", False),
        stage1_ms=proc.get("stage1_ms"),
        stage2_ms=proc.get("stage2_ms"),
        total_ms=round(elapsed_ms, 1),
    )


# ---------------------------------------------------------------------------
# Main consumer loop
# ---------------------------------------------------------------------------

async def run_assembler(
    *,
    engine,
    ch_client,
    s3_client,
    bucket: str,
    redis_client,
    shutdown_event: asyncio.Event | None = None,
) -> None:
    """Run the assembler consumer loop until shutdown_event is set (or forever).

    Parameters
    ----------
    engine:
        Async SQLAlchemy engine for PostgreSQL (incl. the pgvector embedding).
    ch_client:
        Synchronous ``clickhouse_driver.Client`` instance.
    s3_client:
        Synchronous boto3 S3 client with ``endpoint_url`` for MinIO.
    bucket:
        MinIO bucket name.
    redis_client:
        Async ``redis.asyncio.Redis`` client.
    shutdown_event:
        Optional asyncio.Event.  The loop exits when it is set.
    """
    await _ensure_consumer_group(redis_client)

    log.info(
        "assembler_started",
        stream=STREAM_KEY,
        group=CONSUMER_GROUP,
        consumer=CONSUMER_NAME,
    )

    while True:
        if shutdown_event and shutdown_event.is_set():
            log.info("assembler_shutdown_requested")
            break

        try:
            messages = await redis_client.xreadgroup(
                groupname=CONSUMER_GROUP,
                consumername=CONSUMER_NAME,
                streams={STREAM_KEY: ">"},
                count=BATCH_SIZE,
                block=BLOCK_MS,
            )
        except asyncio.CancelledError:
            log.info("assembler_cancelled")
            break
        except Exception as exc:
            msg = str(exc)
            # An idle BLOCK window with no new messages surfaces as a redis
            # TimeoutError — normal when upstream is quiet, not an error.
            if isinstance(exc, asyncio.TimeoutError) or "Timeout" in msg:
                log.debug("xreadgroup_idle")
                continue
            log.error("xreadgroup_error", error=msg)
            if "NOGROUP" in str(exc):
                # Stream/group wiped at runtime (e.g. FLUSHALL) — re-create
                # the group instead of error-looping forever.
                try:
                    await _ensure_consumer_group(redis_client)
                except Exception as group_exc:
                    log.error("group_recreate_failed", error=str(group_exc))
            await asyncio.sleep(1)
            continue

        if not messages:
            # Timeout — stream was empty, loop again
            continue

        for stream_name, entries in messages:
            for msg_id_raw, fields in entries:
                msg_id = (
                    msg_id_raw.decode("utf-8")
                    if isinstance(msg_id_raw, bytes)
                    else msg_id_raw
                )
                try:
                    await _process_message(
                        msg_id,
                        fields,
                        engine=engine,
                        ch_client=ch_client,
                        s3_client=s3_client,
                        bucket=bucket,
                        redis_client=redis_client,
                    )
                except Exception as exc:
                    log.error(
                        "message_processing_error",
                        msg_id=msg_id,
                        error=str(exc),
                    )
                    # Bounded retry-by-re-enqueue, then dead-letter (§8) so a
                    # poison message can't sit in the PEL forever.
                    try:
                        outcome = await record_failure(
                            redis_client,
                            stream=STREAM_KEY,
                            group=CONSUMER_GROUP,
                            msg_id=msg_id_raw,
                            fields=fields,
                            error=exc,
                            max_retries=ASSEMBLER_MAX_RETRIES,
                        )
                        log.warning("assembler_failure_handled", msg_id=msg_id, outcome=outcome)
                    except Exception as dlq_exc:
                        log.error("assembler_dlq_failed", msg_id=msg_id, error=str(dlq_exc))

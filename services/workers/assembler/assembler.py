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
import logging
import os
import sys
import time
from typing import Any

import structlog

# Allow the libs package to be imported when running as a standalone service.
_LIBS_ROOT = os.path.join(os.path.dirname(__file__), "..", "..", "..", "libs")
if _LIBS_ROOT not in sys.path:
    sys.path.insert(0, os.path.abspath(_LIBS_ROOT))

from builder import build_canonical_result
from persistence import (
    persist_clickhouse,
    persist_minio,
    persist_postgres,
)

# ---------------------------------------------------------------------------
# Structured logging
# ---------------------------------------------------------------------------
structlog.configure(
    wrapper_class=structlog.make_filtering_bound_logger(
        logging.getLevelName(os.environ.get("LOG_LEVEL", "INFO"))
    ),
)
log: structlog.BoundLogger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Redis stream constants
# ---------------------------------------------------------------------------
STREAM_KEY = "assembler:queue"
CONSUMER_GROUP = "assembler-group"
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


def _parse_message(raw: dict[str, Any]) -> tuple[str, dict, dict, dict | None, str | None]:
    """Parse and JSON-decode the fields of a single stream message.

    Returns
    -------
    (post_id, normalized_post, stage1_result, stage2_result, job_id)
    """
    # Every inter-stage message is a single ``data`` field holding a JSON
    # envelope: {post_id, normalized_post, stage1_result, stage2_result, ...}.
    env: dict = json.loads(raw["data"])

    post_id: str = env["post_id"]
    normalized_post: dict = env["normalized_post"]
    stage1_result: dict = env["stage1_result"]
    stage2_result: dict | None = env.get("stage2_result")
    job_id: str | None = env.get("job_id") or None

    return post_id, normalized_post, stage1_result, stage2_result, job_id


async def _update_job_status(
    engine,
    job_id: str,
    status: str,
    error: str | None = None,
) -> None:
    """Update the jobs table row for job_id with the given status."""
    from sqlalchemy import text

    sql = text(
        """
        UPDATE jobs
        SET    status     = :status,
               updated_at = NOW(),
               error      = :error
        WHERE  id = :job_id
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
    post_id, normalized_post, stage1_result, stage2_result, job_id = _parse_message(raw)

    bound_log = log.bind(post_id=post_id, msg_id=msg_id)

    # --- Build & validate ---------------------------------------------------
    try:
        result = build_canonical_result(normalized_post, stage1_result, stage2_result)
    except (ValueError, KeyError) as exc:
        bound_log.error("build_failed", error=str(exc))
        if job_id:
            await _track_job_progress(
                redis_client, engine, job_id,
                post_id=post_id, failed=True, error=str(exc),
            )
        # ACK anyway so the message does not loop indefinitely
        await redis_client.xack(STREAM_KEY, CONSUMER_GROUP, msg_id)
        return

    # --- Persist (fan-out, parallel) ----------------------------------------
    # The Stage-1 document embedding rides alongside the canonical result (it
    # is not part of the output schema) and lands in the pgvector column.
    stage1_embedding = stage1_result.get("embedding")
    try:
        await asyncio.gather(
            persist_postgres(result, engine, embedding=stage1_embedding),
            persist_clickhouse(result, ch_client),
            persist_minio(result, s3_client, bucket),
        )
    except Exception as exc:
        bound_log.error("persist_failed", error=str(exc))
        if job_id:
            await _update_job_status(engine, job_id, _JOB_STATUS_FAILED, str(exc))
        # Do NOT ACK — leave the message for retry
        raise

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
            log.error("xreadgroup_error", error=str(exc))
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
                    # Continue to next message; failed message is left
                    # pending in the consumer group for redelivery.

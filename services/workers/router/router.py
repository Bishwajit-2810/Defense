"""
Router worker — reads "router:queue" and dispatches each post to either
"llm:stage2:queue" or "assembler:queue" based on routing rules.

Design target: keep the share of posts reaching Stage-2 LLM low. That share is
measured, not assumed — `stats:llm_routed / stats:total_processed`, exported by
this worker and surfaced as `estimated_llm_share` on the analysis API. See
rules.py for what each gate reads and PROJECT_ASSESSMENT.md §4 for the measured
rate on the 50-post sample.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import time

import redis.asyncio as aioredis
import structlog

from .rules import (
    get_task_flags,
    read_image_sentiment,
    read_overall_confidence,
    read_photo_count,
    read_text_length,
    should_use_llm,
)
from libs.common.logging import setup_logging
from libs.dlq import record_failure
from libs.progress import publish_stage

setup_logging("router")
logger = structlog.get_logger(__name__)

REDIS_URL: str = os.environ.get("REDIS_URL", "redis://localhost:6379/0")

# Bounded retry before a failed message is dead-lettered to router:queue:dlq (§8).
ROUTER_MAX_RETRIES: int = int(os.environ.get("ROUTER_MAX_RETRIES", "3"))

# Stream / consumer-group names
ROUTER_QUEUE: str = "router:queue"
STAGE2_QUEUE: str = "llm:stage2:queue"
ASSEMBLER_QUEUE: str = "assembler:queue"
CONSUMER_GROUP: str = "router-workers"
CONSUMER_NAME: str = os.environ.get("HOSTNAME", "router-0")

# Redis stat keys
STAT_TOTAL: str = "stats:total_processed"
STAT_LLM: str = "stats:llm_routed"

# How long to block waiting for new stream messages (ms)
BLOCK_MS: int = 5_000
# Max messages to read per poll
COUNT: int = 50


async def _ensure_group(redis: aioredis.Redis, stream: str, group: str) -> None:
    """Create the consumer group if it does not exist yet."""
    try:
        await redis.xgroup_create(stream, group, id="0", mkstream=True)
        logger.info("consumer_group_created", stream=stream, group=group)
    except aioredis.ResponseError as exc:
        if "BUSYGROUP" in str(exc):
            # Group already exists — this is fine.
            pass
        else:
            raise


async def _process_message(
    redis: aioredis.Redis,
    message_id: bytes,
    fields: dict,
) -> None:
    """Parse one stream message and route it."""
    t0 = time.monotonic()

    raw = fields.get(b"data") or fields.get("data", b"{}")
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")

    try:
        payload: dict = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.error(
            "router_parse_error",
            message_id=message_id,
            error=str(exc),
        )
        return

    post_id: str = payload.get("post_id", "unknown")
    # Route on the stage-1 result; the rest of the envelope is passed through.
    stage1_result: dict = payload.get("stage1_result", {})
    options: dict = payload.get("options", {})

    use_llm, reasons = should_use_llm(stage1_result, options)
    job_id = payload.get("job_id")

    # Log the inputs the six rules actually read, not just the verdict — via the
    # same readers the rules use, so this can never report a field the gate did
    # not see (it used to log raw key lookups, which read None for fields Stage 1
    # emits under another name and made three dead rules look like passing ones).
    logger.info(
        "router_rules_evaluated",
        post_id=post_id,
        use_llm=use_llm,
        fired=reasons or [],
        confidence=read_overall_confidence(stage1_result),
        post_type=stage1_result.get("post_type"),
        post_type_confidence=stage1_result.get("post_type_confidence"),
        want_summary=options.get("want_summary", False),
        photo_count=read_photo_count(stage1_result),
        image_sentiment=read_image_sentiment(stage1_result),
        toxicity_score=stage1_result.get("toxicity_score"),
        language=stage1_result.get("language"),
        script=stage1_result.get("script"),
        is_banglish=bool(stage1_result.get("is_banglish")),
        caption_chars=read_text_length(stage1_result),
    )

    if use_llm:
        task_flags = get_task_flags(stage1_result, options)
        payload["task_flags"] = task_flags
        await redis.xadd(STAGE2_QUEUE, {"data": json.dumps(payload)})
        await redis.incr(STAT_LLM)
        logger.info(
            "router_decision",
            post_id=post_id,
            destination="llm:stage2:queue",
            reasons=reasons,
        )
        await publish_stage(
            redis, "router", "done",
            job_id=job_id, post_id=post_id,
            ms=(time.monotonic() - t0) * 1000.0,
            detail={
                "use_llm": True,
                "reasons": reasons,
                "task_flags": task_flags,
                "next_stream": STAGE2_QUEUE,
            },
            log=logger,
        )
    else:
        payload["stage2_result"] = None
        await redis.xadd(ASSEMBLER_QUEUE, {"data": json.dumps(payload)})
        logger.info(
            "router_decision",
            post_id=post_id,
            destination="assembler:queue",
            reasons=[],
        )
        await publish_stage(
            redis, "router", "done",
            job_id=job_id, post_id=post_id,
            ms=(time.monotonic() - t0) * 1000.0,
            detail={
                "use_llm": False,
                "reasons": [],
                "next_stream": ASSEMBLER_QUEUE,
            },
            log=logger,
        )
        # Stage 2 is bypassed entirely — say so explicitly so the trace shows a
        # skipped layer rather than a silently missing one.
        await publish_stage(
            redis, "stage2", "skipped",
            job_id=job_id, post_id=post_id,
            detail={"reason": "no routing rule fired"},
            log=logger,
        )

    await redis.incr(STAT_TOTAL)

    elapsed_ms = int((time.monotonic() - t0) * 1000)
    logger.debug("router_message_done", post_id=post_id, elapsed_ms=elapsed_ms)


async def run() -> None:
    """Main event loop: consume router:queue forever."""
    redis = aioredis.from_url(REDIS_URL, decode_responses=False)

    await _ensure_group(redis, ROUTER_QUEUE, CONSUMER_GROUP)

    logger.info(
        "router_started",
        stream=ROUTER_QUEUE,
        group=CONSUMER_GROUP,
        consumer=CONSUMER_NAME,
    )

    shutdown = asyncio.Event()

    def _handle_signal(*_):
        logger.info("router_shutdown_signal")
        shutdown.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _handle_signal)

    while not shutdown.is_set():
        try:
            results = await redis.xreadgroup(
                groupname=CONSUMER_GROUP,
                consumername=CONSUMER_NAME,
                streams={ROUTER_QUEUE: ">"},
                count=COUNT,
                block=BLOCK_MS,
            )
        except asyncio.CancelledError:
            break
        except Exception as exc:
            msg = str(exc)
            # An idle BLOCK window with no new messages surfaces as a redis
            # TimeoutError — that's normal when the queue is empty, not an error.
            # Re-block quietly instead of spamming ERROR every few seconds.
            if isinstance(exc, asyncio.TimeoutError) or "Timeout" in msg:
                logger.debug("router_read_idle")
                continue
            logger.error("router_read_error", error=msg)
            if "NOGROUP" in str(exc):
                # Stream/group wiped at runtime (e.g. FLUSHALL) — re-create
                # the group instead of error-looping forever.
                try:
                    await _ensure_group(redis, ROUTER_QUEUE, CONSUMER_GROUP)
                except Exception as group_exc:
                    logger.error("router_group_recreate_failed", error=str(group_exc))
            await asyncio.sleep(1)
            continue

        if not results:
            continue

        for _stream, messages in results:
            for message_id, fields in messages:
                try:
                    await _process_message(redis, message_id, fields)
                    await redis.xack(ROUTER_QUEUE, CONSUMER_GROUP, message_id)
                except Exception as exc:
                    logger.error(
                        "router_message_error",
                        message_id=message_id,
                        error=str(exc),
                    )
                    # Bounded retry, then dead-letter (§8) — never silently drop
                    # a failed message (record_failure ACKs the original).
                    try:
                        outcome = await record_failure(
                            redis,
                            stream=ROUTER_QUEUE,
                            group=CONSUMER_GROUP,
                            msg_id=message_id,
                            fields=fields,
                            error=exc,
                            max_retries=ROUTER_MAX_RETRIES,
                        )
                        logger.warning(
                            "router_failure_handled",
                            message_id=message_id,
                            outcome=outcome,
                        )
                    except Exception as dlq_exc:
                        logger.error(
                            "router_dlq_failed",
                            message_id=message_id,
                            error=str(dlq_exc),
                        )

    logger.info("router_stopped")
    await redis.aclose()

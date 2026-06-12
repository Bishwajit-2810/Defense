"""Async Redis stream consumer — Stage-1 NLP worker.

Reads from the stream "nlp:stage1:queue" (produced by the ingestion service),
runs the full Stage-1 NLP pipeline on each post, and pushes a partial
analysis result to "router:queue" for the router to forward to Stage-2.

Stream message format (nlp:stage1:queue):
  {
    "post_id": "<cuid>",
    "payload":  "<json-encoded post-with-details object>"
  }

Output message format (router:queue):
  {
    "post_id": "<cuid>",
    "result":  "<json-encoded partial analysis result>"
  }
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from typing import Any

import redis.asyncio as aioredis
import structlog

# ---------------------------------------------------------------------------
# Make the shared libs importable
# ---------------------------------------------------------------------------
_LIBS_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "..", "libs")
if _LIBS_PATH not in sys.path:
    sys.path.insert(0, _LIBS_PATH)

from common.utils import (  # noqa: E402
    compute_coverage,
    platform_from_url,
    reaction_breakdown_to_dict,
)

from .comment_analyzer import analyze_comments  # noqa: E402
from .fusion import fuse_sentiment  # noqa: E402
from .models import ModelRegistry  # noqa: E402
from .text_analyzer import analyze_text  # noqa: E402
from .vision_analyzer import analyze_image  # noqa: E402

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

structlog.configure(
    wrapper_class=structlog.make_filtering_bound_logger(
        getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO)
    ),
)
log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Redis stream / consumer-group settings
# ---------------------------------------------------------------------------

INPUT_STREAM = os.getenv("NLP_STAGE1_STREAM", "nlp:stage1:queue")
OUTPUT_STREAM = os.getenv("ROUTER_STREAM", "router:queue")
CONSUMER_GROUP = os.getenv("NLP_STAGE1_GROUP", "stage1-nlp-group")
CONSUMER_NAME = os.getenv(
    "NLP_STAGE1_CONSUMER",
    f"stage1-nlp-{os.getpid()}",
)
BLOCK_MS = int(os.getenv("REDIS_BLOCK_MS", "2000"))
BATCH_SIZE = int(os.getenv("STAGE1_BATCH_SIZE", "1"))


# ---------------------------------------------------------------------------
# Result builder
# ---------------------------------------------------------------------------

def _build_result(
    post: dict,
    text_result: dict,
    image_result: dict | None,
    comment_analysis: dict,
    overall_sentiment: str,
    sentiment_score: float,
    stage1_ms: float,
) -> dict:
    """Assemble the partial Stage-1 analysis dict.

    All Stage-2 fields (post_summary, post_type, etc.) are set to None so the
    downstream router / assembler always receives a complete schema.
    """
    engagement = post.get("engagement") or {}
    reaction_breakdown = post.get("reactionBreakdown") or {}
    photo_urls: list[str] = post.get("photoUrls") or []

    # Normalise reaction breakdown keys to uppercase for consistency with the
    # upstream schema, but also store a lowercase copy for internal use.
    rb_normalised = {k.upper(): v for k, v in reaction_breakdown.items()}

    # Fill comment coverage now that we have commentCount
    comment_count = engagement.get("commentCount", 0)
    analyzed_count = comment_analysis.get("analyzed", 0)
    comment_analysis["coverage"] = compute_coverage(analyzed_count, comment_count)

    # Confidence: average of language + sentiment confidences where available
    lang_conf = text_result.get("language_confidence") or 0.0
    sent_conf = text_result.get("sentiment_confidence") or 0.0
    confidence = round(
        (lang_conf + sent_conf) / 2.0 if (lang_conf and sent_conf) else max(lang_conf, sent_conf),
        3,
    )

    # Image analysis block
    image_analysis: dict | None = None
    if image_result is not None:
        image_analysis = {
            "image_count": len(photo_urls),
            "ocr_text": image_result.get("ocr_text", ""),
            "description": image_result.get("description"),
            "images": [
                {
                    "ref": f"photoUrls[0]",
                    "sentiment": {
                        "label": image_result.get("image_sentiment", "neutral"),
                        "score": image_result.get("image_sentiment_score", 0.0),
                    },
                    "ocr_text": image_result.get("ocr_text", ""),
                    "description": image_result.get("description"),
                }
            ],
            "vision_model": "stub" if os.getenv("MODEL_STUB_MODE", "true").lower() == "true" else "SigLIP",
        }

    # text_sentiment / image_sentiment in output schema
    text_sentiment_out: dict | None = None
    if text_result.get("sentiment") is not None:
        text_sentiment_out = {
            "label": text_result["sentiment"],
            "score": text_result.get("sentiment_score", 0.0),
        }

    image_sentiment_out: dict | None = None
    if image_result is not None:
        image_sentiment_out = {
            "label": image_result.get("image_sentiment", "neutral"),
            "score": image_result.get("image_sentiment_score", 0.0),
        }

    return {
        # --- Identity ---
        "post_id": post.get("id"),
        "campaign_id": post.get("campaignId"),
        "platform": platform_from_url(post.get("url", "")),
        "platform_post_id": post.get("platformPostId"),
        "media_type": post.get("postType"),
        "url": post.get("url"),

        # --- Language ---
        "language": text_result.get("language"),
        "script": text_result.get("script"),
        "is_banglish": text_result.get("is_banglish", False),
        "language_confidence": text_result.get("language_confidence"),

        # --- Sentiment ---
        "overall_sentiment": overall_sentiment,
        "sentiment_score": sentiment_score,
        "text_sentiment": text_sentiment_out,
        "image_sentiment": image_sentiment_out,
        "baseline_sentiment": post.get("sentiment"),
        "baseline_viral_potential": post.get("viralPotential"),

        # --- NLP features ---
        "emotion": text_result.get("emotion"),
        "topics": text_result.get("topics", []),
        "intents": text_result.get("intents", []),
        "toxicity_score": text_result.get("toxicity_score"),
        "hate_speech_score": text_result.get("hate_speech_score"),
        "entities": text_result.get("entities", []),
        "keywords": text_result.get("keywords", []),
        "embedding": text_result.get("embedding"),

        # --- Vision ---
        "image_analysis": image_analysis,

        # --- Comments ---
        "comment_analysis": comment_analysis,

        # --- Engagement / reactions ---
        "engagement": {
            "reactions": engagement.get("totalReactions", 0),
            "comment_count": engagement.get("commentCount", 0),
            "share_count": engagement.get("shareCount", 0),
            "stored_comments": engagement.get("storedCommentRows", 0),
        },
        "reaction_breakdown": rb_normalised,

        # --- Stage-2 placeholders (filled by LLM worker) ---
        "post_type": None,
        "post_summary": None,
        "post_summary_lang": None,
        "post_summary_grounding": None,

        # --- Provenance ---
        "baseline_isViral": post.get("isViral"),
        "baseline_isViralCandidate": post.get("isViralCandidate"),
        "created_at": post.get("postedAt"),
        "scraped_at": post.get("scrapedAt"),
        "confidence": confidence,

        # --- Processing metadata ---
        "processing": {
            "unit": "post+thread",
            "stage1_ms": round(stage1_ms, 1),
            "llm_used": False,
            "llm_role": None,
            "llm_backend": None,
            "llm_model": None,
            "vision_used": image_result is not None,
            "vision_model": "stub" if os.getenv("MODEL_STUB_MODE", "true").lower() == "true" else "SigLIP",
            "stub_mode": os.getenv("MODEL_STUB_MODE", "true").lower() == "true",
        },
    }


# ---------------------------------------------------------------------------
# Per-message handler
# ---------------------------------------------------------------------------

async def _process_message(
    post: dict,
    registry: ModelRegistry,
) -> dict:
    """Run the full Stage-1 pipeline on a single post-with-details object."""

    caption: str | None = post.get("caption")
    photo_urls: list[str] = post.get("photoUrls") or []
    comments: list[dict] = post.get("comments") or []

    # 1. Text NLP on caption
    text_result = await analyze_text(caption, registry)

    # 2. Image analysis — MVP: only the first image
    image_result: dict | None = None
    if photo_urls:
        image_result = await analyze_image(photo_urls[0], registry)

    # 3. Comment analysis
    comment_analysis = await analyze_comments(comments, registry)

    # 4. Sentiment fusion
    reaction_breakdown = post.get("reactionBreakdown") or {}
    # Normalise keys to lowercase for fusion helper
    rb_lower = reaction_breakdown_to_dict(reaction_breakdown)
    overall_sentiment, sentiment_score = fuse_sentiment(
        text_result=text_result,
        image_result=image_result,
        reaction_breakdown=rb_lower,
        caption=caption,
    )

    return text_result, image_result, comment_analysis, overall_sentiment, sentiment_score


# ---------------------------------------------------------------------------
# Consumer-group bootstrap
# ---------------------------------------------------------------------------

async def _ensure_consumer_group(redis: aioredis.Redis) -> None:
    """Create the consumer group if it does not exist."""
    try:
        await redis.xgroup_create(INPUT_STREAM, CONSUMER_GROUP, id="0", mkstream=True)
        log.info("consumer_group_created", stream=INPUT_STREAM, group=CONSUMER_GROUP)
    except aioredis.ResponseError as exc:
        if "BUSYGROUP" in str(exc):
            log.debug("consumer_group_exists", group=CONSUMER_GROUP)
        else:
            raise


# ---------------------------------------------------------------------------
# Main worker loop
# ---------------------------------------------------------------------------

async def run_worker() -> None:
    """Connect to Redis and consume messages indefinitely."""
    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    log.info(
        "stage1_nlp_worker_starting",
        stream=INPUT_STREAM,
        group=CONSUMER_GROUP,
        consumer=CONSUMER_NAME,
        stub_mode=os.getenv("MODEL_STUB_MODE", "true"),
    )

    registry = ModelRegistry()

    redis = await aioredis.from_url(redis_url, decode_responses=True)
    try:
        await _ensure_consumer_group(redis)

        while True:
            try:
                messages = await redis.xreadgroup(
                    groupname=CONSUMER_GROUP,
                    consumername=CONSUMER_NAME,
                    streams={INPUT_STREAM: ">"},
                    count=BATCH_SIZE,
                    block=BLOCK_MS,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error("stage1_read_error", error=str(exc))
                if "NOGROUP" in str(exc):
                    # Stream/group wiped at runtime (e.g. FLUSHALL) — re-create
                    # the group instead of crashing the worker.
                    try:
                        await _ensure_consumer_group(redis)
                    except Exception as group_exc:
                        log.error("stage1_group_recreate_failed", error=str(group_exc))
                await asyncio.sleep(1)
                continue

            if not messages:
                continue

            # messages: [(stream_name, [(msg_id, {field: value, ...}), ...])]
            for _stream, entries in messages:
                for msg_id, fields in entries:
                    t0 = time.monotonic()
                    post_id = "unknown"

                    try:
                        envelope: dict[str, Any] = json.loads(fields.get("data", "{}"))
                        post: dict[str, Any] = envelope.get("raw_post", {})
                        post_id = envelope.get("post_id", "unknown")
                    except json.JSONDecodeError as exc:
                        log.error(
                            "invalid_json_payload",
                            msg_id=msg_id,
                            post_id=post_id,
                            error=str(exc),
                        )
                        await redis.xack(INPUT_STREAM, CONSUMER_GROUP, msg_id)
                        continue

                    try:
                        (
                            text_result,
                            image_result,
                            comment_analysis,
                            overall_sentiment,
                            sentiment_score,
                        ) = await _process_message(post, registry)

                        stage1_ms = (time.monotonic() - t0) * 1000.0

                        result = _build_result(
                            post=post,
                            text_result=text_result,
                            image_result=image_result,
                            comment_analysis=comment_analysis,
                            overall_sentiment=overall_sentiment,
                            sentiment_score=sentiment_score,
                            stage1_ms=stage1_ms,
                        )

                        # Carry the envelope forward: drop the bulky raw_post,
                        # attach the stage-1 result for the router/assembler.
                        envelope.pop("raw_post", None)
                        envelope["stage1_result"] = result
                        await redis.xadd(
                            OUTPUT_STREAM,
                            {"data": json.dumps(envelope, ensure_ascii=False)},
                        )

                        await redis.xack(INPUT_STREAM, CONSUMER_GROUP, msg_id)

                        log.info(
                            "stage1_processed",
                            post_id=post_id,
                            msg_id=msg_id,
                            sentiment=overall_sentiment,
                            stage1_ms=round(stage1_ms, 1),
                        )

                    except Exception as exc:
                        stage1_ms = (time.monotonic() - t0) * 1000.0
                        log.error(
                            "stage1_processing_error",
                            post_id=post_id,
                            msg_id=msg_id,
                            error=str(exc),
                            stage1_ms=round(stage1_ms, 1),
                            exc_info=True,
                        )
                        # Do NOT ack — leave in PEL for retry / dead-letter logic.
    finally:
        await redis.aclose()

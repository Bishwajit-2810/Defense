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

from dlq import record_failure  # noqa: E402
from progress import publish_stage  # noqa: E402

from .comment_analyzer import analyze_comments  # noqa: E402
from .fusion import fuse_sentiment  # noqa: E402
from .models import ModelRegistry  # noqa: E402
from .text_analyzer import analyze_text  # noqa: E402
from .vision_analyzer import analyze_image  # noqa: E402

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

from common.logging import setup_logging  # noqa: E402

setup_logging("stage1")
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

# Runtime sentiment-model override set from the dashboard (PUT /v1/config/nlp).
# Read fresh per poll; absent/blank => auto-route by detected language.
SENTIMENT_MODEL_CONFIG_KEY = os.getenv("SENTIMENT_MODEL_CONFIG_KEY", "config:sentiment_model")

# Runtime LLM-backend override (local/groq) shared with Stage 2 + the dashboard
# (PUT /v1/config/llm). Read per poll when llm_mode is on; absent => LLM_BACKEND.
LLM_BACKEND_CONFIG_KEY = os.getenv("LLM_BACKEND_KEY", "config:llm_backend")

# Bounded retry before a failed message is dead-lettered to nlp:stage1:queue:dlq.
STAGE1_MAX_RETRIES = int(os.getenv("STAGE1_MAX_RETRIES", "3"))


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

    Stage-2-only fields (post_summary, …) are set to None so the downstream
    router / assembler always receives a complete schema. ``post_type`` is NOT
    one of them: Stage 1 emits its own best-effort classification plus a
    confidence, and Stage 2 refines it only when the router asks. A None
    placeholder there used to mean two different things — "Stage 2 will fill
    this" and "unclassified" — which made the router's Rule 2 fire on every
    single post and routed 100% of traffic to the LLM.

    The dict is also the router's *only* input, so every field the six rules in
    router/rules.py read has to be present here under the name they read.
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

    # Which engine produced the Stage-1 NLP: "llm" (the stage1 LLM), "models"
    # (small-model suite) or "stub". Drives the processing provenance below.
    engine = text_result.get("engine")
    stage1_llm_used = engine == "llm"

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

        # --- Post shape (read by router rules 4 and 6) ---
        # photo_urls and the caption length travel with the result because the
        # router sees nothing else; the caption text itself stays out of the
        # envelope (it is already in normalized_post, and can be 5 KB+).
        "photo_urls": photo_urls,
        "caption_chars": len(post.get("caption") or ""),

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

        # --- Semantic post type (Stage-1 best effort; Stage 2 may refine) ---
        "post_type": text_result.get("post_type"),
        "post_type_confidence": text_result.get("post_type_confidence"),

        # --- Stage-2 placeholders (filled by LLM worker) ---
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
            # Stage-1-internal LLM provenance: true when the `stage1` LLM (not the
            # small-model suite / stub) produced the NLP. The canonical
            # processing.llm_used the assembler emits still tracks Stage-2 only.
            "nlp_engine": engine,
            "llm_used": stage1_llm_used,
            "llm_role": "stage1" if stage1_llm_used else None,
            "llm_backend": text_result.get("llm_backend"),
            "llm_model": text_result.get("llm_model"),
            "vision_used": image_result is not None,
            "vision_model": "stub" if os.getenv("MODEL_STUB_MODE", "true").lower() == "true" else "SigLIP",
            "stub_mode": os.getenv("MODEL_STUB_MODE", "true").lower() == "true",
            # Which concrete models produced this result (reproducibility); the
            # sentiment model is the stage1 LLM in llm_mode, else language-routed.
            "model_versions": {
                "sentiment": text_result.get("sentiment_model"),
                "sentiment_route": text_result.get("sentiment_route"),
            },
        },
    }


# ---------------------------------------------------------------------------
# Per-message handler
# ---------------------------------------------------------------------------

async def _process_message(
    post: dict,
    registry: ModelRegistry,
    sentiment_override: str | None = None,
    backend_override: str | None = None,
) -> dict:
    """Run the full Stage-1 pipeline on a single post-with-details object.

    ``sentiment_override`` is the runtime UI/Redis selection (a key like
    ``"banglabert"``) forcing a specific sentiment model; ``None`` means
    auto-route by detected language (see libs/sentiment_models.py).
    ``backend_override`` ("local"/"groq") follows the dashboard LLM toggle so
    the stage1 LLM uses the same backend as Stage 2; ``None`` => LLM_BACKEND.
    """

    caption: str | None = post.get("caption")
    photo_urls: list[str] = post.get("photoUrls") or []
    comments: list[dict] = post.get("comments") or []
    post_id = post.get("id", "unknown")

    # Each of the four sections below logs its own line so a stalled post can be
    # pinned to a section rather than to "Stage 1" as a whole. Per-post, never
    # per-comment: a 9k-comment thread must not emit 9k lines.

    # 1. Text NLP on caption
    t = time.monotonic()
    text_result = await analyze_text(caption, registry, sentiment_override, backend_override)
    log.info(
        "stage1_text_done",
        post_id=post_id,
        chars=len(caption or ""),
        engine=text_result.get("engine"),
        language=text_result.get("language"),
        sentiment=text_result.get("sentiment"),
        confidence=text_result.get("sentiment_confidence"),
        embedding_dims=len(text_result.get("embedding") or []),
        ms=round((time.monotonic() - t) * 1000, 1),
    )

    # 2. Image analysis — MVP: only the first image
    image_result: dict | None = None
    if photo_urls:
        t = time.monotonic()
        image_result = await analyze_image(photo_urls[0], registry)
        log.info(
            "stage1_vision_done",
            post_id=post_id,
            images=len(photo_urls),
            image_sentiment=(image_result or {}).get("image_sentiment"),
            ocr_chars=len((image_result or {}).get("ocr_text") or ""),
            ms=round((time.monotonic() - t) * 1000, 1),
        )
    else:
        log.debug("stage1_vision_skipped", post_id=post_id, reason="no photoUrls")

    # 3. Comment analysis
    t = time.monotonic()
    comment_analysis = await analyze_comments(
        comments, registry, sentiment_override, backend_override
    )
    log.info(
        "stage1_comments_done",
        post_id=post_id,
        analyzed=comment_analysis.get("analyzed"),
        breakdown=comment_analysis.get("sentiment_breakdown"),
        methods=comment_analysis.get("method_breakdown"),
        ms=round((time.monotonic() - t) * 1000, 1),
    )

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
    total_reactions = sum(rb_lower.values()) if rb_lower else 0
    neg_reactions = sum(v for k, v in rb_lower.items() if k in ("sad", "angry"))
    log.info(
        "stage1_fusion_done",
        post_id=post_id,
        # Which weighting branch fusion.py took, so a surprising score is traceable.
        rule=("text-only" if not image_result
              else "text0.6+image0.4" if (caption or "").strip()
              else "image0.7+ocr0.3"),
        text_sentiment=text_result.get("sentiment"),
        image_sentiment=(image_result or {}).get("image_sentiment"),
        neg_reaction_ratio=round(neg_reactions / total_reactions, 4) if total_reactions else None,
        overall_sentiment=overall_sentiment,
        sentiment_score=sentiment_score,
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

            # Read the runtime sentiment-model override once per poll (cheap GET).
            # Blank/missing => None => auto-route by detected language.
            try:
                sentiment_override = await redis.get(SENTIMENT_MODEL_CONFIG_KEY) or None
            except Exception:
                sentiment_override = None

            # Follow the dashboard LLM backend toggle (PUT /v1/config/llm) so the
            # stage1 LLM uses the same backend as Stage 2. Blank/invalid => None
            # => LLM_BACKEND default. Only read when llm_mode is on.
            backend_override = None
            if registry.llm_mode:
                try:
                    raw_backend = await redis.get(LLM_BACKEND_CONFIG_KEY)
                    if raw_backend in ("local", "groq"):
                        backend_override = raw_backend
                except Exception:
                    backend_override = None

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

                    job_id = envelope.get("job_id")

                    await publish_stage(
                        redis, "stage1", "running",
                        job_id=job_id, post_id=post_id,
                        detail={
                            "engine": "llm" if registry.llm_mode else (
                                "stub" if registry.stub_mode else "models"
                            ),
                            "has_caption": bool((post.get("caption") or "").strip()),
                            "photo_count": len(post.get("photoUrls") or []),
                            "comments_to_score": len(post.get("comments") or []),
                        },
                        log=log,
                    )

                    try:
                        (
                            text_result,
                            image_result,
                            comment_analysis,
                            overall_sentiment,
                            sentiment_score,
                        ) = await _process_message(
                            post, registry, sentiment_override, backend_override
                        )

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

                        ca = result.get("comment_analysis") or {}
                        await publish_stage(
                            redis, "stage1", "done",
                            job_id=job_id, post_id=post_id,
                            ms=stage1_ms,
                            detail={
                                "language": result.get("language"),
                                "script": result.get("script"),
                                "is_banglish": result.get("is_banglish"),
                                "text_sentiment": (result.get("text_sentiment") or {}).get("label"),
                                "image_sentiment": (result.get("image_sentiment") or {}).get("label"),
                                "overall_sentiment": overall_sentiment,
                                "sentiment_score": sentiment_score,
                                "emotion": (result.get("emotion") or {}).get("primary"),
                                "topics": result.get("topics"),
                                "toxicity_score": result.get("toxicity_score"),
                                "entity_count": len(result.get("entities") or []),
                                "keywords": result.get("keywords"),
                                # 0 dims means no vector was produced — the post
                                # will be invisible to semantic search.
                                "embedding_dims": len(result.get("embedding") or []),
                                "comments_analyzed": ca.get("analyzed"),
                                "comment_coverage": ca.get("coverage"),
                                "comment_breakdown": ca.get("sentiment_breakdown"),
                                "confidence": result.get("confidence"),
                                # Both are router inputs (rules 1 and 2) — showing
                                # them next to the router's verdict in the Trace
                                # tab is what makes a routing decision legible.
                                "post_type": result.get("post_type"),
                                "post_type_confidence": result.get("post_type_confidence"),
                                "next_stream": OUTPUT_STREAM,
                            },
                            log=log,
                        )

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
                        # Bounded retry-by-re-enqueue, then dead-letter (§8).
                        try:
                            outcome = await record_failure(
                                redis,
                                stream=INPUT_STREAM,
                                group=CONSUMER_GROUP,
                                msg_id=msg_id,
                                fields=fields,
                                error=exc,
                                max_retries=STAGE1_MAX_RETRIES,
                            )
                            log.warning(
                                "stage1_failure_handled",
                                post_id=post_id,
                                msg_id=msg_id,
                                outcome=outcome,
                            )
                        except Exception as dlq_exc:
                            log.error("stage1_dlq_failed", msg_id=msg_id, error=str(dlq_exc))
    finally:
        await redis.aclose()

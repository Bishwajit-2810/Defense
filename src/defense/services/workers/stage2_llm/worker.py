"""
Stage-2 LLM worker — reads "llm:stage2:queue" and runs LLM tasks
(summary, post-type classification, topic/intent refinement) for posts
that the router flagged as needing deeper analysis.

After processing, results are merged into partial_result and pushed to
"assembler:queue".
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import signal
import sys
import time
from typing import Any

import redis.asyncio as aioredis
import structlog
from defense.libs.common.config import get_settings

# Ensure libs/ is importable when running as a standalone container.
# The Dockerfile sets PYTHONPATH=/app, so this is only a local-dev fallback.

from defense.libs import streams  # noqa: E402
from defense.libs.labels import label_provenance  # noqa: E402
from defense.libs.stance_scoring import (  # noqa: E402
    aggregate_target_stances,
    normalize_llm_target_stances,
)
from defense.libs.stance_targets import load_targets  # noqa: E402
from defense.libs.llm import LLMClient  # noqa: E402
from defense.libs.llm.usage import LANE_COMMENT, LANE_POST, track_usage  # noqa: E402
from defense.libs.progress import publish_stage  # noqa: E402

from .cache import get_cached, set_cached  # noqa: E402
from .prompts import (  # noqa: E402
    build_comment_stance_messages,
    build_comment_summary_messages,
    build_insight_messages,
    build_post_type_messages,
    build_summary_messages,
)

# Context-aware comment stance (post + comments → per-comment stance via LLM).
# All Stage-2 text work — summary, post-type, insight, comment stance/summary —
# runs on the `stage2` model (qwen2.5:7b by default), a different, larger model
# than the Stage-1 NLP model (`stage1` = gemma3:4b). Override with
# STAGE2_LOCAL_MODEL, or per-task via COMMENT_STANCE_ROLE.
# Summarization runs on its own role, separate from classification (§6.5).
# Classification (post_type, insight, comment stance) wants a cheap, constrained
# model — it picks from a fixed vocabulary. Summarization wants a fluent one —
# it writes Bangla prose. Keeping them on separate roles is also the honest
# version of the cost story: the expensive model is spent on the one task that
# needs it, once per post, instead of on every classification call.
# `summary` defaults to the same model as `stage2`, so nothing changes until
# SUMMARY_LOCAL_MODEL / SUMMARY_GROQ_MODEL points it somewhere else.
config = get_settings()
_SUMMARY_ROLE = config.summary_role

_STANCE_ENABLED = config.comment_stance
_STANCE_ROLE = config.comment_stance_role
_STANCE_BATCH = config.comment_stance_batch
_STANCE_MAX_TEXT = 140
# 0 = no cap: EVERY non-emoji comment on a routed post gets the context-aware
# stance pass (§6.3). This used to default to 40, so Stage 2 re-labelled only
# the 40 most-liked comments per post while `sentiment_breakdown` mixed those
# with Stage-1 heuristic labels and could not say which was which.
#
# Note the reach question §6.3 step 4 raises and answers here: Stage-1 comment
# labelling runs for EVERY post, but this Stage-2 stance pass only runs for
# posts the router sent to Stage 2. So "every comment is analysed by an LLM" is
# delivered by the Stage-1 half; the context-aware STANCE upgrade still reaches
# only routed posts. That is deliberate — stance-toward-the-post is the premium
# signal and the gate is what decides which posts earn it.
#
# Set a positive value for _STANCE_MAX_PER_POST = get_settings().comment_stance_max_per_post
_STANCE_MAX_PER_POST = config.comment_stance_max_per_post
# How many concurrent batches to run per post
_STANCE_CONCURRENCY = max(1, config.comment_stance_concurrency)

_STANCE_BATCH_RETRIES = max(0, config.comment_stance_batch_retries)

# Summarize all comments across a post (Insight / Topic discovery)
_COMMENT_SUMMARY_ENABLED = config.comment_summary
# Per-task token ceilings. These used to be hardcoded literals (512 summary,
# 512 insight, 256 comment summary, 128 post_type) and nothing checked whether a
# reply had actually stopped at them. Bangla costs far more tokens per character
# than English under these tokenizers, so a fixed ceiling truncates Bangla and
# Banglish posts while sparing English ones — the wrong bias for this project.
# Defaults are raised; `LLMClient.chat` now auto-continues a length-truncated
# free-text reply and reports whatever state it ends in.
_SUMMARY_MAX_TOKENS = config.summary_max_tokens
_COMMENT_SUMMARY_MAX_TOKENS = config.comment_summary_max_tokens
_INSIGHT_MAX_TOKENS = config.insight_max_tokens
_POST_TYPE_MAX_TOKENS = config.post_type_max_tokens

_STANCE_LABELS = {"positive", "negative", "neutral"}
_STANCE_SCORE = {"positive": 0.6, "negative": -0.6, "neutral": 0.0}
# Emotion taxonomy must match libs/schemas/output_schema.json and the Stage-1
# heuristic (comment_analyzer._EMOTIONS).
_EMOTION_LABELS = {"anger", "sadness", "joy", "fear", "disgust", "surprise", "neutral"}
_EMOTION_KEYS = ("anger", "sadness", "joy", "fear", "disgust", "surprise", "neutral")

from defense.libs.common.logging import setup_logging  # noqa: E402
from defense.libs.dlq import record_failure  # noqa: E402

setup_logging("stage2")
logger = structlog.get_logger(__name__)

REDIS_URL: str = config.redis_url

# Names come from libs/streams.py so the KEDA manifests cannot drift from the
# group this worker actually creates — see §5.1 / §5.5. Stage 2 is the only
# stage where scaling changes cost or latency, and its scaler pointed at a
# group name that never existed.
STAGE2_QUEUE: str = streams.STAGE2_LLM.name
ASSEMBLER_QUEUE: str = streams.ASSEMBLER.name
CONSUMER_GROUP: str = streams.STAGE2_LLM.group
CONSUMER_NAME: str = config.stage2_consumer or f"stage2-llm-0"

BLOCK_MS: int = 5_000
COUNT: int = 10  # LLM calls are slow — keep batches small

# Bounded retry before a failed message is dead-lettered to llm:stage2:queue:dlq (§8).
STAGE2_MAX_RETRIES: int = config.stage2_max_retries


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cache_model(llm: LLMClient, role: str, backend: str) -> str:
    """Resolve the concrete model id for a (role, backend) pair, for the cache key.

    §5.10: the cache key's `model` slot used to carry the ROLE LABEL ("stage2",
    "vlm"), so the actual model id never entered the key and a 7-day entry
    written by one model was served to another. Falls back to the role label
    only if resolution fails — a degraded key is better than a failed task, but
    it must not be the normal path.
    """
    try:
        return llm.default_model(role, backend)
    except Exception as exc:
        logger.warning("cache_model_resolve_failed", role=role, backend=backend, error=str(exc))
        return role


def _task_content_hash(partial_result: dict, task: str, task_flags: dict) -> str:
    """Deterministic hash for (content + task) used as cache key."""
    caption = partial_result.get("caption") or ""
    ocr_text = partial_result.get("ocr_text") or ""
    language = partial_result.get("language") or ""
    sentiment = str(partial_result.get("overall_sentiment") or "")
    topics = ",".join(sorted(partial_result.get("topics") or []))
    target_lang = task_flags.get("target_lang") or ""
    photos = ",".join(partial_result.get("photo_urls") or [])
    raw = f"{task}:{caption}:{ocr_text}:{language}:{sentiment}:{topics}:{target_lang}:{photos}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


_IMAGE_FETCH_TIMEOUT = config.image_fetch_timeout
_IMAGE_MAX_BYTES = 4 * 1024 * 1024  # skip images larger than 4 MB


async def _fetch_images_as_data_urls(urls: list[str]) -> list[str]:
    """Download images and return them as base64 data: URLs.

    Local OpenAI-compatible servers (Ollama) cannot fetch remote URLs
    themselves, so the worker downloads the bytes and inlines them. URLs that
    fail (expired CDN links, oversized files) are skipped; an empty list means
    the caller should fall back to a text-only summary.
    """
    import base64  # noqa: PLC0415

    import httpx  # noqa: PLC0415

    data_urls: list[str] = []
    async with httpx.AsyncClient(timeout=_IMAGE_FETCH_TIMEOUT, follow_redirects=True) as client:
        for url in urls[:2]:
            try:
                resp = await client.get(url)
                resp.raise_for_status()
                if len(resp.content) > _IMAGE_MAX_BYTES:
                    logger.warning("image_too_large_skipped", url=url[:120], bytes=len(resp.content))
                    continue
                mime = resp.headers.get("content-type", "image/jpeg").split(";")[0]
                if not mime.startswith("image/"):
                    mime = "image/jpeg"
                encoded = base64.b64encode(resp.content).decode("ascii")
                data_urls.append(f"data:{mime};base64,{encoded}")
            except Exception as exc:
                logger.warning("image_fetch_failed", url=url[:120], error=str(exc))
    return data_urls


#: Which lane a call belongs to: "post" (summary/post_type/insight, once per
#: post) or "comment" (stance/comment summary, many per post). §6.7: once every
#: comment reaches the LLM the cost becomes comment-dominated and the routing
#: gate stops being the main lever — but that can only be *shown* if the
#: counters record which lane spent the tokens.
#
# Both now come from libs/llm/usage.py. They were defined here, and so was the
# only code that incremented the counters — which is why the five LLM callers
# outside this worker spent tokens nothing counted (§13.4). Fresh calls are
# recorded by `LLMClient.chat` itself now (see the `usage_lane` / `usage_task`
# arguments on the calls below); this module only still reports CACHE HITS,
# because a cache hit never reaches the client.
_track_usage = track_usage


# Sentence terminators across the three scripts this corpus writes in: the
# Bangla danda/double-danda, and Latin punctuation for English/Banglish.
_SENTENCE_ENDS = ("।", "॥", ".", "!", "?", "…")


def _trim_to_sentence(text: str, min_keep_ratio: float = 0.5) -> str:
    """Cut a still-truncated reply back to its last complete sentence.

    Last resort after auto-continuation has been exhausted: a summary that ends
    mid-word reads as a bug, one that ends a sentence early merely reads as
    short. Refuses to trim when doing so would discard more than half the text
    (a long reply with no terminator at all is better kept whole than gutted).
    """
    stripped = text.rstrip()
    if not stripped or stripped.endswith(_SENTENCE_ENDS):
        return stripped
    cut = max(stripped.rfind(end) for end in _SENTENCE_ENDS)
    if cut < 0 or (cut + 1) < len(stripped) * min_keep_ratio:
        return stripped
    return stripped[: cut + 1]


def _safe_json_parse(text: str, fallback: dict) -> dict:
    """Parse JSON from LLM output, stripping markdown fences if present."""
    stripped = text.strip()
    # Remove ```json ... ``` or ``` ... ``` fences
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        # Drop first and last fence lines
        inner = lines[1:-1] if lines[-1].startswith("```") else lines[1:]
        stripped = "\n".join(inner).strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        logger.warning("llm_json_parse_failed", raw=text[:200])
        return fallback


# ---------------------------------------------------------------------------
# Per-task LLM calls
# ---------------------------------------------------------------------------

async def _run_summary(
    llm: LLMClient,
    redis,
    partial_result: dict,
    task_flags: dict,
    role: str,
    backend_override: str | None = None,
) -> dict:
    """Generate a post summary; returns a dict of summary fields."""
    task = "summary"
    content_hash = _task_content_hash(partial_result, task, task_flags)
    backend = backend_override or get_settings().llm_backend

    target_lang = task_flags.get("target_lang") or partial_result.get("language") or "the post's language"
    # Image-grounded summary: the VLM gets the actual image bytes (base64
    # data URLs — local servers like Ollama can't fetch remote URLs) alongside
    # caption/OCR. Expired CDN links or a VLM failure degrade to a text-only
    # summary instead of failing the task.
    #
    # This runs BEFORE the cache lookup on purpose: the model that will write
    # the summary depends on whether an image was actually fetched, and the
    # model id is part of the cache key (§5.10). Keying on the requested role
    # instead would file a text-model summary under the VLM's id. The cost is
    # one wasted fetch on a cache hit for an image post — and image bytes are
    # unreachable today anyway (§5.2), so this branch does not currently run.
    image_urls: list[str] | None = None
    if role == "vlm" and partial_result.get("photo_urls"):
        image_urls = await _fetch_images_as_data_urls(partial_result["photo_urls"]) or None
    grounded_on_image = bool(image_urls)

    # The CONCRETE model id, not the role label — §5.10. Without it, switching
    # SUMMARY_LOCAL_MODEL would serve the previous model's summaries for 7 days,
    # and the moment summarization and classification run on different models
    # the key stops distinguishing them at all.
    cache_role = role if grounded_on_image else _SUMMARY_ROLE
    model_label = _cache_model(llm, cache_role, backend)

    cached = await get_cached(redis, backend, model_label, task, content_hash)
    if cached is not None:
        await _track_usage(redis, cache_hit=True, lane=LANE_POST, task="summary")
        return {**cached, "_cache_hit": True}

    messages = build_summary_messages(
        caption=partial_result.get("caption") or "",
        ocr_text=partial_result.get("ocr_text") or "",
        image_description=partial_result.get("image_description") or "",
        language=partial_result.get("language") or "unknown",
        target_lang=target_lang,
        image_urls=image_urls,
    )

    text_only_messages = build_summary_messages(
        caption=partial_result.get("caption") or "",
        ocr_text=partial_result.get("ocr_text") or "",
        image_description=partial_result.get("image_description") or "",
        language=partial_result.get("language") or "unknown",
        target_lang=target_lang,
    )

    async def _chat(chat_role: str, msgs: list[dict]) -> dict:
        return await llm.chat(
            role=chat_role,
            messages=msgs,
            backend_override=backend_override,
            max_tokens=_SUMMARY_MAX_TOKENS,
            temperature=0.2,
        
            usage_redis=redis,
            usage_lane=LANE_POST,
            usage_task="summary",
        )

    # Only actually drive the VLM when an image was genuinely fetched. A "vlm"
    # role with no fetchable image — e.g. photo_urls are relative storage paths
    # ("posts/.../x.jpg") rather than absolute URLs, or the CDN link expired — is
    # worse than useless: the local VLM is weaker than the text model at text-only
    # summarisation and frequently returns empty. So when there's no image to
    # ground on, summarise on the `stage2` model from caption + OCR directly
    # instead of burning a doomed VLM call that strands the post with a blank
    # summary.
    effective_role = role if grounded_on_image else _SUMMARY_ROLE
    chat_messages = messages if grounded_on_image else text_only_messages

    try:
        response = await _chat(effective_role, chat_messages)
    except Exception as exc:
        if effective_role != "vlm":
            raise
        logger.warning("vlm_summary_failed_falling_back_to_text", error=str(exc))
        grounded_on_image = False
        response = await _chat(_SUMMARY_ROLE, text_only_messages)

    # Local VLMs (qwen3-vl) frequently return EMPTY content without raising — that
    # left image posts with a blank summary. Whenever the VLM was actually used
    # and produced nothing, redo the summary text-only on the summary model so the
    # caption + OCR still yield a summary.
    if effective_role == "vlm" and not (response.get("content") or "").strip():
        logger.warning("vlm_summary_empty_falling_back_to_text", post_id=partial_result.get("post_id"))
        grounded_on_image = False
        response = await _chat(_SUMMARY_ROLE, text_only_messages)


    summary_text = (response.get("content") or "").strip()
    # `truncated` means the reply STILL ended at the token ceiling after
    # LLMClient exhausted its continuation budget. Trim back to the last
    # complete sentence so the text never ends mid-word, and record the fact —
    # a short summary that says it was cut is defensible; a silent half is not.
    truncated = bool(response.get("truncated"))
    if truncated:
        summary_text = _trim_to_sentence(summary_text)
        logger.warning(
            "stage2_summary_truncated",
            post_id=partial_result.get("post_id"),
            chars=len(summary_text),
            max_tokens=_SUMMARY_MAX_TOKENS,
            continuations=response.get("continuations", 0),
        )

    grounding_parts = []
    if partial_result.get("caption"):
        grounding_parts.append("caption")
    if partial_result.get("ocr_text"):
        grounding_parts.append("ocr")
    if grounded_on_image:
        grounding_parts.append("image")
    if partial_result.get("image_description"):
        grounding_parts.append("image_description")

    result = {
        "post_summary": summary_text,
        "post_summary_lang": target_lang,
        # Golden rule 10: "vlm" only when the image actually grounded it.
        "post_summary_source": "vlm" if grounded_on_image else "llm",
        "post_summary_grounding": grounding_parts,
        "post_summary_truncated": truncated,
        "_llm_model": response.get("model", model_label),
        "_llm_backend": response.get("backend", backend),
    }

    # Never cache an empty summary — otherwise the blank result is served back on
    # every retry for the same content and the post can never recover. The same
    # argument applies to a truncated one, and more forcefully: the cache TTL is
    # 7 days, so a half summary written once is served back on every subsequent
    # request for that content instead of being retried with a bigger budget.
    #
    # Nor cache when the role changed mid-flight: the VLM fallbacks above can
    # leave a summary-model answer that would be filed under the VLM's model id,
    # which is precisely the §5.10 defect (one model's output served for
    # another's request) reintroduced from the other direction. Recomputing is
    # not an option either — the next run looks up under the pre-fallback key —
    # so take the miss.
    # `effective_role` is not updated by the fallbacks above; `grounded_on_image`
    # is, so it is the reliable signal that the VLM path was abandoned after the
    # cache key had already been computed from it.
    role_changed = cache_role == "vlm" and not grounded_on_image
    if role_changed:
        logger.info(
            "summary_not_cached_role_changed",
            post_id=partial_result.get("post_id"),
            requested=cache_role, actual=effective_role,
        )
    if summary_text and not truncated and not role_changed:
        await set_cached(redis, backend, model_label, task, content_hash, result)
    return result


async def _run_post_type(
    llm: LLMClient,
    redis,
    partial_result: dict,
    task_flags: dict,
    role: str,
    backend_override: str | None = None,
) -> dict:
    """Classify post type; returns {"post_type": ..., "post_type_confidence": ...}."""
    task = "post_type"
    content_hash = _task_content_hash(partial_result, task, task_flags)
    backend = backend_override or get_settings().llm_backend
    # Concrete model id, not the role label — §5.10.
    model_label = _cache_model(llm, role, backend)

    cached = await get_cached(redis, backend, model_label, task, content_hash)
    if cached is not None:
        await _track_usage(redis, cache_hit=True, lane=LANE_POST, task="post_type")
        return {**cached, "_cache_hit": True}

    text = partial_result.get("caption") or partial_result.get("ocr_text") or ""
    messages = build_post_type_messages(
        text=text,
        language=partial_result.get("language") or "unknown",
    )

    response = await llm.chat(
        role=role,
        messages=messages,
        backend_override=backend_override,
        response_format={"type": "json_object"},
        max_tokens=_POST_TYPE_MAX_TOKENS,
        temperature=0.0,
    
        usage_redis=redis,
        usage_lane=LANE_POST,
        usage_task="post_type",
    )


    # JSON replies can't be continued token-wise into valid JSON, so a truncated
    # one falls through to the caller's fallback — but it must say so rather than
    # letting the fallback masquerade as the model's judgement.
    if response.get("truncated"):
        logger.warning(
            "stage2_post_type_truncated",
            post_id=partial_result.get("post_id"),
            max_tokens=_POST_TYPE_MAX_TOKENS,
        )

    parsed = _safe_json_parse(
        response["content"],
        fallback={"post_type": "other", "confidence": 0.5},
    )

    result = {
        "post_type": parsed.get("post_type", "other"),
        "post_type_confidence": float(parsed.get("confidence", 0.5)),
        "_llm_model": response.get("model", model_label),
        "_llm_backend": response.get("backend", backend),
    }

    if not response.get("truncated"):
        await set_cached(redis, backend, model_label, task, content_hash, result)
    return result


async def _run_insight(
    llm: LLMClient,
    redis,
    partial_result: dict,
    task_flags: dict,
    role: str,
    backend_override: str | None = None,
) -> dict:
    """Refine topics/intents; returns refined_topics, intents, insight."""
    task = "insight"
    content_hash = _task_content_hash(partial_result, task, task_flags)
    backend = backend_override or get_settings().llm_backend
    # Concrete model id, not the role label — §5.10.
    model_label = _cache_model(llm, role, backend)

    cached = await get_cached(redis, backend, model_label, task, content_hash)
    if cached is not None:
        await _track_usage(redis, cache_hit=True, lane=LANE_POST, task="insight")
        return {**cached, "_cache_hit": True}

    text = partial_result.get("caption") or partial_result.get("ocr_text") or ""
    messages = build_insight_messages(
        text=text,
        language=partial_result.get("language") or "unknown",
        sentiment=partial_result.get("overall_sentiment"),
        topics=partial_result.get("topics") or [],
    )

    response = await llm.chat(
        role=role,
        messages=messages,
        backend_override=backend_override,
        response_format={"type": "json_object"},
        max_tokens=_INSIGHT_MAX_TOKENS,
        temperature=0.1,
    
        usage_redis=redis,
        usage_lane=LANE_POST,
        usage_task="insight",
    )


    if response.get("truncated"):
        logger.warning(
            "stage2_insight_truncated",
            post_id=partial_result.get("post_id"),
            max_tokens=_INSIGHT_MAX_TOKENS,
        )

    parsed = _safe_json_parse(
        response["content"],
        fallback={"refined_topics": [], "intents": [], "insight": ""},
    )

    result = {
        "refined_topics": parsed.get("refined_topics") or [],
        "intents": parsed.get("intents") or [],
        "insight": parsed.get("insight") or "",
        "_llm_model": response.get("model", model_label),
        "_llm_backend": response.get("backend", backend),
    }

    if not response.get("truncated"):
        await set_cached(redis, backend, model_label, task, content_hash, result)
    return result


# Watchlist for the Stage-2 target-stance upgrade (stance_targets.md). Loaded
# once per process; an absent file means the feature is simply off.
_STANCE_TARGETS_PATH = config.stance_targets_file
_TARGETS = None


def _targets():
    """The loaded watchlist, cached. Failure is logged, never fatal."""
    global _TARGETS
    if _TARGETS is None:
        try:
            _TARGETS = load_targets(_STANCE_TARGETS_PATH)
        except Exception as exc:
            logger.error("stance_targets_load_failed", path=_STANCE_TARGETS_PATH, error=str(exc))
            from defense.libs.stance_targets import Targets  # noqa: PLC0415
            _TARGETS = Targets()
    return _TARGETS


def _normalize_stance(parsed: Any, n: int, valid_target_ids: set[str] | None = None) -> list:
    """Map an LLM response to a list[n] of {"s","e","t"} dicts (or None per slot).

    Each slot carries the stance ("s") and emotion ("e") the LLM returned for
    that comment number, and — when a watchlist is active — "t", the per-entity
    stances. Any field may be None when the model omits it or returns an
    out-of-taxonomy value, so the caller keeps the Stage-1 fallback.

    Target ids the model invents are dropped, not passed through: a hallucinated
    entity in a stance table is worse than a missing one.
    """
    out: list = [None] * n
    labels = parsed.get("labels") if isinstance(parsed, dict) else parsed
    if not isinstance(labels, list):
        return out
    for item in labels:
        if not isinstance(item, dict):
            continue
        idx = item.get("i", item.get("index"))
        s = str(item.get("s", item.get("sentiment", ""))).lower().strip()
        e = str(item.get("e", item.get("emotion", ""))).lower().strip()
        try:
            idx = int(idx)
        except (TypeError, ValueError):
            continue
        if not (1 <= idx <= n):
            continue
        slot = {
            "s": s if s in _STANCE_LABELS else None,
            "e": e if e in _EMOTION_LABELS else None,
        }
        if valid_target_ids:
            targets = normalize_llm_target_stances(
                item.get("t", item.get("target_stances")), valid_target_ids
            )
            if targets:
                slot["t"] = targets
        out[idx - 1] = slot
    return out


async def _run_comment_stance(
    llm: LLMClient,
    redis,
    stage1_result: dict,
    post_context: str,
    backend_override: str | None,
    progress_cb: Any = None,
) -> int:
    """Re-label every embedded comment with its STANCE TOWARD THE POST via the LLM.

    Batches comments, sends each batch (with the post as context) to the LLM,
    and overwrites each comment's ``sentiment`` (+ score, method="llm"). Mutates
    ``stage1_result["comment_analysis"]`` in place — including a recomputed
    sentiment_breakdown / method_breakdown — so the assembler persists the
    context-aware labels to Postgres + ClickHouse. Returns #comments re-labelled.

    Batches run through a bounded-concurrency queue with per-batch retry, so
    lifting COMMENT_STANCE_MAX_PER_POST to 0 (the default) does not stall the
    post: a 2,857-comment thread is ~115 batches, and running them strictly in
    sequence is what made the cap necessary in the first place.

    ``progress_cb(done, total)`` is awaited after each batch for the Trace tab.
    """
    ca = stage1_result.get("comment_analysis") or {}
    comments = ca.get("comments") or []
    if not comments:
        return 0

    backend = backend_override or get_settings().llm_backend
    # Concrete model id for the cache key — §5.10.
    stance_model = _cache_model(llm, _STANCE_ROLE, backend)
    labeled = 0

    # Emoji-only reactions never enter an LLM batch: there is no text in them to
    # judge stance from, so they are the cheapest possible tokens to waste. They
    # keep their Stage-1 emoji label (❤️/🤬 are real signal — see §6.2).
    eligible = [c for c in comments if c.get("kind") != "emoji"]
    if not eligible:
        return 0

    # Only the most-engaged comments get the premium context-aware LLM stance;
    # the rest keep their instant Stage-1 label (coverage stays 100%). These are
    # the same dict objects as in ``comments``, so mutating them updates the list.
    if 0 < _STANCE_MAX_PER_POST < len(eligible):
        targets = sorted(eligible, key=lambda c: int(c.get("likes") or 0), reverse=True)[:_STANCE_MAX_PER_POST]
    else:
        targets = eligible

    batches = [
        targets[i : i + _STANCE_BATCH] for i in range(0, len(targets), _STANCE_BATCH)
    ]
    total_batches = len(batches)

    # Watchlist for the target-stance upgrade. Only entities a batch actually
    # mentions go into its prompt — sending the whole list on every batch would
    # waste tokens and invite the model to invent mentions.
    watchlist = _targets()
    watchlist_ids = {t.id for t in watchlist.targets} if watchlist else set()
    semaphore = asyncio.Semaphore(_STANCE_CONCURRENCY)
    lock = asyncio.Lock()
    done = 0

    async def _fetch_batch(index: int, batch: list[dict]) -> list:
        """Return this batch's labels (cached, fresh, or all-None on failure)."""
        raw_key = post_context[:1500] + "||" + "|".join(
            (c.get("text") or "")[:_STANCE_MAX_TEXT] for c in batch
        )
        content_hash = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()

        cached = await get_cached(redis, backend, stance_model, "comment_stance", content_hash)
        if cached is not None and isinstance(cached.get("labels"), list) and len(cached["labels"]) == len(batch):
            await _track_usage(redis, cache_hit=True, lane=LANE_COMMENT, task="comment_stance")
            return cached["labels"]

        # Which watchlist entities this batch mentions, if any. Riding inside the
        # call that is already being made is what makes target stance free
        # against the cost model (stance_targets.md §6).
        batch_targets: list[dict] = []
        if watchlist:
            mentioned: set[str] = set()
            for c in batch:
                mentioned.update(watchlist.matched_ids(c.get("text") or ""))
            batch_targets = [
                {"id": t.id, "display": t.display, "aliases": list(t.aliases)}
                for t in watchlist.targets if t.id in mentioned
            ]

        messages = build_comment_stance_messages(
            post_context, batch, _STANCE_MAX_TEXT, targets=batch_targets or None
        )
        for attempt in range(_STANCE_BATCH_RETRIES + 1):
            try:
                resp = await llm.chat(
                    role=_STANCE_ROLE,
                    messages=messages,
                    backend_override=backend_override,
                    response_format={"type": "json_object"},
                    # ~56 tokens/comment covers {"i":N,"s":"...","e":"..."};
                    # target stances add roughly another 40 per entity mentioned.
                    max_tokens=min(
                        4096,
                        (56 + 40 * len(batch_targets)) * len(batch) + 64,
                    ),
                    temperature=0.0,
                
                    usage_redis=redis,
                    usage_lane=LANE_COMMENT,
                    usage_task="comment_stance",
                )
                results = _normalize_stance(
                    _safe_json_parse(resp["content"], {}), len(batch), watchlist_ids
                )
                await set_cached(redis, backend, stance_model, "comment_stance", content_hash, {"labels": results})
                return results
            except Exception as exc:
                import traceback; traceback.print_exc()
                if attempt < _STANCE_BATCH_RETRIES:
                    logger.warning(
                        "comment_stance_batch_retrying",
                        batch=index + 1, of=total_batches, attempt=attempt + 1, error=str(exc),
                    )
                    continue
                # Per-batch isolation: these comments keep their Stage-1 label
                # rather than costing the rest of the post its stance pass.
                logger.warning(
                    "comment_stance_batch_failed",
                    batch=index + 1, of=total_batches, comments=len(batch), error=str(exc),
                )
                return [None] * len(batch)
        return [None] * len(batch)

    async def _run_batch(index: int, batch: list[dict]) -> int:
        nonlocal done
        async with semaphore:
            results = await _fetch_batch(index, batch)

        applied = 0
        # `_normalize_stance` returns one slot per input comment, in order — the
        # merge is index-aligned and a length mismatch would attach each
        # comment's stance to its neighbour.
        if len(results) != len(batch):
            logger.warning(
                "comment_stance_length_mismatch",
                batch=index + 1, got=len(results), expected=len(batch),
            )
            results = [None] * len(batch)
        for c, label in zip(batch, results):
            if not isinstance(label, dict):
                continue
            stance = label.get("s")
            emotion = label.get("e")
            if stance in _STANCE_LABELS:
                c["sentiment"] = stance
                c["sentiment_score"] = _STANCE_SCORE[stance]
                c["method"] = "llm"
                applied += 1
            if emotion in _EMOTION_LABELS:
                # Context-aware emotion overwrites the Stage-1 heuristic guess —
                # and says so, so emotion_breakdown can report its own mix.
                c["emotion"] = emotion
                c["emotion_method"] = "llm"
            # The LLM's target verdicts replace Stage-1's deterministic ones —
            # but only for targets it actually judged. A target the model stayed
            # silent on keeps its deterministic verdict rather than vanishing.
            llm_targets = label.get("t")
            if llm_targets:
                existing = {e["target"]: e for e in (c.get("target_stances") or [])}
                for entry in llm_targets:
                    # Carry the matched alias forward — the LLM does not report
                    # it, and it is what makes a miss debuggable.
                    prior = existing.get(entry["target"])
                    if prior and prior.get("alias"):
                        entry["alias"] = prior["alias"]
                    existing[entry["target"]] = entry
                c["target_stances"] = list(existing.values())
            # else: keep the Stage-1 standalone values as a fallback

        async with lock:
            done += 1
            current = done
        if progress_cb is not None:
            try:
                await progress_cb(current, total_batches)
            except Exception as exc:  # progress must never break analysis
                logger.debug("stance_progress_failed", error=str(exc))
        return applied

    applied_counts = await asyncio.gather(
        *(_run_batch(i, b) for i, b in enumerate(batches)), return_exceptions=True
    )
    labeled = sum(n for n in applied_counts if isinstance(n, int))

    # Recompute aggregates from the (mutated) per-comment list.
    sb = {"positive": 0, "negative": 0, "neutral": 0}
    sb_sub = {"positive": 0, "negative": 0, "neutral": 0}
    reaction_only = 0
    eb = {k: 0 for k in _EMOTION_KEYS}
    mb: dict = {}
    for c in comments:
        s = c.get("sentiment") or "neutral"
        if s not in sb:
            s = "neutral"
        sb[s] += 1
        if c.get("kind") == "emoji":
            reaction_only += 1
        else:
            sb_sub[s] += 1
        e = c.get("emotion") or "neutral"
        if e not in eb:
            e = "neutral"
        eb[e] += 1
        m = c.get("method") or "fast"
        mb[m] = mb.get(m, 0) + 1
    ca["sentiment_breakdown_substantive"] = sb_sub
    ca["reaction_only"] = reaction_only
    ca["sentiment_breakdown"] = sb
    ca["emotion_breakdown"] = eb
    ca["method_breakdown"] = mb
    # Stage 2 re-labels only the top-N comments, so a single sentiment_breakdown
    # can mix Stage-1 heuristic, Stage-1 LLM and Stage-2 stance labels. Recompute
    # the provenance summary here or the chart reports Stage-1's mix for a set of
    # labels Stage 2 has since changed.
    ca["provenance"] = label_provenance(mb)
    # Recompute the per-target rollup too: Stage 2 replaced a subset of the
    # deterministic verdicts, so Stage-1's aggregate now describes labels that
    # have since changed.
    if watchlist:
        ca["target_stances"] = aggregate_target_stances(comments, watchlist)
    return labeled


async def _run_comment_summary(
    llm: LLMClient,
    redis,
    stage1_result: dict,
    post_context: str,
    target_lang: str,
    backend_override: str | None,
) -> str | None:
    """Write a short natural-language summary of how commenters reacted.

    Grounded on the (context-aware) sentiment/emotion breakdowns and a few
    representative comments — so it must run AFTER _run_comment_stance, whose
    recomputed aggregates it reads. Returns the summary text (or None).
    """
    ca = stage1_result.get("comment_analysis") or {}
    if not ca.get("comments"):
        return None

    backend = backend_override or get_settings().llm_backend
    # The comment summary is SUMMARIZATION, so it runs on the summary role —
    # the same split as the post summary (§6.5). Concrete model id in the key.
    summary_model = _cache_model(llm, _SUMMARY_ROLE, backend)
    sb = ca.get("sentiment_breakdown") or {}
    eb = ca.get("emotion_breakdown") or {}
    reps = ca.get("representative_comments") or []

    raw_key = (
        post_context[:500]
        + "||" + json.dumps(sb, sort_keys=True)
        + "||" + json.dumps(eb, sort_keys=True)
        + "||" + "|".join((c.get("text") or "")[:120] for c in reps[:4])
        + "||" + (target_lang or "")
    )
    content_hash = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()

    cached = await get_cached(redis, backend, summary_model, "comment_summary", content_hash)
    if cached is not None:
        await _track_usage(redis, cache_hit=True, lane=LANE_COMMENT, task="comment_summary")
        return cached.get("summary")

    messages = build_comment_summary_messages(post_context, sb, eb, reps, target_lang)
    resp = await llm.chat(
        # Summarization, not classification — the summary role (§6.5).
        role=_SUMMARY_ROLE,
        messages=messages,
        backend_override=backend_override,
        max_tokens=_COMMENT_SUMMARY_MAX_TOKENS,
        temperature=0.3,
    
        usage_redis=redis,
        usage_lane=LANE_COMMENT,
        usage_task="comment_summary",
    )

    summary = (resp.get("content") or "").strip()
    truncated = bool(resp.get("truncated"))
    if truncated:
        summary = _trim_to_sentence(summary)
        logger.warning(
            "stage2_comment_summary_truncated",
            chars=len(summary),
            max_tokens=_COMMENT_SUMMARY_MAX_TOKENS,
            continuations=resp.get("continuations", 0),
        )
    # Same rule as the post summary: a truncated answer must not be cached for
    # 7 days, or the half summary is what every later request gets back.
    if summary and not truncated:
        await set_cached(
            redis, backend, summary_model, "comment_summary", content_hash, {"summary": summary}
        )
    return summary or None


# ---------------------------------------------------------------------------
# Core message processing
# ---------------------------------------------------------------------------

async def _process_message(
    llm: LLMClient,
    redis,
    message_id: bytes,
    fields: dict,
) -> None:
    t0 = time.monotonic()

    raw = fields.get(b"data") or fields.get("data", b"{}")
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")

    try:
        payload: dict = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.error("stage2_parse_error", message_id=message_id, error=str(exc))
        return

    post_id: str = payload.get("post_id", "unknown")
    stage1_result: dict = payload.get("stage1_result", {})
    normalized_post: dict = payload.get("normalized_post", {})
    task_flags: dict = payload.get("task_flags", {})

    log = logger.bind(post_id=post_id)

    # Which backend this post is analysed on.
    #
    # The job envelope wins. The API resolves the backend when it enqueues —
    # request option > global toggle > env — and applies the tenant's privacy
    # lock to the RESULT, which is the only place that can be done: this worker
    # has no database and no tenant, so it cannot evaluate a policy itself.
    #
    # Reading the global toggle first, as this used to, is what made the lock
    # unenforceable for the pipeline: a privacy-locked tenant's posts followed
    # whatever the dashboard switch said, and the per-request `llm_backend`
    # option the policy check guarded was read by nobody (§13.5).
    options: dict = payload.get("options") or {}
    backend_override: str | None = None
    stamped = options.get("llm_backend")
    if stamped in ("local", "groq"):
        backend_override = stamped
    else:
        # No stamp: an envelope from before this landed, or one XADDed by hand.
        # Fall back to the toggle, which is the old behaviour.
        try:
            raw_override = await redis.get("config:llm_backend")
            if raw_override:
                decoded = raw_override.decode() if isinstance(raw_override, bytes) else raw_override
                if decoded in ("local", "groq"):
                    backend_override = decoded
        except Exception as exc:
            log.warning("llm_backend_override_read_failed", error=str(exc))

    log.info("stage2_processing", task_flags=task_flags, backend_override=backend_override)

    job_id = payload.get("job_id")
    await publish_stage(
        redis, "stage2", "running",
        job_id=job_id, post_id=post_id,
        detail={
            "tasks": [k for k, v in task_flags.items() if v is True],
            "target_lang": task_flags.get("target_lang"),
            "backend": backend_override or get_settings().llm_backend,
        },
        log=log,
    )

    # Build a working view the per-task prompts can read caption/OCR from.
    image_analysis = stage1_result.get("image_analysis") or {}
    partial_result: dict = {
        **stage1_result,
        "caption": normalized_post.get("caption"),
        "ocr_text": image_analysis.get("ocr_text"),
        "image_description": image_analysis.get("description"),
        "photo_urls": normalized_post.get("photo_urls") or [],
    }

    # Choose the summary role: VLM for image posts (photo_urls present), the
    # `summary` model otherwise. Set VLM_SUMMARY=false to skip image grounding
    # entirely — useful when the local VLM is weak/unavailable (e.g. returns
    # empty), so every post still gets a text+OCR-grounded summary without burning
    # a wasted VLM call first. Post-type/insight are CLASSIFICATION tasks and
    # always use `stage2` (never the VLM, never the summary model) — that split
    # is the point of §6.5.
    has_photos = bool(normalized_post.get("photo_urls"))
    vlm_enabled = get_settings().vlm_summary
    role = "vlm" if (has_photos and vlm_enabled) else _SUMMARY_ROLE

    # Which concrete model each role resolves to on this backend. Recorded so a
    # summary can always be attributed to the model that wrote it — necessary
    # once summarization and classification are no longer the same model.
    _backend_now = backend_override or get_settings().llm_backend
    role_models = {
        r: _cache_model(llm, r, _backend_now)
        for r in (_SUMMARY_ROLE, "stage2", _STANCE_ROLE)
    }
    if role == "vlm":
        role_models["vlm"] = _cache_model(llm, "vlm", _backend_now)

    stage2_result: dict = {}

    # Post context for the comment lane — derived ONLY from the post's own
    # content (caption → OCR → image description), NEVER from the generated
    # summary. This decoupling is what lets the comment lane run concurrently
    # with the summary lane below instead of waiting for the summary first.
    post_context = (
        (normalized_post.get("caption") or "").strip()
        or (partial_result.get("ocr_text") or "").strip()
        or (partial_result.get("image_description") or "").strip()
    )

    # ---- Lane A: post-level analysis (summary + post-type + insight) ----
    # All run on the stage2 / vlm slot, sequentially within the lane.
    async def _post_level_lane() -> dict:
        out: dict = {}
        backend = model = None

        # Each task logs on the way out with its own latency and whether the
        # answer came from the Redis response cache — the two numbers that explain
        # Stage-2 cost. `_cache_hit` is set by the _run_* helpers.
        if task_flags.get("want_summary", False):
            t = time.monotonic()
            try:
                summary_data = await _run_summary(llm, redis, partial_result, task_flags, role, backend_override)
                out["post_summary"] = summary_data.get("post_summary")
                out["post_summary_lang"] = summary_data.get("post_summary_lang")
                # "vlm" when the image grounded the summary, "llm" otherwise
                # (including VLM→text fallback) — set inside _run_summary.
                out["post_summary_source"] = summary_data.get(
                    "post_summary_source", "vlm" if has_photos else "llm"
                )
                grounding = summary_data.get("post_summary_grounding")
                if isinstance(grounding, (list, tuple)):
                    grounding = "+".join(grounding) if grounding else None
                out["post_summary_grounding"] = grounding
                out["post_summary_truncated"] = bool(summary_data.get("post_summary_truncated"))
                backend = backend or summary_data.get("_llm_backend")
                model = model or summary_data.get("_llm_model")
                log.info(
                    "stage2_summary_done",
                    role=role,
                    chars=len(out.get("post_summary") or ""),
                    lang=out.get("post_summary_lang"),
                    source=out.get("post_summary_source"),
                    grounding=grounding,
                    truncated=out["post_summary_truncated"],
                    cache_hit=bool(summary_data.get("_cache_hit")),
                    ms=round((time.monotonic() - t) * 1000, 1),
                )
            except Exception as exc:
                log.error("stage2_summary_error", error=str(exc),
                          ms=round((time.monotonic() - t) * 1000, 1))

        if task_flags.get("want_post_type", False):
            t = time.monotonic()
            try:
                pt_data = await _run_post_type(llm, redis, partial_result, task_flags, "stage2", backend_override)
                out["post_type"] = pt_data.get("post_type")
                out["post_type_confidence"] = pt_data.get("post_type_confidence")
                backend = backend or pt_data.get("_llm_backend")
                model = model or pt_data.get("_llm_model")
                log.info(
                    "stage2_post_type_done",
                    post_type=out.get("post_type"),
                    confidence=out.get("post_type_confidence"),
                    # Length of the text the classifier actually saw — a 0 here
                    # means it was asked to classify nothing.
                    input_chars=len(
                        (partial_result.get("caption") or partial_result.get("ocr_text") or "")
                    ),
                    cache_hit=bool(pt_data.get("_cache_hit")),
                    ms=round((time.monotonic() - t) * 1000, 1),
                )
            except Exception as exc:
                log.error("stage2_post_type_error", error=str(exc),
                          ms=round((time.monotonic() - t) * 1000, 1))

        if task_flags.get("want_insight", False):
            t = time.monotonic()
            try:
                insight_data = await _run_insight(llm, redis, partial_result, task_flags, "stage2", backend_override)
                if insight_data.get("refined_topics"):
                    out["topics"] = insight_data["refined_topics"]
                out["intents"] = insight_data.get("intents") or []
                out["insight"] = insight_data.get("insight") or ""
                backend = backend or insight_data.get("_llm_backend")
                model = model or insight_data.get("_llm_model")
                log.info(
                    "stage2_insight_done",
                    refined_topics=insight_data.get("refined_topics") or [],
                    intents=out.get("intents"),
                    insight_chars=len(out.get("insight") or ""),
                    cache_hit=bool(insight_data.get("_cache_hit")),
                    ms=round((time.monotonic() - t) * 1000, 1),
                )
            except Exception as exc:
                log.error("stage2_insight_error", error=str(exc),
                          ms=round((time.monotonic() - t) * 1000, 1))

        out["_llm_backend"] = backend
        out["_llm_model"] = model
        return out

    # ---- Lane B: comment analysis (context-aware stance → comment summary) ----
    # Runs on the stage2 slot. Re-labels every embedded comment by its stance
    # toward the post (batched) and mutates stage1_result["comment_analysis"] in
    # place so the assembler persists the context-aware labels. The comment
    # summary must run AFTER stance (it reads the recomputed breakdowns), so this
    # lane stays internally sequential — but the whole lane runs concurrently
    # with Lane A.
    async def _comment_lane() -> dict:
        out: dict = {}
        if not _STANCE_ENABLED:
            return out
        ca = stage1_result.get("comment_analysis") or {}
        if not ca.get("comments"):
            return out

        try:
            # With the cap lifted a large thread is ~115 batches — minutes on one
            # post. A frame per batch keeps the Trace tab from looking hung.
            async def _stance_progress(batch_done: int, batch_total: int) -> None:
                await publish_stage(
                    redis, "stage2", "running",
                    job_id=job_id, post_id=post_id,
                    detail={
                        "phase": "comment_stance",
                        "batch": batch_done,
                        "batches": batch_total,
                    },
                    log=log,
                )

            n_labeled = await _run_comment_stance(
                llm, redis, stage1_result, post_context, backend_override,
                _stance_progress,
            )
            log.info("stage2_comment_stance", labeled=n_labeled,
                     total=len(ca.get("comments") or []),
                     reaction_only=ca.get("reaction_only"))
            out["_llm_backend"] = backend_override or get_settings().llm_backend
        except Exception as exc:
            log.error("stage2_comment_stance_error", error=str(exc))

        if _COMMENT_SUMMARY_ENABLED:
            try:
                summary_lang = (task_flags.get("target_lang")
                                or stage1_result.get("language") or "English")
                comment_summary = await _run_comment_summary(
                    llm, redis, stage1_result, post_context, summary_lang, backend_override
                )
                if comment_summary:
                    ca["summary"] = comment_summary
                    ca["summary_source"] = "llm"
                    log.info("stage2_comment_summary", chars=len(comment_summary))
            except Exception as exc:
                log.error("stage2_comment_summary_error", error=str(exc))
        return out

    # Run the two lanes concurrently. Lane A writes only to its returned dict;
    # Lane B mutates stage1_result["comment_analysis"] in place. They share no
    # mutable state, so there is no race under asyncio's cooperative scheduling.
    post_out, comment_out = await asyncio.gather(_post_level_lane(), _comment_lane())

    llm_backend = post_out.pop("_llm_backend", None) or comment_out.pop("_llm_backend", None)
    llm_model = post_out.pop("_llm_model", None)
    stage2_result.update(post_out)

    elapsed_ms = int((time.monotonic() - t0) * 1000)
    stage2_result["processing"] = {
        "stage2_ms": elapsed_ms,
        "llm_backend": llm_backend,
        "llm_model": llm_model,
        # Per-role model resolution, so "which model wrote this summary?" is
        # answerable from the result rather than from the environment (§6.5).
        "role_models": role_models,
    }

    payload["stage2_result"] = stage2_result
    payload.pop("task_flags", None)
    await redis.xadd(ASSEMBLER_QUEUE, {"data": json.dumps(payload, ensure_ascii=False)})
    log.info("stage2_done", elapsed_ms=elapsed_ms, destination="assembler:queue")

    summary = stage2_result.get("post_summary") or ""
    ca_out = stage1_result.get("comment_analysis") or {}
    await publish_stage(
        redis, "stage2", "done",
        job_id=job_id, post_id=post_id,
        ms=elapsed_ms,
        detail={
            "post_type": stage2_result.get("post_type"),
            "post_type_confidence": stage2_result.get("post_type_confidence"),
            # The summary can be long; send a prefix for the trace and let the
            # tab fetch the full canonical result when the post lands.
            "post_summary_preview": summary[:220],
            "post_summary_chars": len(summary),
            "post_summary_lang": stage2_result.get("post_summary_lang"),
            "post_summary_source": stage2_result.get("post_summary_source"),
            "grounding": stage2_result.get("post_summary_grounding"),
            "post_summary_truncated": bool(stage2_result.get("post_summary_truncated")),
            "role": role,
            "llm_backend": llm_backend,
            "llm_model": llm_model,
            # Visible in the Trace tab: which model each role resolved to.
            "role_models": role_models,
            "comment_summary_added": bool(ca_out.get("summary")),
            "next_stream": ASSEMBLER_QUEUE,
        },
        log=log,
    )


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

async def _ensure_group(redis, stream: str, group: str) -> None:
    try:
        await redis.xgroup_create(stream, group, id="0", mkstream=True)
        logger.info("consumer_group_created", stream=stream, group=group)
    except aioredis.ResponseError as exc:
        if "BUSYGROUP" not in str(exc):
            raise


async def run() -> None:
    """Main event loop: consume llm:stage2:queue forever."""
    redis = aioredis.from_url(REDIS_URL, decode_responses=False)
    llm = LLMClient()

    await _ensure_group(redis, STAGE2_QUEUE, CONSUMER_GROUP)

    logger.info(
        "stage2_llm_started",
        stream=STAGE2_QUEUE,
        group=CONSUMER_GROUP,
        consumer=CONSUMER_NAME,
    )

    shutdown = asyncio.Event()

    def _handle_signal(*_):
        logger.info("stage2_shutdown_signal")
        shutdown.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _handle_signal)

    try:
        while not shutdown.is_set():
            try:
                results = await redis.xreadgroup(
                    groupname=CONSUMER_GROUP,
                    consumername=CONSUMER_NAME,
                    streams={STAGE2_QUEUE: ">"},
                    count=COUNT,
                    block=BLOCK_MS,
                )
            except asyncio.CancelledError:
                break
            except Exception as exc:
                msg = str(exc)
                # Idle BLOCK window with no new messages → redis TimeoutError; normal.
                if isinstance(exc, asyncio.TimeoutError) or "Timeout" in msg:
                    logger.debug("stage2_read_idle")
                    continue
                logger.error("stage2_read_error", error=msg)
                if "NOGROUP" in str(exc):
                    # Stream/group wiped at runtime (e.g. FLUSHALL) — re-create
                    # the group instead of error-looping forever.
                    try:
                        await _ensure_group(redis, STAGE2_QUEUE, CONSUMER_GROUP)
                    except Exception as group_exc:
                        logger.error("stage2_group_recreate_failed", error=str(group_exc))
                await asyncio.sleep(1)
                continue

            if not results:
                continue

            for _stream, messages in results:
                for message_id, fields in messages:
                    try:
                        await _process_message(llm, redis, message_id, fields)
                        await redis.xack(STAGE2_QUEUE, CONSUMER_GROUP, message_id)
                    except Exception as exc:
                        logger.error(
                            "stage2_message_error",
                            message_id=message_id,
                            error=str(exc),
                        )
                        # Bounded retry, then dead-letter (§8) — never silently drop.
                        try:
                            outcome = await record_failure(
                                redis,
                                stream=STAGE2_QUEUE,
                                group=CONSUMER_GROUP,
                                msg_id=message_id,
                                fields=fields,
                                error=exc,
                                max_retries=STAGE2_MAX_RETRIES,
                            )
                            logger.warning(
                                "stage2_failure_handled",
                                message_id=message_id,
                                outcome=outcome,
                            )
                        except Exception as dlq_exc:
                            logger.error(
                                "stage2_dlq_failed",
                                message_id=message_id,
                                error=str(dlq_exc),
                            )
    finally:
        logger.info("stage2_llm_stopped")
        await redis.aclose()


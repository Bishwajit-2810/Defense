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
import logging
import signal
import threading
import time
from typing import Any

import redis.asyncio as aioredis
import structlog
from defense.libs.common.config import apply_hf_offline_policy, get_settings

# Ensure libs/ is importable when running as a standalone container.
# The Dockerfile sets PYTHONPATH=/app, so this is only a local-dev fallback.

from defense.libs import streams  # noqa: E402
from defense.libs.labels import label_provenance  # noqa: E402
from defense.libs.stance_scoring import (  # noqa: E402
    aggregate_target_stances,
    normalize_llm_target_stances,
)
from defense.libs.stance_targets import load_targets, watchlist_verdict  # noqa: E402
from defense.libs import comment_groups  # noqa: E402
from defense.libs.ensemble import (  # noqa: E402
    LLM_SOURCE,
    UNCERTAIN,
    Verdict,
    agreement_summary,
    combine,
    should_escalate,
)
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
#: The two cheap multilingual heads that vote alongside the heuristic and the
#: LLM. Off turns the ensemble back into "Stage-1 heuristic + LLM" without
#: changing any other behaviour — the merge simply sees fewer voters.
_CLASSIFIERS_ENABLED = config.stage2_classifiers_enabled
#: Keep textless comments (emoji reactions, bare links) out of LLM batches.
_FILTER_EMOJI_ONLY = config.filter_emoji_only
#: "all" (default) — every comment with text gets an LLM verdict, so the UI can
#: show LLM / XLM-R / DistilBERT side by side on the same comment.
#: "escalate" — only where the cheap voters disagree. See _comment_lane.
_COMMENT_LLM_MODE = (config.comment_llm_mode or "all").strip().lower()
#: Let an identical (post-normalisation) comment reuse its twin's LLM verdict.
_DEDUP_PROPAGATE = config.comment_dedup_propagate
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

# Before ANY model load: transformers reads TRANSFORMERS_OFFLINE at import
# time, so this has to happen while every transformers import in the tree is
# still lazy. See libs/common/config.apply_hf_offline_policy.
apply_hf_offline_policy()
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
CONSUMER_NAME: str = config.stage2_consumer or "stage2-llm-0"

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



def _map_sentiment_label(label: str) -> str | None:
    """Map a HF head's label to the taxonomy — or None when it doesn't map.

    Returning "neutral" for an unrecognised label (``LABEL_0``, ``LABEL_1`` —
    what a head with no id2label mapping emits) silently turned every comment
    neutral and called it a model verdict. An unmappable label is not a
    measurement, so it is not a vote: None means this source abstains and the
    ensemble carries on with the voters that did speak.
    """
    label = (label or "").lower()
    if "pos" in label or "4 star" in label or "5 star" in label:
        return "positive"
    if "neg" in label or "1 star" in label or "2 star" in label:
        return "negative"
    if "neu" in label or "3 star" in label:
        return "neutral"
    return None


#: Signed score for the ensemble: positive/negative carry the head's confidence
#: with a sign, neutral carries 0. Keeps every voter on one comparable scale.
def _signed_score(label: str, confidence: float) -> float:
    try:
        conf = max(0.0, min(1.0, float(confidence)))
    except (TypeError, ValueError):
        conf = 0.0
    if label == "positive":
        return round(conf, 3)
    if label == "negative":
        return round(-conf, 3)
    return 0.0


_PIPELINES: dict[str, Any] = {}
_PIPELINE_FAILED: set[str] = set()
#: Serialises construction — the two classifiers load concurrently from the
#: executor and must not race each other for the last of the GPU's memory.
_PIPELINE_LOCK = threading.Lock()

def _is_cached(model_id: str) -> bool:
    """True when this checkpoint is already in the local HF cache.

    Asked BEFORE loading, because the obvious alternatives do not work:
    `HF_HUB_OFFLINE` is read by transformers at *import* time, so setting it
    later is ignored (the load quietly hits the network instead), and passing
    `local_files_only` through `model_kwargs` raises "got multiple values".
    Checking the cache directly is the only form that actually holds — and it
    is what lets MODEL_STUB_MODE mean "download nothing" while still using
    weights the machine already has.

    Fails OPEN (returns True) if the cache API is unavailable: a wrong "yes"
    costs a download, a wrong "no" silently removes a labeller.
    """
    try:
        from huggingface_hub import try_to_load_from_cache  # noqa: PLC0415
    except Exception:
        return True
    try:
        for filename in ("config.json", "model.safetensors", "pytorch_model.bin"):
            hit = try_to_load_from_cache(repo_id=model_id, filename=filename)
            if isinstance(hit, str):
                return True
        return False
    except Exception:
        return True

#: How many comment texts go into one forward pass. The whole thread used to be
#: passed as a single call, which on a 2,857-comment post is a multi-GB
#: allocation before the first token is embedded.
_CLASSIFIER_BATCH: int = 64
#: Chars of comment text a small encoder sees (its window is 512 tokens).
_CLASSIFIER_MAX_CHARS: int = 512


def _get_pipeline(name: str, model_id: str):
    """Lazily build a HF sentiment pipeline, once, and never retry a failure.

    In MODEL_STUB_MODE the weights are loaded **only if already cached**
    (`local_files_only`). That mode's promise is "no GPU, no downloads" — not
    "refuse models you already have. Returning None outright meant the two
    classifiers never ran in the default `run_all.py` configuration, so the UI
    showed a dash for both on every comment while the checkpoints sat in the
    local HF cache.

    **Falls back to CPU when the GPU is full.** The GPU is normally already
    hosting the LLM: on a 4 GB card serving qwen2.5:7b there is ~285 MB left,
    the first classifier takes it and the second dies with CUDA OOM. These are
    ~135M-parameter encoders — CPU is perfectly serviceable for them, and a
    working third opinion beats an absent one. `STAGE2_CLASSIFIER_DEVICE=cpu`
    skips the GPU attempt entirely, which is the right setting when the card is
    dedicated to the LLM.

    Construction is serialised: both classifiers load concurrently from the
    executor, and letting them race for the last few hundred MB of VRAM is how
    a fallback that works in isolation still loses one of the two.
    """
    if name in _PIPELINE_FAILED:
        return None
    if name in _PIPELINES:
        return _PIPELINES[name]

    with _PIPELINE_LOCK:
        # Re-check: another thread may have built it while we waited.
        if name in _PIPELINES:
            return _PIPELINES[name]
        if name in _PIPELINE_FAILED:
            return None

        try:
            import torch  # noqa: PLC0415 — heavy, and optional
            from transformers import pipeline  # noqa: PLC0415
        except Exception as exc:
            _PIPELINE_FAILED.add(name)
            logger.error("classifier_import_failed", classifier=name, error=str(exc))
            return None

        settings = get_settings()
        # Stub mode = "download nothing", not "use nothing". Cached weights are
        # still loaded; an absent checkpoint is skipped rather than fetched, so
        # the documented no-weights run never turns into a 1 GB download.
        if settings.model_stub_mode and not _is_cached(model_id):
            _PIPELINE_FAILED.add(name)
            logger.info(
                "classifier_skipped_not_cached",
                classifier=name, model=model_id,
                hint="MODEL_STUB_MODE=true downloads nothing; pre-fetch the "
                     "checkpoint or set MODEL_STUB_MODE=false to allow it",
            )
            return None

        pref = (settings.stage2_classifier_device or "auto").strip().lower()
        if pref == "cpu":
            devices = [-1]
        elif pref == "cuda":
            devices = [0]
        else:
            devices = [0, -1] if torch.cuda.is_available() else [-1]

        for device in devices:
            try:
                _PIPELINES[name] = pipeline(
                    "sentiment-analysis", model=model_id, device=device
                )
                logger.info(
                    "classifier_loaded",
                    classifier=name, model=model_id,
                    device="cuda" if device >= 0 else "cpu",
                )
                return _PIPELINES[name]
            except Exception as exc:
                last = device == devices[-1]
                logger.log(
                    logging.ERROR if last else logging.WARNING,
                    "classifier_load_failed" if last else "classifier_load_retrying_on_cpu",
                    classifier=name, model=model_id,
                    device="cuda" if device >= 0 else "cpu", error=str(exc),
                )
                if last:
                    # Once, not once per post.
                    _PIPELINE_FAILED.add(name)
        return None


async def _run_hf_classifier(comments: list[dict], name: str, model_id: str) -> int:
    """Label ``comments`` with one HF head, writing ``parallel_labels[name]``.

    Returns the number of comments this source actually voted on. A comment gets
    no entry at all when the model is unavailable or the label doesn't map —
    an absent voter, not a neutral one.
    """
    eligible = [c for c in comments if c.get("kind") not in ("emoji", "filtered", "link")]
    if not eligible:
        return 0

    pipe = _get_pipeline(name, model_id)
    if pipe is None:
        return 0

    texts = [
        ((c.get("text_norm") or c.get("text") or "")[:_CLASSIFIER_MAX_CHARS])
        for c in eligible
    ]

    loop = asyncio.get_running_loop()

    def _run() -> list:
        out: list = []
        for i in range(0, len(texts), _CLASSIFIER_BATCH):
            chunk = texts[i : i + _CLASSIFIER_BATCH]
            try:
                out.extend(pipe(chunk, truncation=True))
            except Exception as exc:
                logger.error(
                    "classifier_inference_failed",
                    classifier=name, batch_start=i, size=len(chunk), error=str(exc),
                )
                # This chunk abstains; the rest of the thread still gets labelled.
                out.extend([None] * len(chunk))
        return out

    results = await loop.run_in_executor(None, _run)

    voted = 0
    for c, res in zip(eligible, results):
        if not isinstance(res, dict):
            continue
        label = _map_sentiment_label(res.get("label", ""))
        if label is None:
            continue
        c.setdefault("parallel_labels", {})[name] = {
            "sentiment": label,
            "score": _signed_score(label, res.get("score", 0.0)),
            "confidence": round(float(res.get("score") or 0.0), 3),
        }
        voted += 1
    return voted


async def _run_llm_comment_labeling(
    llm: LLMClient,
    redis,
    selected: list[dict],
    post_summary: str,
    backend_override: str | None,
    progress_cb: Any = None,
) -> int:
    """Label ``selected`` comments with their STANCE TOWARD THE POST via the LLM.

    Writes each verdict into ``comment["parallel_labels"]["llm"]`` — one opinion
    among several. It does NOT decide the comment's final label and it no longer
    recomputes the thread's aggregates: both belong to the ensemble merge in
    ``_comment_lane``, which is the single place that sees every voter. Doing it
    here as well is what produced two disagreeing breakdowns in one payload.

    ``selected`` is chosen by the caller (see ensemble.should_escalate) — the
    comments where the cheap voters disagreed, had nothing to say, or where a
    watchlist entity is at stake. Returns #comments the LLM actually voted on.

    Batches run through a bounded-concurrency queue with per-batch retry, so
    lifting COMMENT_STANCE_MAX_PER_POST to 0 (the default) does not stall the
    post: a 2,857-comment thread is ~115 batches, and running them strictly in
    sequence is what made the cap necessary in the first place.

    ``progress_cb(done, total)`` is awaited after each batch for the Trace tab.
    """
    if not selected:
        return 0

    backend = backend_override or get_settings().llm_backend
    # Concrete model id for the cache key — §5.10.
    stance_model = _cache_model(llm, _STANCE_ROLE, backend)
    labeled = 0

    # `selected` is exactly what the caller decided should get a call —
    # including COMMENT_STANCE_MAX_PER_POST, which is applied in the lane so
    # that the comments it drops are recorded rather than silently absent.
    targets = list(selected)

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
        # Key on exactly what the model will read, not on the raw text — two
        # comments differing only in emoji produce one prompt and must share
        # one cache entry.
        raw_key = post_summary[:1500] + "||" + "|".join(
            (c.get("text_norm") or c.get("text") or "")[:_STANCE_MAX_TEXT] for c in batch
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
            post_summary, batch, _STANCE_MAX_TEXT, targets=batch_targets or None
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
            if "parallel_labels" not in c:
                c["parallel_labels"] = {}
            llm_data = {}
            if stance in _STANCE_LABELS:
                llm_data["sentiment"] = stance
                llm_data["sentiment_score"] = _STANCE_SCORE[stance]
                applied += 1
            if emotion in _EMOTION_LABELS:
                llm_data["emotion"] = emotion
            llm_targets = label.get("t")
            if llm_targets:
                llm_data["target_stances"] = llm_targets
            c["parallel_labels"]["llm"] = llm_data

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
    for outcome in applied_counts:
        if isinstance(outcome, BaseException):
            logger.warning("comment_stance_batch_crashed", error=str(outcome))
    labeled = sum(n for n in applied_counts if isinstance(n, int))
    # No aggregate recomputation here — the ensemble merge in _comment_lane owns
    # every aggregate, because it is the only place that has seen every voter.
    return labeled


#: Stage-1 methods that represent an actual reading of the comment. `stub` is
#: the deterministic hash fallback used when no model could be loaded — the
#: schema's own words are "it is reproducible, and it is not sentiment" — and
#: `failed` is the absence of a label. Neither may vote: a hash of the text
#: dressed up as an opinion is exactly the fabricated-neutral problem in
#: another costume, and on a real Bangla thread it is the MAJORITY of Stage-1
#: labels (measured: 11 of 12 comments), so it would dominate the ensemble.
_VOTING_STAGE1_METHODS = frozenset({"fast", "emoji", "model", "llm", "link"})


def _seed_heuristic_vote(comment: dict) -> None:
    """Make Stage 1's own label a first-class voter — when it is a real one.

    Stage 1 labelled every comment (emoji/lexicon, a small model, or its own LLM
    pass). That verdict used to be the comment's `sentiment` and nothing else —
    invisible to the ensemble, then silently overwritten by an aggregate built
    from a different source. It votes now, under the name of whatever produced
    it, unless that was the hash stub.
    """
    label = comment.get("sentiment")
    if label not in ("positive", "negative", "neutral"):
        return
    method = comment.get("method") or "fast"
    if method not in _VOTING_STAGE1_METHODS:
        return
    comment.setdefault("parallel_labels", {}).setdefault(
        "heuristic",
        {
            "sentiment": label,
            "sentiment_score": comment.get("sentiment_score", 0.0),
            "via": method,
        },
    )


def _merge_ensemble(
    ca: dict,
    watchlist,
    *,
    escalation_reasons: dict[str, int],
    dedup_stats: dict,
    voters_used: list[str],
) -> dict:
    """Collapse every voter into one label per comment, then recompute the
    aggregates ONCE from those labels.

    This is the single writer of ``sentiment`` / ``sentiment_score`` /
    ``method`` / the breakdowns. Previously three places wrote them — Stage 1,
    the stance pass, and the comment lane — and the last writer won, which is
    how a post could report eight negative comments while every comment in the
    list read neutral.
    """
    comments = ca.get("comments") or []
    verdicts: list[Verdict] = []

    sb = {"positive": 0, "negative": 0, "neutral": 0, "uncertain": 0}
    sb_sub = {"positive": 0, "negative": 0, "neutral": 0, "uncertain": 0}
    eb = {k: 0 for k in _EMOTION_KEYS}
    mb: dict[str, int] = {}
    reaction_only = 0

    for c in comments:
        labels = c.get("parallel_labels") or {}
        verdict = combine(labels)
        verdicts.append(verdict)

        if verdict.voters:
            c["sentiment"] = verdict.label
            c["sentiment_score"] = round(verdict.score, 3)
            c["label_agreement"] = round(verdict.agreement, 3)
            # How many labellers actually spoke, and who. Without these,
            # `label_agreement: 1.0` renders as "100% agree" for a comment only
            # ONE model read — a single opinion presented as a consensus.
            c["label_voters"] = verdict.voters
            c["label_sources"] = list(verdict.sources)
            if verdict.tie_broken_by:
                c["tie_broken_by"] = verdict.tie_broken_by
            if c.get("label_source") != "propagated":
                c["label_source"] = "ensemble"
                # `method` keeps naming the most authoritative source that voted,
                # so method_breakdown/provenance still answer "did a model read
                # this?" — with `ensemble` reserved for multi-voter agreement.
                c["method"] = "ensemble" if verdict.voters > 1 else (
                    "llm" if LLM_SOURCE in labels else (c.get("method") or "fast")
                )
            else:
                c["method"] = "propagated"
        else:
            # NOBODY read this comment. Stage 1 fell back to the hash stub (or
            # produced nothing), the classifiers were unavailable, and no LLM
            # verdict came back. Leaving Stage 1's label in place here is what
            # let the stub keep the last word after issue 13 stopped it voting:
            # `_seed_heuristic_vote` correctly declined the vote, but the hash
            # label still became the comment's `sentiment` and was counted in
            # `sentiment_breakdown` as a measurement, while `ensemble.abstained`
            # simultaneously reported that every comment had abstained. Two
            # contradicting aggregates in one payload — the same defect the
            # single-writer merge exists to prevent.
            #
            # An unread comment is `uncertain`, which is exactly what that label
            # is for. `method` is left alone so `provenance` still reports which
            # cheap path ran, and `label_voters: 0` tells the UI why no label is
            # claimed.
            c["sentiment"] = UNCERTAIN
            c["sentiment_score"] = 0.0
            c["label_agreement"] = 0.0
            c["label_voters"] = 0
            c["label_sources"] = []

        # Emotion: the LLM's when it gave one, else Stage 1's heuristic.
        llm_emotion = (labels.get(LLM_SOURCE) or {}).get("emotion")
        if llm_emotion in _EMOTION_LABELS:
            c["emotion"] = llm_emotion
            c["emotion_method"] = "llm"

        s = c.get("sentiment") if c.get("sentiment") in sb else "neutral"
        sb[s] += 1
        if c.get("kind") == "emoji":
            reaction_only += 1
        else:
            sb_sub[s] += 1
        e = c.get("emotion") if c.get("emotion") in eb else "neutral"
        c["emotion"] = e
        eb[e] += 1
        m = c.get("method") or "fast"
        mb[m] = mb.get(m, 0) + 1

    ca["sentiment_breakdown"] = sb
    ca["sentiment_breakdown_substantive"] = sb_sub
    ca["reaction_only"] = reaction_only
    ca["emotion_breakdown"] = eb
    ca["method_breakdown"] = mb
    ca["provenance"] = label_provenance(mb)

    # Per-target rollup, recomputed from the merged per-comment stances (the LLM
    # replaced a subset of the deterministic verdicts, so Stage 1's aggregate now
    # describes labels that have since changed).
    if watchlist:
        ca["target_stances"] = aggregate_target_stances(comments, watchlist)

    escalated = sum(escalation_reasons.get(r, 0) for r in (
        "cheap_disagreement", "insufficient_cheap_voters", "watchlist_mention", "llm_all",
    ))
    # Comments that actually carry an LLM verdict, counted from the labels
    # themselves rather than from the intent — a batch can fail, and a post
    # whose stance pass half-failed must not report full LLM coverage.
    llm_labelled = sum(
        1 for c in comments if (c.get("parallel_labels") or {}).get(LLM_SOURCE)
    )
    summary = agreement_summary(verdicts)
    summary.update({
        "escalated": escalated,
        "escalated_share": round(escalated / len(comments), 4) if comments else 0.0,
        "escalation_reasons": dict(escalation_reasons),
        "llm_labelled": llm_labelled,
        "llm_share": round(llm_labelled / len(comments), 4) if comments else 0.0,
        "capped_out": escalation_reasons.get("capped_by_max_per_post", 0),
        "mode": _COMMENT_LLM_MODE,
        "deduplicated": dedup_stats.get("duplicates", 0),
        "duplicate_share": dedup_stats.get("duplicate_share", 0.0),
        "voters": voters_used,
    })
    ca["ensemble"] = summary
    return summary


def _merge_llm_target_stances(comment: dict) -> None:
    """Fold the LLM's per-entity stances into the comment's own field.

    The LLM writes them into ``parallel_labels.llm.target_stances``; the
    watchlist rollup and the alert both read ``comment["target_stances"]``. With
    no merge the two never met: tokens were spent on target stance (40 per
    entity per comment) and the result reached neither the alert nor the
    aggregate. LLM verdicts replace the deterministic one for the same target
    and are tagged ``method: "llm"`` so the upgrade stays visible.
    """
    llm_targets = ((comment.get("parallel_labels") or {}).get(LLM_SOURCE) or {}).get(
        "target_stances"
    )
    if not llm_targets:
        return
    existing = {t.get("target"): t for t in (comment.get("target_stances") or [])}
    for entry in llm_targets:
        target_id = entry.get("target")
        if not target_id:
            continue
        merged = dict(existing.get(target_id) or {})
        merged.update(entry)
        merged["method"] = "llm"
        existing[target_id] = merged
    comment["target_stances"] = list(existing.values())


#: The alert rule itself lives in libs/stance_targets so this path and the
#: assembler's cannot drift apart — see `watchlist_verdict`'s docstring.
_watchlist_verdict = watchlist_verdict


async def _run_comment_summary(
    llm: LLMClient,
    redis,
    stage1_result: dict,
    post_summary: str,
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
        post_summary[:500]
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

    messages = build_comment_summary_messages(post_summary, sb, eb, reps, target_lang)
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
        ca = stage1_result.get("comment_analysis") or {}
        comments = ca.get("comments") or []
        if not comments:
            return out

        # What the LLM judges each comment AGAINST. Stage 1's summary first —
        # a comment's stance is toward what the post SAID, and the summary is
        # the readable form of that. Falls back to the post's own content
        # (caption → OCR → image description) when Stage 1 wrote no summary.
        #
        # Deliberately NOT Lane A's summary, which is being generated right now:
        # waiting for it would serialise the two lanes.
        post_summary = (
            (stage1_result.get("post_summary") or "").strip()
            or post_context
            or "(no summary provided)"
        )
        watchlist = _targets()

        try:
            # Every comment already carries Stage 1's verdict; make it a voter.
            for c in comments:
                _seed_heuristic_vote(c)

            # ---- cheap voters: two small heads over the whole thread --------
            voters_used: list[str] = ["heuristic"]
            if _CLASSIFIERS_ENABLED:
                xlmr_n, distil_n = await asyncio.gather(
                    _run_hf_classifier(comments, "xlmr", config.stage2_classifier_1),
                    _run_hf_classifier(comments, "distilbert", config.stage2_classifier_2),
                )
                if xlmr_n:
                    voters_used.append("xlmr")
                if distil_n:
                    voters_used.append("distilbert")
                log.info("stage2_cheap_voters", xlmr=xlmr_n, distilbert=distil_n)

            # ---- near-duplicate grouping: label one, propagate to the rest --
            # Grouping is EXACT match after normalisation, so a duplicate's
            # prompt would be character-for-character identical to its
            # representative's and the model's answer is the same by
            # construction. It is a cache, not an approximation — but it is
            # still switchable, because "every comment got its own call" is
            # sometimes the claim being made.
            if _DEDUP_PROPAGATE:
                groups, dedup_stats = comment_groups.group_indices(
                    [(c.get("text_norm") or c.get("text") or "") for c in comments]
                )
            else:
                groups, dedup_stats = {}, {"duplicates": 0, "duplicate_share": 0.0}
            member_of = comment_groups.representative_of(groups)

            # ---- who gets an LLM call? --------------------------------------
            # COMMENT_LLM_MODE:
            #   "all"      — every comment with text to read, which is what the
            #                three-labeller comparison in the UI needs: LLM,
            #                XLM-R and DistilBERT side by side on the SAME
            #                comment. The default.
            #   "escalate" — only where the cheap voters disagree, have nothing
            #                to say, or a watchlist entity is at stake. Cheaper
            #                (measured ~20% of comments on the corpus post), at
            #                the cost of an empty LLM row on the other 80%.
            escalation_reasons: dict[str, int] = {}
            selected: list[dict] = []
            for idx, c in enumerate(comments):
                if idx in member_of:
                    # A near-duplicate: its representative pays for both.
                    escalation_reasons["near_duplicate"] = (
                        escalation_reasons.get("near_duplicate", 0) + 1
                    )
                    c["escalation_reason"] = "near_duplicate"
                    continue

                # Emoji reactions and bare links carry no text for an LLM to
                # read in either mode — sending them is the cheapest possible
                # way to waste tokens, and they keep their emoji-heuristic
                # label. FILTER_EMOJI_ONLY=false opts into sending them anyway.
                textless = _FILTER_EMOJI_ONLY and c.get("kind") in ("emoji", "filtered", "link")

                if _COMMENT_LLM_MODE == "all":
                    escalate, reason = (False, "no_text") if textless else (True, "llm_all")
                else:
                    escalate, reason = should_escalate(
                        c.get("parallel_labels") or {},
                        kind=c.get("kind") if _FILTER_EMOJI_ONLY else None,
                        mentions_watchlist=bool(c.get("target_stances")),
                    )
                escalation_reasons[reason] = escalation_reasons.get(reason, 0) + 1
                c["escalation_reason"] = reason
                if escalate:
                    selected.append(c)

            # COMMENT_STANCE_MAX_PER_POST caps the premium pass at the top-N
            # most-engaged comments so one 2,857-comment thread cannot stall the
            # worker for minutes. Applied HERE, not inside the labeller, so the
            # comments it drops are recorded as dropped: with the cap set and
            # COMMENT_LLM_MODE="all", "every comment gets an LLM verdict" is
            # true only up to N, and the UI has to be able to say so.
            capped_out = 0
            if 0 < _STANCE_MAX_PER_POST < len(selected):
                selected.sort(key=lambda c: int(c.get("likes") or 0), reverse=True)
                selected, dropped = selected[:_STANCE_MAX_PER_POST], selected[_STANCE_MAX_PER_POST:]
                capped_out = len(dropped)
                for c in dropped:
                    c["escalation_reason"] = "capped_by_max_per_post"
                escalation_reasons["capped_by_max_per_post"] = capped_out
                log.warning(
                    "stage2_comment_llm_capped",
                    cap=_STANCE_MAX_PER_POST,
                    dropped=capped_out,
                    hint="set COMMENT_STANCE_MAX_PER_POST=0 to label every comment",
                )

            n_labeled = 0
            if _STANCE_ENABLED and selected:
                # With the cap lifted a large thread is ~115 batches — minutes on
                # one post. A frame per batch keeps the Trace tab from looking hung.
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

                n_labeled = await _run_llm_comment_labeling(
                    llm, redis, selected, post_summary, backend_override,
                    _stance_progress,
                )
                voters_used.append(LLM_SOURCE)

            # ---- propagate the representative's verdict to its duplicates ---
            for rep_idx, members in groups.items():
                rep = comments[rep_idx]
                rep_llm = (rep.get("parallel_labels") or {}).get(LLM_SOURCE)
                if not rep_llm:
                    continue
                for m in members:
                    dup = comments[m]
                    dup.setdefault("parallel_labels", {})[LLM_SOURCE] = dict(rep_llm)
                    dup["label_source"] = "propagated"
                    dup["propagated_from"] = rep.get("id") or ""

            # ---- fold the LLM's entity stances into the comment's own field -
            for c in comments:
                _merge_llm_target_stances(c)

            # ---- one merge, one set of aggregates ---------------------------
            summary = _merge_ensemble(
                ca, watchlist,
                escalation_reasons=escalation_reasons,
                dedup_stats=dedup_stats,
                voters_used=voters_used,
            )

            alert, reason = watchlist_verdict(
                watchlist, partial_result.get("caption") or "", comments
            )
            out["watchlist_alert"] = alert
            out["watchlist_alert_reason"] = reason

            log.info(
                "stage2_comment_ensemble",
                labeled=n_labeled,
                total=len(comments),
                escalated=summary.get("escalated"),
                escalated_share=summary.get("escalated_share"),
                unanimous_share=summary.get("unanimous_share"),
                abstained=summary.get("abstained"),
                deduplicated=summary.get("deduplicated"),
                reasons=escalation_reasons,
                watchlist_alert=alert,
                reaction_only=ca.get("reaction_only"),
            )
            out["_llm_backend"] = backend_override or get_settings().llm_backend
        except Exception as exc:
            log.error("stage2_comment_ensemble_error", error=str(exc), exc_info=True)

        if _COMMENT_SUMMARY_ENABLED:
            try:
                summary_lang = (task_flags.get("target_lang")
                                or stage1_result.get("language") or "English")
                comment_summary = await _run_comment_summary(
                    llm, redis, stage1_result, post_summary, summary_lang, backend_override
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
    stage2_result.update(comment_out)

    elapsed_ms = int((time.monotonic() - t0) * 1000)
    stage2_result["processing"] = {
        "stage2_ms": elapsed_ms,
        "llm_backend": llm_backend,
        "llm_model": llm_model,
        # Per-role model resolution, so "which model wrote this summary?" is
        # answerable from the result rather than from the environment (§6.5).
        "role_models": role_models,
        # Every post now reaches Stage 2 for its COMMENTS; only some get the
        # post-level tasks. `llm_used` downstream means the latter, so it has to
        # be carried rather than inferred from "a stage2_result exists" — which
        # is now true of every post and would report 100% LLM use.
        "post_level_routed": bool(task_flags.get("post_level_routed")),
        "comment_llm_used": bool(
            (stage1_result.get("comment_analysis") or {}).get("ensemble", {}).get("llm_labelled")
        ),
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


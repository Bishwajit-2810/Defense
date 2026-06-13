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

# Ensure libs/ is importable when running as a standalone container.
# The Dockerfile sets PYTHONPATH=/app, so this is only a local-dev fallback.
_LIBS_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "libs")
if _LIBS_PATH not in sys.path:
    sys.path.insert(0, os.path.abspath(_LIBS_PATH))

from libs.llm import LLMClient  # noqa: E402

from .cache import get_cached, set_cached  # noqa: E402
from .prompts import (  # noqa: E402
    build_comment_stance_messages,
    build_comment_summary_messages,
    build_insight_messages,
    build_post_type_messages,
    build_summary_messages,
)

# Context-aware comment stance (post + comments → per-comment stance via LLM).
# Comment stance runs on the lighter llm_b model slot (e.g. gemma3:4b) — set
# LLM_B_LOCAL_MODEL to control it. Summaries/post-type stay on llm_a (qwen2.5:7b).
_STANCE_ENABLED = os.environ.get("COMMENT_STANCE", "true").lower() == "true"
_STANCE_ROLE = os.environ.get("COMMENT_STANCE_ROLE", "llm_b")
_STANCE_BATCH = int(os.environ.get("COMMENT_STANCE_BATCH", "40"))
_STANCE_MAX_TEXT = 140
# Cap how many comments per post get the (slow) context-aware LLM stance: the
# top-N by likes. The rest keep their instant Stage-1 heuristic label, so EVERY
# comment is still analysed (full coverage) — the cap only bounds the premium LLM
# pass so a post with thousands of comments doesn't stall the pipeline.
#   COMMENT_STANCE_MAX_PER_POST=0  → LLM-label every comment (full LLM coverage;
#   only practical on Groq / a GPU — on local CPU Ollama this takes ages).
_STANCE_MAX_PER_POST = int(os.environ.get("COMMENT_STANCE_MAX_PER_POST", "40"))
# Natural-language summary of the comment reactions (one short LLM call per post
# that has comments). Set COMMENT_SUMMARY=false to disable.
_COMMENT_SUMMARY_ENABLED = os.environ.get("COMMENT_SUMMARY", "true").lower() == "true"
_STANCE_LABELS = {"positive", "negative", "neutral"}
_STANCE_SCORE = {"positive": 0.6, "negative": -0.6, "neutral": 0.0}
# Emotion taxonomy must match libs/schemas/output_schema.json and the Stage-1
# heuristic (comment_analyzer._EMOTIONS).
_EMOTION_LABELS = {"anger", "sadness", "joy", "fear", "disgust", "surprise", "neutral"}
_EMOTION_KEYS = ("anger", "sadness", "joy", "fear", "disgust", "surprise", "neutral")

from libs.common.logging import setup_logging  # noqa: E402

setup_logging("stage2")
logger = structlog.get_logger(__name__)

REDIS_URL: str = os.environ.get("REDIS_URL", "redis://localhost:6379/0")

STAGE2_QUEUE: str = "llm:stage2:queue"
ASSEMBLER_QUEUE: str = "assembler:queue"
CONSUMER_GROUP: str = "stage2-llm-workers"
CONSUMER_NAME: str = os.environ.get("HOSTNAME", "stage2-llm-0")

BLOCK_MS: int = 5_000
COUNT: int = 10  # LLM calls are slow — keep batches small


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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


_IMAGE_FETCH_TIMEOUT = 10.0
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


async def _track_usage(redis, response: dict | None = None, cache_hit: bool = False) -> None:
    """Increment the Redis usage counters GET /v1/usage reads.

    Counters: usage:llm_calls (fresh calls), usage:cache_hits,
    usage:tokens:total (sum of total_tokens across fresh calls).
    """
    try:
        if cache_hit:
            await redis.incr("usage:cache_hits")
            return
        await redis.incr("usage:llm_calls")
        usage = (response or {}).get("usage") or {}
        total = int(usage.get("total_tokens") or 0)
        if total:
            await redis.incrby("usage:tokens:total", total)
    except Exception as exc:
        logger.warning("usage_tracking_failed", error=str(exc))


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
    backend = backend_override or os.environ.get("LLM_BACKEND", "local")

    # Resolve model id for cache key (best-effort; use role label as fallback)
    model_label = role

    cached = await get_cached(redis, backend, model_label, task, content_hash)
    if cached is not None:
        await _track_usage(redis, cache_hit=True)
        return {**cached, "_cache_hit": True}

    target_lang = task_flags.get("target_lang") or partial_result.get("language") or "the post's language"
    # Image-grounded summary: the VLM gets the actual image bytes (base64
    # data URLs — local servers like Ollama can't fetch remote URLs) alongside
    # caption/OCR. Expired CDN links or a VLM failure degrade to a text-only
    # summary instead of failing the task.
    image_urls: list[str] | None = None
    if role == "vlm" and partial_result.get("photo_urls"):
        image_urls = await _fetch_images_as_data_urls(partial_result["photo_urls"]) or None
    grounded_on_image = bool(image_urls)
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
            max_tokens=512,
            temperature=0.2,
        )

    # Only actually drive the VLM when an image was genuinely fetched. A "vlm"
    # role with no fetchable image — e.g. photo_urls are relative storage paths
    # ("posts/.../x.jpg") rather than absolute URLs, or the CDN link expired — is
    # worse than useless: the local VLM is weaker than llm_a at text-only
    # summarisation and frequently returns empty. So when there's no image to
    # ground on, summarise on llm_a from caption + OCR directly instead of burning
    # a doomed VLM call that strands the post with a blank summary.
    effective_role = role if grounded_on_image else "llm_a"
    chat_messages = messages if grounded_on_image else text_only_messages

    try:
        response = await _chat(effective_role, chat_messages)
    except Exception as exc:
        if effective_role != "vlm":
            raise
        logger.warning("vlm_summary_failed_falling_back_to_text", error=str(exc))
        grounded_on_image = False
        response = await _chat("llm_a", text_only_messages)

    # Local VLMs (qwen3-vl) frequently return EMPTY content without raising — that
    # left image posts with a blank summary. Whenever the VLM was actually used
    # and produced nothing, redo the summary text-only on llm_a so the caption +
    # OCR still yield a summary.
    if effective_role == "vlm" and not (response.get("content") or "").strip():
        logger.warning("vlm_summary_empty_falling_back_to_text", post_id=partial_result.get("post_id"))
        grounded_on_image = False
        response = await _chat("llm_a", text_only_messages)

    await _track_usage(redis, response)

    summary_text = (response.get("content") or "").strip()

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
        "_llm_model": response.get("model", model_label),
        "_llm_backend": response.get("backend", backend),
    }

    # Never cache an empty summary — otherwise the blank result is served back on
    # every retry for the same content and the post can never recover.
    if summary_text:
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
    backend = backend_override or os.environ.get("LLM_BACKEND", "local")
    model_label = role

    cached = await get_cached(redis, backend, model_label, task, content_hash)
    if cached is not None:
        await _track_usage(redis, cache_hit=True)
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
        max_tokens=128,
        temperature=0.0,
    )

    await _track_usage(redis, response)

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
    backend = backend_override or os.environ.get("LLM_BACKEND", "local")
    model_label = role

    cached = await get_cached(redis, backend, model_label, task, content_hash)
    if cached is not None:
        await _track_usage(redis, cache_hit=True)
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
        max_tokens=512,
        temperature=0.1,
    )

    await _track_usage(redis, response)

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

    await set_cached(redis, backend, model_label, task, content_hash, result)
    return result


def _normalize_stance(parsed: Any, n: int) -> list:
    """Map an LLM response to a list[n] of {"s","e"} dicts (or None per slot).

    Each slot carries the stance ("s") and emotion ("e") the LLM returned for
    that comment number. Either field may be None when the model omits it or
    returns an out-of-taxonomy value, so the caller keeps the Stage-1 fallback.
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
        out[idx - 1] = {
            "s": s if s in _STANCE_LABELS else None,
            "e": e if e in _EMOTION_LABELS else None,
        }
    return out


async def _run_comment_stance(
    llm: LLMClient,
    redis,
    stage1_result: dict,
    post_context: str,
    backend_override: str | None,
) -> int:
    """Re-label every embedded comment with its STANCE TOWARD THE POST via the LLM.

    Batches comments, sends each batch (with the post as context) to the LLM,
    and overwrites each comment's ``sentiment`` (+ score, method="llm"). Mutates
    ``stage1_result["comment_analysis"]`` in place — including a recomputed
    sentiment_breakdown / method_breakdown — so the assembler persists the
    context-aware labels to Postgres + ClickHouse. Returns #comments re-labelled.
    """
    ca = stage1_result.get("comment_analysis") or {}
    comments = ca.get("comments") or []
    if not comments:
        return 0

    backend = backend_override or os.environ.get("LLM_BACKEND", "local")
    labeled = 0

    # Only the most-engaged comments get the premium context-aware LLM stance;
    # the rest keep their instant Stage-1 label (coverage stays 100%). These are
    # the same dict objects as in ``comments``, so mutating them updates the list.
    if 0 < _STANCE_MAX_PER_POST < len(comments):
        targets = sorted(comments, key=lambda c: int(c.get("likes") or 0), reverse=True)[:_STANCE_MAX_PER_POST]
    else:
        targets = comments

    for start in range(0, len(targets), _STANCE_BATCH):
        batch = targets[start : start + _STANCE_BATCH]
        raw_key = post_context[:1500] + "||" + "|".join(
            (c.get("text") or "")[:_STANCE_MAX_TEXT] for c in batch
        )
        content_hash = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()

        cached = await get_cached(redis, backend, _STANCE_ROLE, "comment_stance", content_hash)
        if cached is not None and isinstance(cached.get("labels"), list) and len(cached["labels"]) == len(batch):
            results = cached["labels"]
            await _track_usage(redis, cache_hit=True)
        else:
            messages = build_comment_stance_messages(post_context, batch, _STANCE_MAX_TEXT)
            try:
                resp = await llm.chat(
                    role=_STANCE_ROLE,
                    messages=messages,
                    backend_override=backend_override,
                    response_format={"type": "json_object"},
                    # ~56 tokens/comment covers {"i":N,"s":"...","e":"..."}.
                    max_tokens=min(4096, 56 * len(batch) + 64),
                    temperature=0.0,
                )
                await _track_usage(redis, resp)
                results = _normalize_stance(_safe_json_parse(resp["content"], {}), len(batch))
                await set_cached(redis, backend, _STANCE_ROLE, "comment_stance", content_hash, {"labels": results})
            except Exception as exc:
                logger.warning("comment_stance_batch_failed", start=start, error=str(exc))
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
                labeled += 1
            if emotion in _EMOTION_LABELS:
                # Context-aware emotion overwrites the Stage-1 heuristic guess.
                c["emotion"] = emotion
            # else: keep the Stage-1 standalone values as a fallback

    # Recompute aggregates from the (mutated) per-comment list.
    sb = {"positive": 0, "negative": 0, "neutral": 0}
    eb = {k: 0 for k in _EMOTION_KEYS}
    mb: dict = {}
    for c in comments:
        s = c.get("sentiment") or "neutral"
        if s not in sb:
            s = "neutral"
        sb[s] += 1
        e = c.get("emotion") or "neutral"
        if e not in eb:
            e = "neutral"
        eb[e] += 1
        m = c.get("method") or "fast"
        mb[m] = mb.get(m, 0) + 1
    ca["sentiment_breakdown"] = sb
    ca["emotion_breakdown"] = eb
    ca["method_breakdown"] = mb
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

    backend = backend_override or os.environ.get("LLM_BACKEND", "local")
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

    cached = await get_cached(redis, backend, _STANCE_ROLE, "comment_summary", content_hash)
    if cached is not None:
        await _track_usage(redis, cache_hit=True)
        return cached.get("summary")

    messages = build_comment_summary_messages(post_context, sb, eb, reps, target_lang)
    resp = await llm.chat(
        role=_STANCE_ROLE,
        messages=messages,
        backend_override=backend_override,
        max_tokens=256,
        temperature=0.3,
    )
    await _track_usage(redis, resp)

    summary = (resp.get("content") or "").strip()
    if summary:
        await set_cached(
            redis, backend, _STANCE_ROLE, "comment_summary", content_hash, {"summary": summary}
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

    # Runtime backend override set via PUT /v1/config/llm (dashboard LLM toggle).
    backend_override: str | None = None
    try:
        raw_override = await redis.get("config:llm_backend")
        if raw_override:
            decoded = raw_override.decode() if isinstance(raw_override, bytes) else raw_override
            if decoded in ("local", "groq"):
                backend_override = decoded
    except Exception as exc:
        log.warning("llm_backend_override_read_failed", error=str(exc))

    log.info("stage2_processing", task_flags=task_flags, backend_override=backend_override)

    # Build a working view the per-task prompts can read caption/OCR from.
    image_analysis = stage1_result.get("image_analysis") or {}
    partial_result: dict = {
        **stage1_result,
        "caption": normalized_post.get("caption"),
        "ocr_text": image_analysis.get("ocr_text"),
        "image_description": image_analysis.get("description"),
        "photo_urls": normalized_post.get("photo_urls") or [],
    }

    # Choose role: VLM for image posts (photo_urls present), LLM-A otherwise.
    # Set VLM_SUMMARY=false to skip image grounding entirely — useful when the
    # local VLM is weak/unavailable (e.g. returns empty), so every post still gets
    # a text+OCR-grounded summary without burning a wasted VLM call first.
    has_photos = bool(normalized_post.get("photo_urls"))
    vlm_enabled = os.environ.get("VLM_SUMMARY", "true").lower() == "true"
    role = "vlm" if (has_photos and vlm_enabled) else "llm_a"

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
    # All run on the llm_a / vlm slot, sequentially within the lane.
    async def _post_level_lane() -> dict:
        out: dict = {}
        backend = model = None

        if task_flags.get("want_summary", False):
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
                backend = backend or summary_data.get("_llm_backend")
                model = model or summary_data.get("_llm_model")
            except Exception as exc:
                log.error("stage2_summary_error", error=str(exc))

        if task_flags.get("want_post_type", False):
            try:
                pt_data = await _run_post_type(llm, redis, partial_result, task_flags, role, backend_override)
                out["post_type"] = pt_data.get("post_type")
                out["post_type_confidence"] = pt_data.get("post_type_confidence")
                backend = backend or pt_data.get("_llm_backend")
                model = model or pt_data.get("_llm_model")
            except Exception as exc:
                log.error("stage2_post_type_error", error=str(exc))

        if task_flags.get("want_insight", False):
            try:
                insight_data = await _run_insight(llm, redis, partial_result, task_flags, role, backend_override)
                if insight_data.get("refined_topics"):
                    out["topics"] = insight_data["refined_topics"]
                out["intents"] = insight_data.get("intents") or []
                out["insight"] = insight_data.get("insight") or ""
                backend = backend or insight_data.get("_llm_backend")
                model = model or insight_data.get("_llm_model")
            except Exception as exc:
                log.error("stage2_insight_error", error=str(exc))

        out["_llm_backend"] = backend
        out["_llm_model"] = model
        return out

    # ---- Lane B: comment analysis (context-aware stance → comment summary) ----
    # Runs on the llm_b slot. Re-labels every embedded comment by its stance
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
            n_labeled = await _run_comment_stance(
                llm, redis, stage1_result, post_context, backend_override
            )
            log.info("stage2_comment_stance", labeled=n_labeled,
                     total=len(ca.get("comments") or []))
            out["_llm_backend"] = backend_override or os.environ.get("LLM_BACKEND", "local")
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
    }

    payload["stage2_result"] = stage2_result
    payload.pop("task_flags", None)
    await redis.xadd(ASSEMBLER_QUEUE, {"data": json.dumps(payload, ensure_ascii=False)})
    log.info("stage2_done", elapsed_ms=elapsed_ms, destination="assembler:queue")


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
                except Exception as exc:
                    logger.error(
                        "stage2_message_error",
                        message_id=message_id,
                        error=str(exc),
                    )
                finally:
                    try:
                        await redis.xack(STAGE2_QUEUE, CONSUMER_GROUP, message_id)
                    except Exception as ack_exc:
                        logger.warning(
                            "stage2_ack_failed",
                            message_id=message_id,
                            error=str(ack_exc),
                        )

    logger.info("stage2_llm_stopped")
    await redis.aclose()

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
    build_insight_messages,
    build_post_type_messages,
    build_summary_messages,
)

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

    try:
        response = await llm.chat(
            role=role,
            messages=messages,
            backend_override=backend_override,
            max_tokens=512,
            temperature=0.2,
        )
    except Exception as exc:
        if not grounded_on_image:
            raise
        logger.warning("vlm_summary_failed_falling_back_to_text", error=str(exc))
        grounded_on_image = False
        messages = build_summary_messages(
            caption=partial_result.get("caption") or "",
            ocr_text=partial_result.get("ocr_text") or "",
            image_description=partial_result.get("image_description") or "",
            language=partial_result.get("language") or "unknown",
            target_lang=target_lang,
        )
        response = await llm.chat(
            role="llm_a",
            messages=messages,
            backend_override=backend_override,
            max_tokens=512,
            temperature=0.2,
        )

    await _track_usage(redis, response)

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
        "post_summary": response["content"].strip(),
        "post_summary_lang": target_lang,
        # Golden rule 10: "vlm" only when the image actually grounded it.
        "post_summary_source": "vlm" if grounded_on_image else "llm",
        "post_summary_grounding": grounding_parts,
        "_llm_model": response.get("model", model_label),
        "_llm_backend": response.get("backend", backend),
    }

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

    # Choose role: VLM for image posts (photo_urls present), LLM-A otherwise
    has_photos = bool(normalized_post.get("photo_urls"))
    role = "vlm" if has_photos else "llm_a"

    stage2_result: dict = {}
    llm_backend: str | None = None
    llm_model: str | None = None

    # ---- Summary ----
    if task_flags.get("want_summary", False):
        try:
            summary_data = await _run_summary(llm, redis, partial_result, task_flags, role, backend_override)
            stage2_result["post_summary"] = summary_data.get("post_summary")
            stage2_result["post_summary_lang"] = summary_data.get("post_summary_lang")
            # "vlm" when the image grounded the summary, "llm" otherwise
            # (including VLM→text fallback) — set inside _run_summary.
            stage2_result["post_summary_source"] = summary_data.get(
                "post_summary_source", "vlm" if has_photos else "llm"
            )
            grounding = summary_data.get("post_summary_grounding")
            if isinstance(grounding, (list, tuple)):
                grounding = "+".join(grounding) if grounding else None
            stage2_result["post_summary_grounding"] = grounding
            llm_backend = llm_backend or summary_data.get("_llm_backend")
            llm_model = llm_model or summary_data.get("_llm_model")
        except Exception as exc:
            log.error("stage2_summary_error", error=str(exc))

    # ---- Post type ----
    if task_flags.get("want_post_type", False):
        try:
            pt_data = await _run_post_type(llm, redis, partial_result, task_flags, role, backend_override)
            stage2_result["post_type"] = pt_data.get("post_type")
            stage2_result["post_type_confidence"] = pt_data.get("post_type_confidence")
            llm_backend = llm_backend or pt_data.get("_llm_backend")
            llm_model = llm_model or pt_data.get("_llm_model")
        except Exception as exc:
            log.error("stage2_post_type_error", error=str(exc))

    # ---- Insight ----
    if task_flags.get("want_insight", False):
        try:
            insight_data = await _run_insight(llm, redis, partial_result, task_flags, role, backend_override)
            if insight_data.get("refined_topics"):
                stage2_result["topics"] = insight_data["refined_topics"]
            stage2_result["intents"] = insight_data.get("intents") or []
            stage2_result["insight"] = insight_data.get("insight") or ""
            llm_backend = llm_backend or insight_data.get("_llm_backend")
            llm_model = llm_model or insight_data.get("_llm_model")
        except Exception as exc:
            log.error("stage2_insight_error", error=str(exc))

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
            logger.error("stage2_read_error", error=str(exc))
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

"""
Redis-backed cache for Stage-2 LLM responses.

Keys follow the pattern:
    llm_cache:{backend}:{model}:{task}:{content_hash}

Default TTL: 7 days (604 800 seconds).
"""

from __future__ import annotations

import json
import re

import structlog

logger = structlog.get_logger(__name__)

_DEFAULT_TTL: int = 86_400 * 7  # 7 days in seconds

# Sanitise model IDs that contain '/' (e.g. "Qwen/Qwen2.5-7B-Instruct")
# so Redis key segments don't get ambiguous.
_SAFE = re.compile(r"[^A-Za-z0-9._-]")


def _safe(value: str) -> str:
    return _SAFE.sub("_", value)


def _build_key(backend: str, model: str, task: str, content_hash: str) -> str:
    return f"llm_cache:{_safe(backend)}:{_safe(model)}:{_safe(task)}:{content_hash}"


async def get_cached(
    redis,
    backend: str,
    model: str,
    task: str,
    content_hash: str,
) -> dict | None:
    """Check Redis for a cached LLM response.

    Parameters
    ----------
    redis:
        An active ``redis.asyncio.Redis`` (or compatible) instance.
    backend:
        LLM backend identifier, e.g. ``"groq"`` or ``"local"``.
    model:
        Model ID string.
    task:
        Task name: ``"summary"``, ``"post_type"``, or ``"insight"``.
    content_hash:
        SHA-256 hex digest of the content that was sent to the LLM.

    Returns
    -------
    Cached response dict, or ``None`` on cache miss.
    """
    key = _build_key(backend, model, task, content_hash)
    try:
        raw = await redis.get(key)
        if raw is None:
            return None
        result: dict = json.loads(raw)
        logger.debug("llm_cache_hit", key=key, task=task)
        return result
    except Exception as exc:
        logger.warning("llm_cache_get_error", key=key, error=str(exc))
        return None


async def set_cached(
    redis,
    backend: str,
    model: str,
    task: str,
    content_hash: str,
    response: dict,
    ttl: int = _DEFAULT_TTL,
) -> None:
    """Store an LLM response in Redis.

    Parameters
    ----------
    redis:
        An active ``redis.asyncio.Redis`` (or compatible) instance.
    backend:
        LLM backend identifier.
    model:
        Model ID string.
    task:
        Task name.
    content_hash:
        SHA-256 hex digest of the content that was sent to the LLM.
    response:
        The parsed response dict to cache.
    ttl:
        Expiry in seconds (default 7 days).
    """
    key = _build_key(backend, model, task, content_hash)
    try:
        await redis.set(key, json.dumps(response), ex=ttl)
        logger.debug("llm_cache_set", key=key, task=task, ttl=ttl)
    except Exception as exc:
        logger.warning("llm_cache_set_error", key=key, error=str(exc))

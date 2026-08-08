"""
Redis-backed cache for Stage-2 LLM responses.

Keys follow the pattern:
    llm_cache:{backend}:{model}:{task}:{content_hash}

**The `model` slot must be the resolved model id, not a role label.** It used to
be called with the role ("stage2", "vlm") — the concrete model id
(STAGE2_LOCAL_MODEL, STAGE2_GROQ_MODEL) was not in the key at all, and entries
live 7 days. Changing the model and re-running the same posts therefore returned
the *previous* model's answers. That was already a correctness bug, and it is a
direct threat to any model comparison: a benchmark across BanglaBERT, XLM-R,
gemma3:4b, qwen2.5:7b and llama-3.3-70b would silently compare each model
against its own cached output.

Splitting summarization onto its own `summary` role turns that latent bug
active, because two different models are then in play for two different tasks
under the same key. Callers resolve the model id via
``LLMClient.default_model(role, backend)`` and pass it here.

Default TTL: 7 days (604 800 seconds). Set LLM_CACHE_DISABLED=1 to bypass the
cache entirely for an evaluation run.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from defense.libs.common.config import get_settings

import json
import re

import structlog

logger = structlog.get_logger(__name__)

_DEFAULT_TTL: int = 86_400 * 7  # 7 days in seconds


def _disabled() -> bool:
    """True when LLM_CACHE_DISABLED is set — the escape hatch for eval runs."""
    return get_settings().llm_cache_disabled

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
    if _disabled():
        return None
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
    if _disabled():
        return
    key = _build_key(backend, model, task, content_hash)
    try:
        await redis.set(key, json.dumps(response), ex=ttl)
        logger.debug("llm_cache_set", key=key, task=task, ttl=ttl)
    except Exception as exc:
        logger.warning("llm_cache_set_error", key=key, error=str(exc))

"""Runtime configuration endpoints — currently the LLM backend toggle.

The active backend is resolved per Stage-2 message as:
    Redis override (``config:llm_backend``)  >  LLM_BACKEND env  >  "local"

PUT here only sets/clears the Redis override — it never mutates the env, so a
worker restart always returns to the deployed default.
"""

from __future__ import annotations

import os
from typing import Optional

import structlog
import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from deps import get_current_user, get_redis

from libs import sentiment_models

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/v1/config", tags=["config"])

LLM_BACKEND_KEY = "config:llm_backend"
_VALID_BACKENDS = ("local", "groq")

# Runtime sentiment-model override (mirrors the Stage-1 worker's key). Absent =>
# auto-route by detected language.
SENTIMENT_MODEL_KEY = "config:sentiment_model"

# Mirrors libs/llm/client.py role→model defaults.
_MODEL_ENVS = {
    "local": {
        "stage1": ("STAGE1_LOCAL_MODEL", "gemma3:4b"),
        "stage2": ("STAGE2_LOCAL_MODEL", "qwen2.5:7b"),
        "llm_a": ("LLM_A_LOCAL_MODEL", "qwen2.5:7b"),
        "llm_b": ("LLM_B_LOCAL_MODEL", "qwen2.5:7b"),
        "vlm": ("VLM_LOCAL_MODEL", "qwen3-vl:4b"),
    },
    "groq": {
        "stage1": ("STAGE1_GROQ_MODEL", "llama-3.1-8b-instant"),
        "stage2": ("STAGE2_GROQ_MODEL", "llama-3.3-70b-versatile"),
        "llm_a": ("LLM_A_GROQ_MODEL", "llama-3.1-8b-instant"),
        "llm_b": ("LLM_B_GROQ_MODEL", "llama-3.3-70b-versatile"),
        "vlm": ("VLM_GROQ_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct"),
    },
}


class LLMConfigUpdate(BaseModel):
    backend: Optional[str] = None  # "local" | "groq" | null (clear override)


def _models() -> dict:
    return {
        be: {role: os.environ.get(env, default) for role, (env, default) in roles.items()}
        for be, roles in _MODEL_ENVS.items()
    }


async def _current(redis: aioredis.Redis) -> dict:
    default_backend = os.environ.get("LLM_BACKEND", "local")
    raw = await redis.get(LLM_BACKEND_KEY)
    override = raw.decode() if isinstance(raw, bytes) else raw
    if override not in _VALID_BACKENDS:
        override = None
    return {
        "backend": override or default_backend,
        "default_backend": default_backend,
        "override": override,
        "models": _models(),
    }


@router.get("/llm", summary="Get the active LLM backend configuration")
async def get_llm_config(
    redis: aioredis.Redis = Depends(get_redis),
    current_user: dict = Depends(get_current_user),
) -> dict:
    return await _current(redis)


@router.put("/llm", summary="Set or clear the LLM backend override")
async def put_llm_config(
    body: LLMConfigUpdate,
    redis: aioredis.Redis = Depends(get_redis),
    current_user: dict = Depends(get_current_user),
) -> dict:
    """Applies to new Stage-2 work only; in-flight messages keep their backend.

    ``{"backend": null}`` clears the override (falls back to the env default).
    Groq requires GROQ_API_KEY in the worker's environment.
    """
    if body.backend is None:
        await redis.delete(LLM_BACKEND_KEY)
        log.info("llm_backend_override_cleared")
    elif body.backend in _VALID_BACKENDS:
        await redis.set(LLM_BACKEND_KEY, body.backend)
        log.info("llm_backend_override_set", backend=body.backend)
    else:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"backend must be one of {list(_VALID_BACKENDS)} or null",
        )
    return await _current(redis)


# ---------------------------------------------------------------------------
# Stage-1 sentiment model (language-routed, runtime-switchable)
# ---------------------------------------------------------------------------


class NLPConfigUpdate(BaseModel):
    # A model key (e.g. "xlmr", "banglabert") to force, or null to clear the
    # override and return to auto-routing by detected language.
    model: Optional[str] = None


async def _nlp_current(redis: aioredis.Redis) -> dict:
    raw = await redis.get(SENTIMENT_MODEL_KEY)
    override = raw.decode() if isinstance(raw, bytes) else raw
    if override not in sentiment_models.valid_keys():
        override = None
    return {
        "override": override,                       # forced key, or None
        "mode": "forced" if override else "auto",   # auto = route by language
        "default_key": sentiment_models.DEFAULT_KEY,
        "route_table": sentiment_models.route_table(),
        "options": sentiment_models.options_status(),
    }


@router.get("/nlp", summary="Get the active Stage-1 sentiment-model configuration")
async def get_nlp_config(
    redis: aioredis.Redis = Depends(get_redis),
    current_user: dict = Depends(get_current_user),
) -> dict:
    return await _nlp_current(redis)


@router.put("/nlp", summary="Force a sentiment model or clear the override (auto-route)")
async def put_nlp_config(
    body: NLPConfigUpdate,
    redis: aioredis.Redis = Depends(get_redis),
    current_user: dict = Depends(get_current_user),
) -> dict:
    """Applies to new Stage-1 work only; in-flight messages keep their model.

    ``{"model": null}`` clears the override (auto-route by language). A forced
    model must be a known key **and** currently available (its checkpoint
    configured) — unavailable slots are rejected so the UI can't pin a model
    that would silently fall back.
    """
    if body.model is None:
        await redis.delete(SENTIMENT_MODEL_KEY)
        log.info("sentiment_model_override_cleared")
    elif body.model not in sentiment_models.valid_keys():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"model must be one of {sentiment_models.valid_keys()} or null",
        )
    elif not sentiment_models.is_available(body.model):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"model {body.model!r} has no checkpoint configured "
                "(set its *_SENTIMENT_MODEL env var first)"
            ),
        )
    else:
        await redis.set(SENTIMENT_MODEL_KEY, body.model)
        log.info("sentiment_model_override_set", model=body.model)
    return await _nlp_current(redis)

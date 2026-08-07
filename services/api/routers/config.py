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

from sqlalchemy.ext.asyncio import AsyncSession

from deps import get_current_user, get_db, get_redis, tenant_is_privacy_locked

from libs import sentiment_models

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/v1/config", tags=["config"])

LLM_BACKEND_KEY = "config:llm_backend"
_VALID_BACKENDS = ("local", "groq")

#: Roles allowed to flip the GLOBAL backend switch. Selecting `groq` routes
#: every tenant's analysis off-box, so it is an operator action (§13.5).
_ADMIN_ROLES = frozenset({"admin", "owner", "operator"})

# Runtime sentiment-model override (mirrors the Stage-1 worker's key). Absent =>
# auto-route by detected language.
SENTIMENT_MODEL_KEY = "config:sentiment_model"

# Role→model resolution is READ FROM libs/llm/client.py, never mirrored.
#
# This was a hand-maintained copy of that module's two tables, and it had already
# drifted: it listed five roles and omitted `summary` — the role §6.5 added
# specifically so summarization gets a stronger model than classification. So
# `GET /v1/config/llm` could not show which model writes the summaries, which is
# the headline of that requirement (PROJECT_ASSESSMENT §13.7a). A copy of another
# module's table is §5.1's pattern; the fix is to stop copying.


class LLMConfigUpdate(BaseModel):
    backend: Optional[str] = None  # "local" | "groq" | null (clear override)


def _models() -> dict:
    """The resolved role→model map per backend, straight from the LLM client.

    `default_model` already applies each role's env override, so this reports
    exactly what a call would use — and gains any new role automatically.
    """
    from libs.llm.client import VALID_ROLES, LLMClient  # noqa: PLC0415

    client = LLMClient()
    return {
        be: {role: client.default_model(role, be) for role in sorted(VALID_ROLES)}
        for be in _VALID_BACKENDS
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
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> dict:
    """Applies to new Stage-2 work only; in-flight messages keep their backend.

    ``{"backend": null}`` clears the override (falls back to the env default).
    Groq requires GROQ_API_KEY in the worker's environment.

    **This is a GLOBAL switch**, not a per-request one: it applies to every
    tenant's pipeline work. It used to be settable by any authenticated caller
    with no policy check at all, which meant a privacy-locked tenant's post
    content followed it straight to Groq (PROJECT_ASSESSMENT §13.5). Two guards
    now apply:

    * switching to ``groq`` requires an **admin** role — a global switch is an
      operator action, not a tenant-user one;
    * a caller whose own tenant is privacy-locked cannot select ``groq`` at all,
      because it would be asking for its own content to be sent off-box.

    Locked tenants are unaffected by whatever the switch ends up saying: the API
    pins them to ``local`` when it resolves each job's backend.
    """
    if body.backend == "groq":
        role = (current_user.get("role") or "user").lower()
        if role not in _ADMIN_ROLES:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    "Switching the global LLM backend to 'groq' requires an admin "
                    "role — it routes every tenant's analysis off-box."
                ),
            )
        if await tenant_is_privacy_locked(db, current_user):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    f"Tenant '{current_user.get('tenant_id') or 'default'}' is "
                    "privacy-locked: 'groq' is not permitted (data must not "
                    "leave the local backend)"
                ),
            )

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

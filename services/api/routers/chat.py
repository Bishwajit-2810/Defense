"""Chatbot endpoint — free-form chat backed by the same LLM the pipeline uses,
honouring the dashboard's runtime backend toggle (local Ollama ↔ Groq Cloud).

    POST /v1/chat          → {"reply", "backend", "model", "usage"}
    POST /v1/chat/stream    → Server-Sent Events (token streaming)

Backend resolution, per request:

    request.backend ("local"|"groq")
        > Redis toggle (config:llm_backend, set by the dashboard / --groq/--ollama)
        > LLM_BACKEND env
        > "local"

Send ``backend: "auto"`` (or omit it) to follow the toggle; send ``"local"`` or
``"groq"`` to force one for this call. A privacy-locked tenant cannot force
``"groq"`` — the same policy the analysis endpoints enforce applies here.
"""

from __future__ import annotations

import json
import os
from typing import AsyncGenerator, Optional

import redis.asyncio as aioredis
import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from deps import (
    check_llm_backend_policy,
    get_current_user,
    get_db,
    get_redis,
    rate_limit,
)

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/v1/chat", tags=["chat"])

LLM_BACKEND_KEY = "config:llm_backend"
_VALID_BACKENDS = ("local", "groq")

# The chatbot speaks through the quality role (llm_b): qwen2.5:7b locally,
# llama-3.3-70b-versatile on Groq. See libs/llm/client.py role→model maps.
_CHAT_ROLE = "llm_b"

_DEFAULT_SYSTEM = (
    "You are a helpful, concise assistant embedded in the Defense social-media "
    "analysis platform. Answer the user's questions directly and factually. If "
    "you don't know something, say so rather than inventing an answer."
)

# Guards so a runaway client can't push an unbounded history at the LLM.
_MAX_MESSAGES = 50
_MAX_CHARS = 24_000
_VALID_MSG_ROLES = ("system", "user", "assistant")


class ChatMessage(BaseModel):
    role: str = Field(..., description='"system", "user", or "assistant"')
    content: str = Field(..., min_length=1)


class ChatRequest(BaseModel):
    message: Optional[str] = Field(
        None, description="Single-turn convenience: one user message"
    )
    messages: Optional[list[ChatMessage]] = Field(
        None, description="Full conversation history (overrides `message`)"
    )
    system: Optional[str] = Field(
        None, description="Override the default system prompt (ignored if `messages` already has a system turn)"
    )
    backend: Optional[str] = Field(
        None, description='"local" | "groq" | "auto"/null (follow the toggle)'
    )
    model: Optional[str] = Field(
        None, description="Specific model id to use (e.g. 'qwen2.5:7b'); null = the backend's default"
    )
    temperature: float = Field(0.3, ge=0.0, le=2.0)
    max_tokens: int = Field(1024, ge=1, le=8192)


class ChatResponse(BaseModel):
    reply: str
    backend: str  # backend that actually served the reply (after any failover)
    model: str
    usage: dict


# --------------------------------------------------------------------------- #
# Backend + message resolution
# --------------------------------------------------------------------------- #


async def _toggle_backend(redis: aioredis.Redis) -> Optional[str]:
    """The dashboard's runtime backend override, or None if unset/invalid."""
    try:
        raw = await redis.get(LLM_BACKEND_KEY)
    except Exception:
        return None
    val = raw.decode() if isinstance(raw, (bytes, bytearray)) else raw
    return val if val in _VALID_BACKENDS else None


def _normalise_backend(requested: Optional[str]) -> Optional[str]:
    """request.backend → a client override value, or None to mean "auto"."""
    if requested is None:
        return None
    r = requested.strip().lower()
    if r in ("", "auto"):
        return None
    if r in _VALID_BACKENDS:
        return r
    raise HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail=f'backend must be one of {list(_VALID_BACKENDS)}, "auto", or null',
    )


def _build_messages(body: ChatRequest) -> list[dict]:
    """Assemble the OpenAI-style message list, injecting a system prompt."""
    history = list(body.messages or [])
    if not history and body.message:
        history = [ChatMessage(role="user", content=body.message)]
    if not history:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="provide either `message` (string) or `messages` (non-empty list)",
        )
    if len(history) > _MAX_MESSAGES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"too many messages (max {_MAX_MESSAGES})",
        )

    msgs: list[dict] = []
    if not any(m.role == "system" for m in history):
        msgs.append({"role": "system", "content": body.system or _DEFAULT_SYSTEM})

    total = 0
    for m in history:
        if m.role not in _VALID_MSG_ROLES:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"invalid message role {m.role!r}; use one of {list(_VALID_MSG_ROLES)}",
            )
        total += len(m.content)
        msgs.append({"role": m.role, "content": m.content})
    if total > _MAX_CHARS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"conversation too long (max {_MAX_CHARS} characters)",
        )
    return msgs


async def _resolve(
    body: ChatRequest,
    redis: aioredis.Redis,
    db: AsyncSession,
    current_user: dict,
) -> tuple[list[dict], Optional[str]]:
    """Validate the request and settle on (messages, backend_override).

    ``backend_override`` is what we hand the client: an explicit "local"/"groq",
    or None to let the client fall back to env default. The fully-resolved
    backend (override → toggle → env) is what the tenant privacy policy is
    checked against, so a privacy-locked tenant can't reach Groq by any path.
    """
    messages = _build_messages(body)
    explicit = _normalise_backend(body.backend)
    backend_override = explicit if explicit is not None else await _toggle_backend(redis)
    effective = backend_override or os.environ.get("LLM_BACKEND", "local")
    await check_llm_backend_policy(db, current_user, {"llm_backend": effective})
    return messages, backend_override


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #


@router.post("", response_model=ChatResponse, summary="Chat with the platform LLM (local or Groq)")
async def chat(
    body: ChatRequest,
    redis: aioredis.Redis = Depends(get_redis),
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
    _rl: None = Depends(rate_limit),
) -> ChatResponse:
    """Ask the model anything. Uses whichever backend the toggle points at unless
    ``backend`` overrides it. Groq failures transparently fall back to local."""
    from libs.llm.client import LLMClient  # noqa: PLC0415 — lazy: only chat needs it

    messages, backend_override = await _resolve(body, redis, db, current_user)
    try:
        result = await LLMClient().chat(
            role=_CHAT_ROLE,
            messages=messages,
            backend_override=backend_override,
            max_tokens=body.max_tokens,
            temperature=body.temperature,
            model=(body.model or "").strip() or None,
        )
    except Exception as exc:
        log.error("chat_failed", error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"LLM backend error: {exc}",
        ) from exc

    return ChatResponse(
        reply=result.get("content", ""),
        backend=result.get("backend", ""),
        model=result.get("model", ""),
        usage=result.get("usage", {}),
    )


@router.get("/models", summary="List models the chat can use, per backend")
async def list_chat_models(
    redis: aioredis.Redis = Depends(get_redis),
    current_user: dict = Depends(get_current_user),
) -> dict:
    """Available models for the model picker.

    For each backend returns the live catalogue (Ollama's pulled models / Groq's
    hosted models) plus the configured default, and reports which backend the
    toggle currently resolves to so the UI can preselect the right list.
    """
    from libs.llm.client import LLMClient  # noqa: PLC0415

    client = LLMClient()
    backends: dict = {}
    for be in _VALID_BACKENDS:
        default = client.default_model(_CHAT_ROLE, be)
        models = await client.list_models(be)
        # Always surface the default even if the live list came back empty.
        if default and default not in models:
            models = [default, *models]
        backends[be] = {"models": models, "default": default}

    active = await _toggle_backend(redis) or os.environ.get("LLM_BACKEND", "local")
    return {"active_backend": active, "backends": backends}


async def _sse(
    messages: list[dict], backend_override: Optional[str], body: ChatRequest
) -> AsyncGenerator[str, None]:
    """Bridge LLMClient.chat_stream() events to SSE frames."""
    from libs.llm.client import LLMClient  # noqa: PLC0415

    yield "event: open\ndata: {}\n\n"
    try:
        async for evt in LLMClient().chat_stream(
            role=_CHAT_ROLE,
            messages=messages,
            backend_override=backend_override,
            max_tokens=body.max_tokens,
            temperature=body.temperature,
            model=(body.model or "").strip() or None,
        ):
            etype = evt.pop("type", "delta")
            yield f"event: {etype}\ndata: {json.dumps(evt, ensure_ascii=False)}\n\n"
    except Exception as exc:  # pragma: no cover — defensive
        log.error("chat_stream_failed", error=str(exc))
        yield f"event: error\ndata: {json.dumps({'error': str(exc)})}\n\n"


@router.post(
    "/stream",
    summary="Chat with token streaming (Server-Sent Events)",
    response_class=StreamingResponse,
)
async def chat_stream(
    body: ChatRequest,
    redis: aioredis.Redis = Depends(get_redis),
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
    _rl: None = Depends(rate_limit),
) -> StreamingResponse:
    """Same as POST /v1/chat, but streams the answer as it's generated.

    Event sequence: ``open`` → ``meta`` (backend/model) → many ``delta``
    (``{"content": "..."}``) → ``done`` (usage), or ``error``.
    """
    messages, backend_override = await _resolve(body, redis, db, current_user)
    return StreamingResponse(
        _sse(messages, backend_override, body),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

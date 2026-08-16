"""Chatbot endpoint — free-form chat backed by the same LLM the pipeline uses,
honouring the dashboard's runtime backend toggle (local Ollama ↔ Groq Cloud).

    POST /v1/chat          → {"reply", "backend", "model", "usage"}
    POST /v1/chat/stream    → Server-Sent Events (token streaming)
    POST /v1/chat/agent     → route the message to an MCP-backed agent, or
                              back to plain chat when no agent fits

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
from typing import Any, AsyncGenerator, Optional
from defense.libs.common.config import get_settings

import httpx
import redis.asyncio as aioredis
import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from defense.services.api.deps import (
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
    effective = backend_override or get_settings().llm_backend
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
    from defense.libs.llm.client import LLMClient  # noqa: PLC0415 — lazy: only chat needs it

    messages, backend_override = await _resolve(body, redis, db, current_user)
    try:
        result = await LLMClient().chat(
            role=_CHAT_ROLE,
            messages=messages,
            backend_override=backend_override,
            max_tokens=body.max_tokens,
            temperature=body.temperature,
            model=(body.model or "").strip() or None,
            usage_redis=redis,
            usage_task="chat",
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
    from defense.libs.llm.client import LLMClient  # noqa: PLC0415

    client = LLMClient()
    backends: dict = {}
    for be in _VALID_BACKENDS:
        default = client.default_model(_CHAT_ROLE, be)
        models = await client.list_models(be)
        # Always surface the default even if the live list came back empty.
        if default and default not in models:
            models = [default, *models]
        backends[be] = {"models": models, "default": default}

    active = await _toggle_backend(redis) or get_settings().llm_backend
    return {"active_backend": active, "backends": backends}


async def _sse(
    messages: list[dict], backend_override: Optional[str], body: ChatRequest
) -> AsyncGenerator[str, None]:
    """Bridge LLMClient.chat_stream() events to SSE frames."""
    from defense.libs.llm.client import LLMClient  # noqa: PLC0415

    yield "event: open\ndata: {}\n\n"
    try:
        async for evt in LLMClient().chat_stream(
            role=_CHAT_ROLE,
            messages=messages,
            backend_override=backend_override,
            max_tokens=body.max_tokens,
            temperature=body.temperature,
            model=(body.model or "").strip() or None,
            usage_task="chat_stream",
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


# --------------------------------------------------------------------------- #
# Agent routing
# --------------------------------------------------------------------------- #
# Plain chat cannot see the corpus — it has no tools. The agents service can:
# it runs a real MCP tool loop over the analytics / retrieval / ingest servers.
# This endpoint decides, per message, which of the two should answer, so the
# operator gets one chat box instead of having to know in advance whether their
# question is a "data question".
#
# The decision is a small LLM classification over the agent registry's own
# descriptions, so adding an agent to the registry is enough to make it
# reachable from chat — there is no second list here to keep in sync.

# The fast role (llm_a): the routing call must be cheap enough that it is not a
# reason to skip routing. It classifies; it never answers.
_ROUTER_ROLE = "llm_a"

# Messages this short and this conversational are not corpus questions, and a
# round-trip to classify "thanks" is pure latency.
_SMALL_TALK = {
    "hi", "hello", "hey", "yo", "thanks", "thank you", "ty", "ok", "okay",
    "cool", "nice", "got it", "bye", "goodbye", "sup", "hola",
}

# Fallback only — consulted when the classifier errors or names an agent that
# does not exist. Deliberately NOT a pre-pass: "what does toxicity mean?" is a
# definitional question that a keyword match would hand to the toxicity agent
# and a language model correctly sends to plain chat.
_KEYWORD_FALLBACK: tuple[tuple[tuple[str, ...], str], ...] = (
    (("compare", "versus", " vs ", "difference between"), "comparator"),
    (("toxic", "hate speech", "harass", "abusive"), "toxicity"),
    (("stance", "support", "oppose", "in favour", "in favor"), "stance"),
    (("narrative", "theme", "topic", "cluster"), "narrative"),
    (("coverage", "how many comments", "under-analysed", "under-analyzed"), "coverage"),
    (("quality", "agreement", "confidence", "how reliable"), "quality"),
    (("report", "briefing", "summary of the campaign"), "reporter"),
    (("alert", "spike", "surge"), "alerting"),
)

_ROUTER_SYSTEM = """You are a router for a social-media analysis platform. You do not answer questions — you choose who should.

You get the user's latest message. Decide whether answering it requires querying the platform's own data (posts, comments, sentiment, stance, toxicity, engagement, clusters, coverage statistics), or whether it is general conversation, a definition, a how-does-this-work question, or anything else a plain assistant can answer without data.

If it requires the platform's data, pick the single best-fitting agent from this list:

{agents}

Reply with ONLY a JSON object, no prose:
{{"agent": "<agent name, or null for plain chat>", "reason": "<one short clause>"}}

Rules:
- "agent": null when no data lookup is needed. Prefer null when genuinely unsure — a wrong agent wastes a tool budget and answers the wrong question.
- Never invent an agent name; use one from the list exactly as written.
- A follow-up that refers to earlier data ("and for the other campaign?") still needs an agent.
- If an agent already answered earlier in this conversation, it is named below. Keep it when the new message continues the same line of enquiry; switch only when the question genuinely needs a different capability. Do not switch for rephrasings, clarifications, or "show me more of that"."""


class ChatAgentRequest(BaseModel):
    """A chat message plus how the caller wants it routed."""

    message: Optional[str] = Field(None, description="Single-turn convenience: one user message")
    messages: Optional[list[ChatMessage]] = Field(
        None, description="Full conversation history (the last user turn is the question)"
    )
    agent: str = Field(
        "auto",
        description=(
            '"auto" to let the router choose, "none" to force plain chat, or an '
            "agent name from /v1/agents/types to pin one."
        ),
    )
    campaign_id: Optional[str] = Field(None, description="Campaign to scope the run to")
    backend: Optional[str] = Field(None, description='"local" | "groq" | "auto"/null')
    previous_agent: Optional[str] = Field(
        None,
        description=(
            "Agent that answered the last turn of this conversation. Makes the "
            "router prefer continuity, and lets the response report a switch."
        ),
    )


def _agent_registry() -> dict:
    from defense.services.agents.registry import AGENT_REGISTRY  # noqa: PLC0415

    return AGENT_REGISTRY


def _split_turns(body: ChatAgentRequest) -> tuple[str, list[dict]]:
    """Return ``(question, prior_turns)`` from either request shape."""
    turns = [
        {"role": m.role, "content": m.content}
        for m in (body.messages or [])
        if m.role in ("user", "assistant")
    ]
    if not turns and body.message:
        turns = [{"role": "user", "content": body.message}]
    if not turns or turns[-1]["role"] != "user":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="the conversation must end with a user message",
        )
    return turns[-1]["content"], turns[:-1]


def _keyword_agent(question: str) -> Optional[str]:
    lowered = f" {question.lower()} "
    for needles, agent in _KEYWORD_FALLBACK:
        if any(n in lowered for n in needles):
            return agent
    return None


async def _route(
    question: str,
    prior: list[dict],
    backend_override: Optional[str],
    redis: aioredis.Redis,
    previous_agent: Optional[str] = None,
) -> tuple[Optional[str], str]:
    """Choose an agent for ``question``, or ``None`` to answer as plain chat.

    Returns ``(agent_name, reason)``. Routing never raises: a router that is
    down must degrade to answering the message, not to failing it.
    """
    stripped = question.strip().lower().rstrip("!.?")
    if stripped in _SMALL_TALK:
        return None, "small talk"

    registry = _agent_registry()
    catalogue = "\n".join(f"- {a.name}: {a.description}" for a in registry.values())

    from defense.libs.llm.client import LLMClient  # noqa: PLC0415

    # Two turns of context are enough to resolve "and the other campaign?"
    # without paying to classify the whole conversation.
    context = "".join(
        f"{t['role']}: {t['content'][:300]}\n" for t in prior[-2:]
    )
    user_content = (
        (f"Recent context:\n{context}\n" if context else "")
        + (
            f"Agent that answered the previous turn: {previous_agent}\n"
            if previous_agent
            else ""
        )
        + f"Latest message: {question}"
    )

    try:
        result = await LLMClient().chat(
            role=_ROUTER_ROLE,
            messages=[
                {"role": "system", "content": _ROUTER_SYSTEM.format(agents=catalogue)},
                {"role": "user", "content": user_content},
            ],
            backend_override=backend_override,
            response_format={"type": "json_object"},
            max_tokens=200,
            temperature=0.0,
            usage_redis=redis,
            usage_task="chat_route",
        )
        parsed = json.loads(result.get("content") or "{}")
    except Exception as exc:
        fallback = _keyword_agent(question)
        log.warning("chat_route_failed", error=str(exc), fallback=fallback)
        return fallback, f"router unavailable ({exc}); keyword fallback"

    choice = parsed.get("agent")
    reason = str(parsed.get("reason") or "").strip() or "router choice"

    if choice in (None, "", "null", "none", "chat"):
        return None, reason
    if choice in registry:
        return choice, reason

    # The classifier named something that is not an agent. Its judgement that
    # this needs data is still worth something; its spelling is not.
    fallback = _keyword_agent(question)
    log.warning("chat_route_unknown_agent", proposed=choice, fallback=fallback)
    return fallback, f"router proposed unknown agent {choice!r}; keyword fallback"


@router.post("/agent", summary="Route a chat message to an MCP-backed agent")
async def chat_agent(
    body: ChatAgentRequest,
    redis: aioredis.Redis = Depends(get_redis),
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
    _rl: None = Depends(rate_limit),
) -> dict:
    """Decide who answers this message, and if it's an agent, start the run.

    Returns ``{"mode": "chat", ...}`` when the message needs no corpus data —
    the caller should then use ``POST /v1/chat/stream`` as before. Otherwise
    returns ``{"mode": "agent", ...}`` merged with the agents-service response,
    which is either a finished run or a ``run_id`` to poll at
    ``GET /v1/agents/{run_id}``.
    """
    question, prior = _split_turns(body)

    explicit = _normalise_backend(body.backend)
    backend_override = explicit if explicit is not None else await _toggle_backend(redis)
    effective = backend_override or get_settings().llm_backend
    await check_llm_backend_policy(db, current_user, {"llm_backend": effective})

    requested = (body.agent or "auto").strip().lower()
    registry = _agent_registry()

    previous = body.previous_agent if body.previous_agent in registry else None

    if requested in ("none", "chat", "off"):
        return {"mode": "chat", "agent": None, "reason": "plain chat requested"}
    if requested in ("auto", ""):
        agent_name, reason = await _route(
            question, prior, backend_override, redis, previous_agent=previous
        )
    elif requested in registry:
        agent_name, reason = requested, "pinned by the operator"
    else:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"unknown agent {body.agent!r}; known: {sorted(registry)} (or 'auto'/'none')",
        )

    # A handover mid-conversation is a fact about the answer, not a UI detail:
    # the operator asked one follow-up question and a different analyst with a
    # different toolset picked it up. Say which, and why.
    switched_from = previous if (previous and previous != agent_name) else None

    if agent_name is None:
        return {
            "mode": "chat",
            "agent": None,
            "reason": reason,
            "switched_from": previous,
        }

    payload: dict[str, Any] = {
        "question": question,
        "agent": agent_name,
        "history": prior,
        "want_citations": True,
    }
    if body.campaign_id and body.campaign_id.strip():
        payload["campaign_id"] = body.campaign_id.strip()
    if backend_override:
        payload["llm_backend"] = backend_override

    agents_url = get_settings().agents_service_url
    try:
        async with httpx.AsyncClient(timeout=35.0) as client:
            resp = await client.post(f"{agents_url}/v1/agents/query", json=payload)
    except httpx.RequestError as exc:
        # The agent layer is optional in this deployment. Telling the caller to
        # fall back to plain chat keeps the chat box working; a 502 would make
        # a missing sidecar look like a broken chat.
        log.warning("chat_agent_unreachable", error=str(exc))
        return {
            "mode": "chat",
            "agent": None,
            "reason": f"agents service unreachable ({exc}); answering without tools",
            "degraded": True,
        }

    if resp.status_code >= 500:
        log.error("chat_agent_upstream_error", code=resp.status_code, body=resp.text[:200])
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Agents service returned an error",
        )
    if resp.status_code >= 400:
        raise HTTPException(status_code=resp.status_code, detail=resp.text[:300])

    return {
        "mode": "agent",
        "agent": agent_name,
        "reason": reason,
        "switched_from": switched_from,
        **resp.json(),
    }

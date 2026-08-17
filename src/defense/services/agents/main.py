"""
Agent Orchestrator Service — FastAPI application entry point.

Exposes:
  POST /v1/agents/query  — submit an agent run (async; returns 202 or 200)
  GET  /v1/agents/{run_id} — poll a run by ID
  GET  /v1/agents          — list recent runs
  GET  /health             — liveness probe

Architecture constraints (Architecture.md §11):
  - CPU-only service, no GPU dependency
  - Uses LLM-B (configurable per agent)
  - Corpus-tier ONLY — never invoked per-post
  - Every run is budget-capped (max_tool_calls)
  - Tenant local policy is honoured
  - All data access through MCP tools
"""

from __future__ import annotations

import asyncio
import os
from defense.libs.common.config import get_settings
config = get_settings()
import time
from typing import Any, Optional

import redis.asyncio as aioredis
import structlog
from fastapi import BackgroundTasks, FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, model_validator

from .mcp_client import MCPClient
from .registry import AGENT_REGISTRY
from .runner import AgentRun, AgentRunner
from .store import AgentRunStore

# ---------------------------------------------------------------------------
# Structured logging
# ---------------------------------------------------------------------------

from defense.libs.common.logging import setup_logging  # noqa: E402

setup_logging("agents")
log = structlog.get_logger("agents-service")

# ---------------------------------------------------------------------------
# Configuration from environment
# ---------------------------------------------------------------------------

_REDIS_URL: str = config.redis_url

# Timeout (seconds) for inline / synchronous response — if the agent finishes
# within this window we return 200 with the full result; otherwise 202 + poll URL.
_SYNC_TIMEOUT: float = config.agent_sync_timeout

# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Defense Agent Orchestrator",
    version="1.0.0",
    description=(
        "Corpus-tier agent service for analyst Q&A, coverage analysis, "
        "and alerting over social-media campaign data."
    ),
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Shared singletons (initialised on startup)
# ---------------------------------------------------------------------------

_mcp_client: MCPClient | None = None
_run_store: AgentRunStore | None = None
_agent_runner: AgentRunner | None = None
# Set at startup; the lazy fallbacks below read it when a request arrives before
# (or without) the startup hook — an in-memory store is the correct degradation,
# a NameError is not.
_redis_client: Any = None


def _get_mcp_client() -> MCPClient:
    assert _mcp_client is not None, "MCPClient not initialised"
    return _mcp_client


def _get_store() -> AgentRunStore:
    global _run_store
    if _run_store is None:
        _run_store = AgentRunStore(redis=_redis_client)
    return _run_store


def _get_runner() -> AgentRunner:
    global _agent_runner
    if _agent_runner is None:
        from defense.libs.llm.client import LLMClient
        mcp_client = MCPClient()
        _agent_runner = AgentRunner(llm_client=LLMClient(), mcp_client=mcp_client)
    return _agent_runner


# ---------------------------------------------------------------------------
# Pydantic request / response models
# ---------------------------------------------------------------------------


class DateRange(BaseModel):
    from_: str = Field(..., alias="from")
    to: str

    model_config = {"populate_by_name": True}


class ChatTurn(BaseModel):
    role: str = Field(..., description='"user" or "assistant"')
    content: str


class AgentQueryRequest(BaseModel):
    question: Optional[str] = Field(None, description="Natural language question or instruction.")
    query: Optional[str] = Field(None, description="Natural language query alias.")
    campaign_id: Optional[str] = Field(None, description="Campaign to scope the analysis to.")
    date_range: Optional[DateRange] = Field(None, description="Optional date range hint passed as context.")
    agent: Optional[str] = Field(
        None,
        description=(
            "Agent to invoke: analyst, coverage, alerting, stance, "
            "comparator, toxicity, narrative, quality, reporter."
        ),
    )
    agent_type: Optional[str] = Field(None, description="Agent type alias.")
    max_tool_calls: Optional[int] = Field(
        None,
        ge=1,
        le=50,
        description=(
            "Budget cap for this run. Omit to use the agent's own cap from the "
            "registry (a comparator needs more calls than an alerting sweep)."
        ),
    )
    llm_backend: Optional[str] = Field(
        None, description="Per-request backend override (local/groq), subject to tenant policy."
    )
    want_citations: bool = Field(True, description="Whether to extract and return post ID citations.")
    history: Optional[list[ChatTurn]] = Field(
        None,
        description=(
            "Prior conversation turns, oldest first, excluding the current "
            "question. Sent by the chat surface so follow-ups keep their context."
        ),
    )

    @model_validator(mode="after")
    def populate_aliases(self) -> "AgentQueryRequest":
        if not self.question and self.query:
            self.question = self.query
        if not self.question:
            raise ValueError("question or query is required")
        if not self.agent and self.agent_type:
            self.agent = self.agent_type
        if not self.agent:
            self.agent = "analyst"
        return self


class AgentQueryResponse(BaseModel):
    run_id: str
    status: str
    status_url: str
    answer: Optional[str] = None
    citations: list[str] = Field(default_factory=list)
    comment_citations: list[str] = Field(
        default_factory=list,
        description=(
            "Comment IDs returned by the tools. Separate from `citations` because "
            "a comment ID is the same CUID shape as a post ID but is not a post."
        ),
    )
    unverified_citations: list[str] = Field(
        default_factory=list,
        description="Post or comment IDs asserted in the answer that no tool call returned.",
    )
    unverified_quotes: list[str] = Field(
        default_factory=list,
        description="Quoted comment text in the answer that no tool call returned.",
    )
    unverified_stats: list[str] = Field(
        default_factory=list,
        description=(
            "Claims about comment content in a run where no comment-level tool "
            "returned data."
        ),
    )
    tools_used: list[Any] = Field(default_factory=list)
    llm_backend: Optional[str] = None
    llm_model: Optional[str] = None
    usage: dict = Field(default_factory=dict)
    query: Optional[str] = None
    agent_name: Optional[str] = None
    agent_type: Optional[str] = None
    campaign_id: Optional[str] = None
    error: Optional[str] = None
    created_at: Optional[float] = None
    completed_at: Optional[float] = None


# ---------------------------------------------------------------------------
# Helper: convert AgentRun → response dict
# ---------------------------------------------------------------------------


def _run_to_response(run: AgentRun, base_url: str, want_citations: bool = True) -> AgentQueryResponse:
    return AgentQueryResponse(
        run_id=run.run_id,
        status=run.status,
        status_url=f"{base_url}/v1/agents/{run.run_id}",
        answer=run.answer,
        citations=run.citations if want_citations else [],
        comment_citations=run.comment_citations if want_citations else [],
        unverified_citations=run.unverified_citations,
        unverified_quotes=run.unverified_quotes,
        unverified_stats=run.unverified_stats,
        tools_used=run.tools_used,
        llm_backend=run.llm_backend,
        llm_model=run.llm_model,
        usage=run.usage,
        query=run.query,
        agent_name=run.agent_name,
        agent_type=run.agent_name,
        campaign_id=run.campaign_id,
        error=run.error,
        created_at=run.created_at,
        completed_at=run.completed_at,
    )


# ---------------------------------------------------------------------------
# Background task wrapper
# ---------------------------------------------------------------------------


# In-flight asyncio tasks keyed by run_id (enables active cancellation on delete)
_active_tasks: dict[str, asyncio.Task] = {}


async def _run_agent_task(
    runner: AgentRunner,
    store: AgentRunStore,
    agent_name: str,
    query: str,
    campaign_id: Optional[str],
    run_id: str,
    max_tool_calls: int,
    backend_override: Optional[str],
    tenant_policy: Any,
    history: Optional[list[dict]] = None,
) -> AgentRun:
    """Execute an agent run and persist the result."""
    from .registry import AGENT_REGISTRY

    agent_def = AGENT_REGISTRY[agent_name]
    try:
        run = await runner.run(
            agent_def=agent_def,
            query=query,
            campaign_id=campaign_id,
            run_id=run_id,
            max_tool_calls=max_tool_calls,
            tenant_policy=tenant_policy,
            backend_override=backend_override,
            history=history,
            # Persist each tool call as it starts and finishes, so a poller sees
            # the trace build up instead of a spinner followed by everything.
            on_progress=store.save,
        )
        await store.save(run)
        return run
    except asyncio.CancelledError:
        log.info("agent_run_cancelled", run_id=run_id)
        cancelled_run = AgentRun(
            run_id=run_id,
            agent_name=agent_name,
            query=query,
            campaign_id=campaign_id,
            status="cancelled",
            error="Task cancelled by user.",
        )
        await store.save(cancelled_run)
        raise
    except Exception as exc:
        log.error("agent_run_error", run_id=run_id, error=str(exc))
        failed_run = AgentRun(
            run_id=run_id,
            agent_name=agent_name,
            query=query,
            campaign_id=campaign_id,
            status="failed",
            error=str(exc),
        )
        await store.save(failed_run)
        return failed_run
    finally:
        _active_tasks.pop(run_id, None)


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


@app.on_event("startup")
async def on_startup() -> None:
    global _mcp_client, _run_store, _agent_runner, _redis_client

    # Initialise Redis (optional — store falls back to in-memory on failure)
    redis_client = None
    try:
        redis_client = aioredis.from_url(
            _REDIS_URL, encoding="utf-8", decode_responses=True
        )
        await redis_client.ping()
        log.info("redis_connected", url=_REDIS_URL)
    except Exception as exc:
        log.warning("redis_unavailable", error=str(exc), fallback="in-memory store")
        redis_client = None

    _mcp_client = MCPClient()
    _redis_client = redis_client
    _run_store = AgentRunStore(redis=redis_client)

    # Initialise the LLM client
    from defense.libs.llm.client import LLMClient

    _agent_runner = AgentRunner(
        llm_client=LLMClient(),
        mcp_client=_mcp_client,
    )

    log.info(
        "agents_service_started",
        agents=list(AGENT_REGISTRY.keys()),
        sync_timeout=_SYNC_TIMEOUT,
    )


@app.on_event("shutdown")
async def on_shutdown() -> None:
    log.info("agents_service_stopping")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/health", summary="Liveness probe")
async def health() -> dict:
    """Always returns 200 when the process is running."""
    return {
        "status": "ok",
        "service": "defense-agents",
        "agents": list(AGENT_REGISTRY.keys()),
    }


@app.get("/v1/agents/types", summary="List available agent types and their descriptions")
async def list_agent_types() -> list[dict]:
    """Returns available agent types with their descriptions, permitted MCP tools, and max tool budget."""
    return [
        {
            "name": a.name,
            "description": a.description,
            "tools": a.tools,
            "llm_role": a.llm_role,
            "max_tool_calls": a.max_tool_calls,
        }
        for a in AGENT_REGISTRY.values()
    ]


@app.post(
    "/v1/agents/query",
    summary="Submit an agent run",
    status_code=status.HTTP_202_ACCEPTED,
)
async def submit_query(
    request: AgentQueryRequest,
    background_tasks: BackgroundTasks,
) -> AgentQueryResponse:
    """Submit an agent question.

    If the agent finishes within the sync timeout window (default 28 s) the
    response is HTTP 200 with the full result.  Otherwise the response is
    HTTP 202 Accepted with a ``status_url`` to poll.
    """
    # Validate agent name
    if request.agent not in AGENT_REGISTRY:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown agent {request.agent!r}. Known: {sorted(AGENT_REGISTRY)}",
        )

    runner = _get_runner()
    store = _get_store()

    import uuid

    run_id = str(uuid.uuid4())

    # Build query string — include date range as context if provided
    query = request.question
    if request.date_range:
        query += (
            f"\n\n[Date range context: from {request.date_range.from_} "
            f"to {request.date_range.to}]"
        )

    # Load tenant policy — currently uses the default (no restrictions).
    # In a multi-tenant deployment, retrieve by tenant_id from the auth context.
    from defense.libs.llm.policy import DEFAULT_POLICY

    tenant_policy = DEFAULT_POLICY

    # Create a placeholder run so GET /v1/agents/{run_id} works immediately
    from .runner import AgentRun

    placeholder = AgentRun(
        run_id=run_id,
        agent_name=request.agent,
        query=query,
        campaign_id=request.campaign_id,
        status="running",
    )
    await store.save(placeholder)

    base_url = config.public_base_url

    # The registry's per-agent cap is the default, not a decoration: a comparator
    # must pull both sides of a comparison and a reporter builds seven sections,
    # so capping every agent at the request default silently truncates them.
    # An explicit request value still wins.
    max_tool_calls = (
        request.max_tool_calls
        if request.max_tool_calls is not None
        else AGENT_REGISTRY[request.agent].max_tool_calls
    )

    # Launch the agent as an asyncio task
    task = asyncio.create_task(
        _run_agent_task(
            runner=runner,
            store=store,
            agent_name=request.agent,
            query=query,
            campaign_id=request.campaign_id,
            run_id=run_id,
            max_tool_calls=max_tool_calls,
            backend_override=request.llm_backend,
            tenant_policy=tenant_policy,
            history=[t.model_dump() for t in request.history] if request.history else None,
        )
    )
    _active_tasks[run_id] = task

    # Try to wait for the result within the sync timeout window
    try:
        completed_run: AgentRun = await asyncio.wait_for(
            asyncio.shield(task), timeout=_SYNC_TIMEOUT
        )
        # Completed in time — return 200 with full result
        return _run_to_response(
            completed_run,
            base_url=base_url,
            want_citations=request.want_citations,
        )
    except asyncio.TimeoutError:
        # Still running — return 202 Accepted so the client can poll
        log.info("agent_run_async", run_id=run_id, reason="sync timeout exceeded")
        return AgentQueryResponse(
            run_id=run_id,
            status="running",
            status_url=f"{base_url}/v1/agents/{run_id}",
        )


@app.get(
    "/v1/agents/{run_id}",
    summary="Get a single agent run",
)
async def get_run(run_id: str) -> AgentQueryResponse:
    """Retrieve the full result of a run by ID."""
    store = _get_store()
    run = await store.get(run_id)
    if run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Run {run_id!r} not found (may have expired after 24 h).",
        )
    base_url = config.public_base_url
    return _run_to_response(run, base_url=base_url)


@app.get(
    "/v1/agents",
    summary="List recent agent runs",
)
async def list_runs(limit: int = 20) -> list[AgentQueryResponse]:
    """Return the most recent `limit` runs, newest first."""
    if limit < 1 or limit > 100:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="limit must be between 1 and 100.",
        )
    store = _get_store()
    runs = await store.list_recent(limit=limit)
    base_url = config.public_base_url
    return [_run_to_response(r, base_url=base_url) for r in runs]


@app.delete(
    "/v1/agents/{run_id}",
    summary="Delete / Cancel a single agent run",
)
async def delete_run(run_id: str) -> dict:
    """Delete a single agent run by ID and abort it if currently running."""
    active_task = _active_tasks.pop(run_id, None)
    if active_task and not active_task.done():
        active_task.cancel()
        log.info("agent_task_aborted_on_delete", run_id=run_id)

    store = _get_store()
    deleted = await store.delete(run_id)
    return {"deleted": deleted, "run_id": run_id}


@app.delete(
    "/v1/agents",
    summary="Clear all agent run history",
)
async def clear_all_runs() -> dict:
    """Delete all agent run history and cancel all active tasks."""
    for r_id, active_task in list(_active_tasks.items()):
        if not active_task.done():
            active_task.cancel()
            log.info("agent_task_aborted_on_clear_all", run_id=r_id)
    _active_tasks.clear()

    store = _get_store()
    count = await store.clear_all()
    return {"status": "cleared", "deleted_count": count}


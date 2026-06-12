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
import time
from typing import Any, Optional

import redis.asyncio as aioredis
import structlog
from fastapi import BackgroundTasks, FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from .mcp_client import MCPClient
from .registry import AGENT_REGISTRY
from .runner import AgentRun, AgentRunner
from .store import AgentRunStore

# ---------------------------------------------------------------------------
# Structured logging
# ---------------------------------------------------------------------------

from libs.common.logging import setup_logging  # noqa: E402

setup_logging("agents")
log = structlog.get_logger("agents-service")

# ---------------------------------------------------------------------------
# Configuration from environment
# ---------------------------------------------------------------------------

_REDIS_URL: str = os.getenv("REDIS_URL", "redis://localhost:6379/0")

# Timeout (seconds) for inline / synchronous response — if the agent finishes
# within this window we return 200 with the full result; otherwise 202 + poll URL.
_SYNC_TIMEOUT: float = float(os.getenv("AGENT_SYNC_TIMEOUT", "28"))

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


def _get_mcp_client() -> MCPClient:
    assert _mcp_client is not None, "MCPClient not initialised"
    return _mcp_client


def _get_store() -> AgentRunStore:
    assert _run_store is not None, "AgentRunStore not initialised"
    return _run_store


def _get_runner() -> AgentRunner:
    assert _agent_runner is not None, "AgentRunner not initialised"
    return _agent_runner


# ---------------------------------------------------------------------------
# Pydantic request / response models
# ---------------------------------------------------------------------------


class DateRange(BaseModel):
    from_: str = Field(..., alias="from")
    to: str

    model_config = {"populate_by_name": True}


class AgentQueryRequest(BaseModel):
    question: str = Field(..., min_length=1, description="Natural language question or instruction.")
    campaign_id: Optional[str] = Field(None, description="Campaign to scope the analysis to.")
    date_range: Optional[DateRange] = Field(None, description="Optional date range hint passed as context.")
    agent: str = Field("analyst", description="Agent to invoke: analyst / coverage / alerting.")
    max_tool_calls: int = Field(10, ge=1, le=50, description="Budget cap for this run.")
    llm_backend: Optional[str] = Field(
        None, description="Per-request backend override (local/groq), subject to tenant policy."
    )
    want_citations: bool = Field(True, description="Whether to extract and return post ID citations.")


class AgentQueryResponse(BaseModel):
    run_id: str
    status: str
    status_url: str
    answer: Optional[str] = None
    citations: list[str] = Field(default_factory=list)
    tools_used: list[Any] = Field(default_factory=list)
    llm_backend: Optional[str] = None
    llm_model: Optional[str] = None
    usage: dict = Field(default_factory=dict)


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
        tools_used=run.tools_used,
        llm_backend=run.llm_backend,
        llm_model=run.llm_model,
        usage=run.usage,
    )


# ---------------------------------------------------------------------------
# Background task wrapper
# ---------------------------------------------------------------------------


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
) -> AgentRun:
    """Execute an agent run and persist the result."""
    from .registry import AGENT_REGISTRY

    agent_def = AGENT_REGISTRY[agent_name]
    run = await runner.run(
        agent_def=agent_def,
        query=query,
        campaign_id=campaign_id,
        run_id=run_id,
        max_tool_calls=max_tool_calls,
        tenant_policy=tenant_policy,
        backend_override=backend_override,
    )
    await store.save(run)
    return run


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


@app.on_event("startup")
async def on_startup() -> None:
    global _mcp_client, _run_store, _agent_runner

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
    _run_store = AgentRunStore(redis=redis_client)

    # Initialise the LLM client
    from libs.llm.client import LLMClient

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
    from libs.llm.policy import DEFAULT_POLICY

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

    base_url = os.getenv("PUBLIC_BASE_URL", "")

    # Launch the agent as an asyncio task
    task = asyncio.create_task(
        _run_agent_task(
            runner=runner,
            store=store,
            agent_name=request.agent,
            query=query,
            campaign_id=request.campaign_id,
            run_id=run_id,
            max_tool_calls=request.max_tool_calls,
            backend_override=request.llm_backend,
            tenant_policy=tenant_policy,
        )
    )

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
    base_url = os.getenv("PUBLIC_BASE_URL", "")
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
    base_url = os.getenv("PUBLIC_BASE_URL", "")
    return [_run_to_response(r, base_url=base_url) for r in runs]

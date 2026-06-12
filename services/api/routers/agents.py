"""Agents router — proxy to the agents microservice."""

from __future__ import annotations

import os
from typing import Any

import httpx
import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from deps import get_current_user

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/v1/agents", tags=["agents"])

AGENTS_URL: str = os.getenv("AGENTS_SERVICE_URL", "http://agents:8010")


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class AgentQueryRequest(BaseModel):
    """Request body forwarded to the agents service."""

    query: str
    """The natural-language query or instruction for the agent."""

    campaign_id: str | None = None
    """Optional campaign scope for the agent run."""

    post_ids: list[str] | None = None
    """Explicit post IDs the agent should operate on."""

    agent_type: str | None = None
    """Agent variant to invoke (e.g. 'analysis', 'report', 'search')."""

    options: dict[str, Any] | None = None
    """Arbitrary key/value options forwarded verbatim to the agents service."""

    stream: bool = False
    """When True the agents service should stream partial results."""


# ---------------------------------------------------------------------------
# POST /v1/agents/query
# ---------------------------------------------------------------------------


@router.post(
    "/query",
    summary="Submit a query to the agents service",
    status_code=status.HTTP_200_OK,
)
async def query_agent(
    request: AgentQueryRequest,
    user: dict = Depends(get_current_user),
) -> Any:
    """POST /v1/agents/query — proxy to the agents service.

    Returns 202 Accepted with ``run_id`` for async execution, or 200 with a
    full result if the agents service responds within 30 s.
    """
    # Translate the public API shape to the agents-service contract:
    # query → question, agent_type → agent (extras the service doesn't know are dropped).
    payload = request.model_dump(exclude_none=True)
    payload["question"] = payload.pop("query")
    if "agent_type" in payload:
        payload["agent"] = payload.pop("agent_type")
    for extra in ("stream", "post_ids", "options"):
        payload.pop(extra, None)

    async with httpx.AsyncClient(timeout=35.0) as client:
        try:
            resp = await client.post(
                f"{AGENTS_URL}/v1/agents/query",
                json=payload,
            )
        except httpx.RequestError as exc:
            log.error("agents_proxy_error", endpoint="query", error=str(exc))
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Agents service unavailable",
            ) from exc

        if resp.status_code >= 500:
            log.error(
                "agents_service_error",
                endpoint="query",
                upstream_status=resp.status_code,
                body=resp.text[:200],
            )
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Agents service returned an error",
            )

        return resp.json()


# ---------------------------------------------------------------------------
# GET /v1/agents
# ---------------------------------------------------------------------------


@router.get(
    "",
    summary="List recent agent runs",
)
async def list_agent_runs(
    limit: int = Query(20, ge=1, le=200, description="Maximum number of runs to return"),
    user: dict = Depends(get_current_user),
) -> Any:
    """GET /v1/agents — list recent agent runs from the agents service."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            resp = await client.get(
                f"{AGENTS_URL}/v1/agents",
                params={"limit": limit},
            )
        except httpx.RequestError as exc:
            log.error("agents_proxy_error", endpoint="list", error=str(exc))
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Agents service unavailable",
            ) from exc

        if resp.status_code >= 500:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Agents service returned an error",
            )

        return resp.json()


# ---------------------------------------------------------------------------
# GET /v1/agents/{run_id}
# ---------------------------------------------------------------------------


@router.get(
    "/{run_id}",
    summary="Poll agent run status and result",
)
async def get_agent_run(
    run_id: str,
    user: dict = Depends(get_current_user),
) -> Any:
    """GET /v1/agents/{run_id} — poll agent run status / result."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            resp = await client.get(f"{AGENTS_URL}/v1/agents/{run_id}")
        except httpx.RequestError as exc:
            log.error("agents_proxy_error", endpoint="get_run", run_id=run_id, error=str(exc))
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Agents service unavailable",
            ) from exc

        if resp.status_code == 404:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Agent run not found",
            )

        if resp.status_code >= 500:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Agents service returned an error",
            )

        return resp.json()

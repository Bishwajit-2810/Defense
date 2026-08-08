"""Health and readiness endpoints."""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

import redis.asyncio as aioredis
from defense.services.api.deps import get_db, get_redis

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/v1", tags=["health"])


@router.get("/health", summary="Liveness probe")
async def health() -> dict:
    """Return 200 OK if the process is alive."""
    return {"status": "ok", "version": "1.0.0"}


@router.get("/ready", summary="Readiness probe")
async def ready(
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
) -> dict:
    """Check Postgres and Redis connectivity.

    Returns 200 when both backends are reachable, 503 otherwise.
    """
    errors: dict[str, str] = {}

    # --- Postgres ---
    try:
        await db.execute(text("SELECT 1"))
    except Exception as exc:
        log.error("readiness_postgres_failed", error=str(exc))
        errors["postgres"] = str(exc)

    # --- Redis ---
    try:
        pong = await redis.ping()
        if not pong:
            raise RuntimeError("PONG not received")
    except Exception as exc:
        log.error("readiness_redis_failed", error=str(exc))
        errors["redis"] = str(exc)

    if errors:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"status": "unavailable", "errors": errors},
        )

    return {"status": "ready", "postgres": "ok", "redis": "ok"}

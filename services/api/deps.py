"""FastAPI dependency providers.

Provides:
  - get_db()       — async SQLAlchemy session
  - get_redis()    — async Redis client
  - verify_token() — JWT validation
  - get_current_user() — Bearer JWT or X-API-Key authentication
"""

from __future__ import annotations

import os
from typing import AsyncGenerator

import redis.asyncio as aioredis
import structlog
from fastapi import Header, HTTPException, Query, Security, status
from sqlalchemy import text
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

_DATABASE_URL: str = os.environ.get(
    "DATABASE_URL",
    "postgresql+asyncpg://defense:defense@localhost:5432/defense",
)

# SQLAlchemy requires the +asyncpg driver scheme
if _DATABASE_URL.startswith("postgresql://"):
    _DATABASE_URL = _DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)
elif _DATABASE_URL.startswith("postgres://"):
    _DATABASE_URL = _DATABASE_URL.replace("postgres://", "postgresql+asyncpg://", 1)

engine = create_async_engine(
    _DATABASE_URL,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=20,
    echo=False,
)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
    autocommit=False,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Yield an async SQLAlchemy session and close it after the request."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


# ---------------------------------------------------------------------------
# Redis
# ---------------------------------------------------------------------------

_REDIS_URL: str = os.environ.get("REDIS_URL", "redis://localhost:6379/0")

_redis_pool: aioredis.Redis | None = None


def _get_redis_pool() -> aioredis.Redis:
    global _redis_pool
    if _redis_pool is None:
        _redis_pool = aioredis.from_url(
            _REDIS_URL,
            encoding="utf-8",
            decode_responses=True,
        )
    return _redis_pool


async def get_redis() -> AsyncGenerator[aioredis.Redis, None]:
    """Yield the shared async Redis client."""
    client = _get_redis_pool()
    try:
        yield client
    finally:
        pass  # Pool is shared; do not close per-request


# ---------------------------------------------------------------------------
# JWT auth
# ---------------------------------------------------------------------------

_JWT_SECRET: str = os.environ.get("JWT_SECRET", "change-me")
_JWT_ALGORITHM: str = "HS256"


def verify_token(token: str) -> dict:
    """Validate a JWT and return its payload.

    Raises:
        HTTPException 401 if the token is invalid or expired.
    """
    try:
        payload = jwt.decode(token, _JWT_SECRET, algorithms=[_JWT_ALGORITHM])
        return payload
    except JWTError as exc:
        log.warning("jwt_validation_failed", error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


# ---------------------------------------------------------------------------
# Unified auth dependency
# ---------------------------------------------------------------------------

_bearer_scheme = HTTPBearer(auto_error=False)


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Security(_bearer_scheme),
    api_key: str | None = Header(None, alias="X-API-Key"),
    api_key_query: str | None = Query(
        None, alias="api_key", include_in_schema=False
    ),
) -> dict:
    """Authenticate via Bearer JWT, X-API-Key header, or ``?api_key=`` query param.

    The query-param form exists for browser EventSource (SSE) connections,
    which cannot set custom headers.

    Returns a dict representing the authenticated principal.

    MVP behaviour:
    - JWT: fully validated (HS256, JWT_SECRET).
    - API key: accepted if non-empty (real key storage in Phase 2).

    Raises:
        HTTPException 401 if no credential is present or valid.
    """
    # --- Bearer JWT path ---
    if credentials is not None:
        payload = verify_token(credentials.credentials)
        return {"sub": payload.get("sub", "unknown"), "auth_method": "jwt", **payload}

    # --- API key path (header preferred, query param for EventSource) ---
    effective_key = (api_key or "").strip() or (api_key_query or "").strip()
    if effective_key:
        # MVP: accept any non-empty key; Phase 2 will look up in DB
        log.debug("api_key_auth", key_prefix=effective_key[:8] + "…")
        return {"sub": "api_key_user", "auth_method": "api_key", "api_key": effective_key}

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Authentication required: provide a Bearer token or X-API-Key header",
        headers={"WWW-Authenticate": "Bearer"},
    )


# ---------------------------------------------------------------------------
# Tenant LLM policy enforcement (api_design.md §1b / architecture.md §9)
# ---------------------------------------------------------------------------


async def check_llm_backend_policy(
    db: AsyncSession,
    current_user: dict,
    options: dict | None,
) -> None:
    """Reject a per-request ``llm_backend: "groq"`` override when the tenant
    is privacy-locked (tenant_policies.privacy_locked).

    Raises:
        HTTPException 403 when the override violates the tenant policy.
    """
    requested = (options or {}).get("llm_backend")
    if requested != "groq":
        return
    tenant_id = current_user.get("tenant_id") or "default"
    try:
        row = (
            await db.execute(
                text(
                    "SELECT privacy_locked FROM tenant_policies WHERE tenant_id = :tid"
                ),
                {"tid": tenant_id},
            )
        ).first()
    except Exception as exc:
        log.warning("tenant_policy_lookup_failed", tenant_id=tenant_id, error=str(exc))
        return
    if row and row[0]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"Tenant '{tenant_id}' is privacy-locked: llm_backend='groq' "
                "is not permitted (data must not leave the local backend)"
            ),
        )

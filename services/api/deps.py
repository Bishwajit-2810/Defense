"""FastAPI dependency providers.

Provides:
  - get_db()       — async SQLAlchemy session
  - get_redis()    — async Redis client
  - verify_token() — JWT validation
  - get_current_user() — Bearer JWT or X-API-Key authentication
"""

from __future__ import annotations

import os
import sys
from typing import AsyncGenerator

import redis.asyncio as aioredis
import structlog
from fastapi import Depends, Header, HTTPException, Query, Request, Response, Security, status
from sqlalchemy import text
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# Repo root on path so `libs.*` imports when the API runs from services/api.
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from libs.common.config import (  # noqa: E402
    JWT_ALGORITHM,
    get_jwt_secret,
    jwt_secret_fingerprint,
    jwt_secret_is_default,
    require_jwt_secret,
)
from libs.ratelimit import check_rate_limit  # noqa: E402

log = structlog.get_logger(__name__)

# Per-identity request rate limit (architecture §9).
RATE_LIMIT_ENABLED = os.getenv("RATE_LIMIT_ENABLED", "true").lower() == "true"
RATE_LIMIT_PER_MIN = int(os.getenv("RATE_LIMIT_PER_MIN", "120"))

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

# The secret lives in libs/common/config.py and is read per call, so issuer and
# verifier cannot drift and the value can be rotated without a restart.
# Verified at import so a misconfigured deployment fails at startup rather than
# on the first request.
require_jwt_secret()
log.info(
    "jwt_verifier_ready",
    secret_fingerprint=jwt_secret_fingerprint(),
    is_default_secret=jwt_secret_is_default(),
)

# Claims the server is willing to take from a token body. `auth_method` is
# deliberately absent: it is how downstream code tells a human session from a
# service call, and it is set server-side after the merge, never by the client.
_ALLOWED_JWT_CLAIMS = frozenset({"sub", "tenant_id", "role", "exp", "iat", "scope"})


def verify_token(token: str) -> dict:
    """Validate a JWT and return its payload.

    Raises:
        HTTPException 401 if the token is invalid or expired.
    """
    try:
        payload = jwt.decode(token, get_jwt_secret(), algorithms=[JWT_ALGORITHM])
        return payload
    except JWTError as exc:
        log.warning("jwt_validation_failed", error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


def _looks_like_jwt(credential: str) -> bool:
    """True when a credential has the shape of a JWS compact serialization.

    Used to decide whether an opaque-looking credential should be *verified* as
    a token rather than waved through as an API key. Shape only — validity is
    still decided by verify_token().
    """
    parts = credential.split(".")
    return len(parts) == 3 and all(parts) and parts[0].startswith("ey")


def _principal_from_claims(payload: dict) -> dict:
    """Build the authenticated principal from verified token claims.

    The payload used to be spread LAST over the server's own fields, so any
    claim in the token won — a self-signed token could set `auth_method` to
    "internal-service" or `tenant_id` to another tenant's. Server-controlled
    fields are now written after an allowlisted copy of the claims.
    """
    principal = {k: v for k, v in payload.items() if k in _ALLOWED_JWT_CLAIMS}
    principal["sub"] = payload.get("sub") or "unknown"
    principal["auth_method"] = "jwt"
    return principal


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

    **Any credential that looks like a JWT is verified as one, on every
    transport.** It used to be that only the `Authorization` header was parsed
    as a token: a credential arriving via `?api_key=` or `X-API-Key` was treated
    as an opaque API key and accepted merely for being non-empty. Since the
    dashboard sends the session token that way on all four SSE streams, an
    expired token — and a token signed with the wrong secret — both
    authenticated, expiry was unenforceable on streams, and the user's identity
    collapsed to the shared `api_key_user` principal (losing tenant claims and
    bucketing all stream traffic into one rate-limit key).

    Returns a dict representing the authenticated principal.

    MVP behaviour:
    - JWT: fully validated (HS256, JWT_SECRET) on every transport.
    - API key: accepted if non-empty (real key storage in Phase 2).

    Raises:
        HTTPException 401 if no credential is present or valid.
    """
    # --- Bearer JWT path ---
    if credentials is not None:
        return _principal_from_claims(verify_token(credentials.credentials))

    # --- Header / query credential (header preferred, query for EventSource) ---
    effective_key = (api_key or "").strip() or (api_key_query or "").strip()
    if effective_key:
        # A token presented here is still a token. Verify it as one — including
        # its signature and expiry — instead of accepting it as an opaque
        # string. Only a credential that is not JWT-shaped falls through to the
        # API-key path.
        if _looks_like_jwt(effective_key):
            return _principal_from_claims(verify_token(effective_key))
        # MVP: accept any non-empty key; Phase 2 will look up in DB
        log.debug("api_key_auth", key_prefix=effective_key[:8] + "…")
        return {"sub": "api_key_user", "auth_method": "api_key", "api_key": effective_key}

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Authentication required: provide a Bearer token or X-API-Key header",
        headers={"WWW-Authenticate": "Bearer"},
    )


# ---------------------------------------------------------------------------
# Rate limiting (architecture §9) — per authenticated principal (or client IP)
# ---------------------------------------------------------------------------


async def rate_limit(
    request: Request,
    response: Response,
    redis: aioredis.Redis = Depends(get_redis),
    current_user: dict = Depends(get_current_user),
) -> None:
    """Dependency that enforces a per-identity request budget per minute.

    Attach to expensive endpoints (analysis runs, report/agent generation). Sets
    ``X-RateLimit-*`` headers and raises 429 with ``Retry-After`` when exceeded.
    Disabled with ``RATE_LIMIT_ENABLED=false``.
    """
    if not RATE_LIMIT_ENABLED:
        return

    ident = current_user.get("sub") or (request.client.host if request.client else "anon")
    result = await check_rate_limit(
        redis, ident, limit=RATE_LIMIT_PER_MIN, window_seconds=60
    )
    response.headers["X-RateLimit-Limit"] = str(result.limit)
    response.headers["X-RateLimit-Remaining"] = str(result.remaining)
    if not result.allowed:
        log.warning("rate_limited", identity=ident, limit=result.limit)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Rate limit exceeded — slow down.",
            headers={"Retry-After": str(result.window_seconds)},
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

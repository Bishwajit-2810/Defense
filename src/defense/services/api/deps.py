"""FastAPI dependency providers.

Provides:
  - get_db()       — async SQLAlchemy session
  - get_redis()    — async Redis client
  - verify_token() — JWT validation
  - get_current_user() — Bearer JWT or X-API-Key authentication
"""

from __future__ import annotations

from defense.libs.common.config import get_settings

config = get_settings()
import sys
import time
from typing import Any, AsyncGenerator

import redis.asyncio as aioredis
import structlog
from fastapi import Depends, Header, HTTPException, Query, Request, Response, Security, status
from sqlalchemy import text
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# Repo root on path so `libs.*` imports when the API runs from services/api.

from defense.libs.auth import hash_api_key, redeem_sse_ticket  # noqa: E402
from defense.libs.common.config import (  # noqa: E402
    JWT_ALGORITHM,
    app_env,
    get_jwt_secret,
    jwt_secret_fingerprint,
    jwt_secret_is_default,
    require_jwt_secret,
)
from defense.libs.ratelimit import check_rate_limit  # noqa: E402

# MVP escape hatch: accept any non-empty API key, as the system did before
# `api_keys` existed. Defaults to ON only in a dev environment, so a production
# deployment that forgets to provision keys fails closed instead of open.
_ALLOW_UNKNOWN_API_KEYS = (
    config.allow_unknown_api_keys
    if config.allow_unknown_api_keys is not None
    else (app_env() in ("dev", "development", "local", "test", "ci"))
)

log = structlog.get_logger(__name__)

#: The runtime LLM-backend override the dashboard sets and the pipeline workers
#: read. Mirrors `routers/config.py::LLM_BACKEND_KEY` and
#: `stage2_llm/worker.py` — this is the key that actually decides which backend
#: a post is analysed on, which is why the tenant policy has to be applied
#: against it and not only against the per-request option (§13.5).
LLM_BACKEND_CONFIG_KEY = "config:llm_backend"

# Per-identity request rate limit (architecture §9).
RATE_LIMIT_ENABLED = get_settings().rate_limit_enabled
RATE_LIMIT_PER_MIN = get_settings().rate_limit_per_min

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

_DATABASE_URL: str = get_settings().database_url or "postgresql+asyncpg://defense:defense@localhost:5432/defense"

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

_REDIS_URL: str = config.redis_url

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

#: After a hard lookup failure, suppress retries for this many seconds so a
#: missing table costs one warning burst rather than one per request. Recovers
#: automatically when the DB comes back — unlike the old boolean flag that
#: permanently disabled lookups until a process restart (§P7.11).
_API_KEY_TABLE_RETRY_AFTER: float = 0.0


async def _principal_from_api_key(db: AsyncSession, raw_key: str) -> dict | None:
    """Look up an API key by hash. The tenant comes from the ROW, not the client.

    Returns None when the key is unknown — the caller decides whether that is a
    401 or, in dev, a fall-through to the permissive MVP behaviour.
    """
    global _API_KEY_TABLE_RETRY_AFTER
    if time.time() < _API_KEY_TABLE_RETRY_AFTER:
        # A recent lookup failed hard (no table, DB down). Suppress retries
        # for 30 s to avoid one warning per request, then try again.
        return None

    key_hash = hash_api_key(raw_key)
    try:
        row = (
            await db.execute(
                text(
                    "SELECT tenant_id, role, label FROM api_keys "
                    "WHERE key_hash = :h AND active = TRUE"
                ),
                {"h": key_hash},
            )
        ).first()
    except Exception as exc:
        # No table yet, or the DB is down. Log ONCE and let the caller apply its
        # own policy rather than 500-ing every request.
        #
        # The ROLLBACK is not optional. On Postgres a failed statement aborts the
        # whole transaction, and this session is the request's session — the
        # endpoint handler runs its own queries on it moments later. Without the
        # rollback, swallowing this error only *looked* like graceful
        # degradation: the request went on to die with
        # `InFailedSQLTransactionError: current transaction is aborted`, i.e. a
        # 500 from whatever endpoint the caller was hitting, in the exact failure
        # mode this branch exists to avoid. Only the first request per process
        # showed it (the flag below skips the lookup afterwards), which is what
        # made it easy to miss.
        _API_KEY_TABLE_RETRY_AFTER = time.time() + 30
        try:
            await db.rollback()
        except Exception:
            pass
        log.warning(
            "api_key_lookup_failed_disabling_lookup",
            error=str(exc),
            detail=(
                "api_keys is unreadable — falling back to the ALLOW_UNKNOWN_API_KEYS "
                "policy for the rest of this process. Run deploy/init-db.sql to "
                "create the table, then restart."
            ),
        )
        return None
    if row is None:
        return None
    try:
        await db.execute(
            text("UPDATE api_keys SET last_used = NOW() WHERE key_hash = :h"),
            {"h": key_hash},
        )
    except Exception:
        # last_used is telemetry, not authorization — but a failed UPDATE still
        # aborts the request's transaction, so the endpoint's own queries would
        # 500 on a purely cosmetic write. Roll back and carry on authenticated.
        try:
            await db.rollback()
        except Exception:
            pass
    return {
        "sub": f"api_key:{row[2] or 'unnamed'}",
        "auth_method": "api_key",
        "tenant_id": row[0],
        "role": row[1] or "user",
    }


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Security(_bearer_scheme),
    api_key: str | None = Header(None, alias="X-API-Key"),
    api_key_query: str | None = Query(
        None, alias="api_key", include_in_schema=False
    ),
    sse_ticket: str | None = Query(None, alias="ticket", include_in_schema=False),
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
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
    # --- SSE ticket (preferred for EventSource) ---
    # Single-use and ~60s, so a URL that leaks into a proxy log or browser
    # history is worthless. Checked first because it is the narrowest credential.
    if sse_ticket:
        principal = await redeem_sse_ticket(redis, sse_ticket.strip())
        if principal is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or already-used stream ticket",
            )
        return principal

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

        # Real key storage (§5.6 / P1.1): the tenant comes from the api_keys row,
        # never from anything the client supplied. This is what makes the
        # privacy-locked-tenant policy bind to API-key callers at all.
        principal = await _principal_from_api_key(db, effective_key)
        if principal is not None:
            return principal

        # Unknown key. Dev keeps the permissive MVP behaviour so a fresh
        # checkout still works; anywhere else this is a 401, so a deployment
        # that forgot to provision keys fails CLOSED.
        if _ALLOW_UNKNOWN_API_KEYS:
            log.debug("api_key_auth_unregistered", key_prefix=effective_key[:8] + "…")
            return {
                "sub": "api_key_user",
                "auth_method": "api_key",
                "tenant_id": "default",
                "api_key_registered": False,
            }
        log.warning("api_key_rejected", key_prefix=effective_key[:8] + "…")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unknown API key",
        )

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

    # Prefer X-Forwarded-For / X-Real-IP behind a reverse proxy so that all
    # anonymous traffic doesn't share one rate-limit bucket (§P7.17g).
    if current_user.get("sub"):
        ident = current_user["sub"]
    elif request.headers.get("x-forwarded-for"):
        ident = request.headers["x-forwarded-for"].split(",")[0].strip()
    elif request.headers.get("x-real-ip"):
        ident = request.headers["x-real-ip"]
    else:
        ident = request.client.host if request.client else "anon"
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


async def tenant_is_privacy_locked(db: AsyncSession, current_user: dict) -> bool:
    """Whether this principal's tenant is pinned to the local backend.

    **Fails closed**, by raising 503 rather than returning False: a privacy
    guarantee that evaporates when the database hiccups is not a guarantee (the
    reasoning §5.6 arrived at, preserved here now that two callers share it).
    """
    # `tenant_id` now originates from the api_keys row or from verified,
    # allowlisted JWT claims — never merged wholesale from a token body, which
    # used to let a self-signed token name any tenant it liked.
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
        log.error("tenant_policy_lookup_failed_denying", tenant_id=tenant_id, error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Cannot verify the tenant's LLM-backend policy right now, so the "
                "requested 'groq' backend is refused. Retry, or omit llm_backend "
                "to use the default local backend."
            ),
        ) from exc
    return bool(row and row[0])


async def resolve_llm_backend(
    db: AsyncSession,
    redis: Any,
    current_user: dict,
    options: dict | None,
) -> str:
    """Resolve the backend this job will actually run on, and enforce the lock.

    **This exists because the guarded knob was not the deciding knob**
    (PROJECT_ASSESSMENT §13.5). `check_llm_backend_policy` inspected
    ``options["llm_backend"]``, which no worker ever read: Stage 1 and Stage 2
    both resolve their backend from the global Redis key ``config:llm_backend``.
    So a privacy-locked tenant's posts went to Groq whenever that global toggle
    pointed there, while an explicit ``llm_backend: "groq"`` on the request still
    returned a 403 that read as the guarantee holding.

    The resolution order matches what the workers do — request > toggle > env —
    and the **resolved** value is what the caller stamps into the job envelope,
    so the decision is made once, here, where the tenant is known. The workers
    have no database and so cannot make it themselves.

    Two different outcomes for a locked tenant, deliberately:

    * they **asked** for groq → 403. It is their request and it is refusable.
    * the **toggle or the env default** says groq → silently pinned to local.
      The operator's global switch is not their choice, and the guarantee is
      "their content never leaves the local backend", not "they get an error".
      Raising here would take a locked tenant offline whenever anyone flipped
      the switch.
    """
    explicit = (options or {}).get("llm_backend")
    toggle = None
    try:
        raw = await redis.get(LLM_BACKEND_CONFIG_KEY)
        decoded = raw.decode() if isinstance(raw, (bytes, bytearray)) else raw
        if decoded in ("local", "groq"):
            toggle = decoded
    except Exception as exc:  # Redis down → fall through to the env default
        log.warning("llm_backend_toggle_read_failed", error=str(exc))

    resolved = explicit or toggle or config.llm_backend
    if resolved != "groq":
        return resolved

    if not await tenant_is_privacy_locked(db, current_user):
        return resolved

    tenant_id = current_user.get("tenant_id") or "default"
    if explicit == "groq":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"Tenant '{tenant_id}' is privacy-locked: llm_backend='groq' "
                "is not permitted (data must not leave the local backend)"
            ),
        )
    log.info(
        "privacy_locked_tenant_pinned_to_local",
        tenant_id=tenant_id,
        would_have_been=resolved,
        source="toggle" if toggle == "groq" else "env_default",
    )
    return "local"


async def check_llm_backend_policy(
    db: AsyncSession,
    current_user: dict,
    options: dict | None,
) -> None:
    """Reject a per-request ``llm_backend: "groq"`` override when the tenant
    is privacy-locked (tenant_policies.privacy_locked).

    **Scope:** this guards a backend the CALLER named. For work handed to the
    pipeline, use :func:`resolve_llm_backend` instead — the pipeline's backend
    comes from the global toggle, which this function never sees (§13.5).

    Raises:
        HTTPException 403 when the override violates the tenant policy.
    """
    requested = (options or {}).get("llm_backend")
    if requested != "groq":
        return
    # `tenant_id` now originates from the api_keys row or from verified,
    # allowlisted JWT claims — never merged wholesale from a token body, which
    # used to let a self-signed token name any tenant it liked.
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
        # FAIL CLOSED. This used to `return`, i.e. permit egress to Groq whenever
        # the policy table was unreachable — so the one failure mode where you
        # most want the guarantee to hold was exactly where it did not. A
        # privacy guarantee that evaporates when the database hiccups is not a
        # guarantee.
        log.error("tenant_policy_lookup_failed_denying", tenant_id=tenant_id, error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Cannot verify the tenant's LLM-backend policy right now, so the "
                "requested 'groq' backend is refused. Retry, or omit llm_backend "
                "to use the default local backend."
            ),
        ) from exc
    if row and row[0]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"Tenant '{tenant_id}' is privacy-locked: llm_backend='groq' "
                "is not permitted (data must not leave the local backend)"
            ),
        )

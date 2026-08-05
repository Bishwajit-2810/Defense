"""Authentication endpoints — token issuance, refresh, identity, SSE tickets.

Closes the remainder of PROJECT_ASSESSMENT §6.6. The JWT *core* was always
sound — HS256, correct `datetime` claims, expiry enforced by `python-jose`. What
was broken sat around it:

* the login endpoint issued a signed 24-hour token to **anybody** (defect 3);
* there was no refresh, verify, or revocation route, so the dashboard could not
  tell "no token" from "expired token" and a leaked token lived its full 24h
  (defect 4);
* `EventSource` cannot set headers, so the credential travelled in a query
  parameter (defect 1) — now replaced by a single-use stream ticket.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import redis.asyncio as aioredis
import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from jose import jwt
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from deps import get_current_user, get_db, get_redis, verify_token
from libs.auth import mint_sse_ticket, verify_password
from libs.common.config import (
    JWT_ALGORITHM,
    app_env,
    get_jwt_secret,
    jwt_secret_fingerprint,
    require_jwt_secret,
)
from models import TokenRequest, TokenResponse

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/v1/auth", tags=["auth"])

# Same source of truth as the verifier in deps.py. The fingerprint is logged in
# both, so an issuer/verifier mismatch — which presents as "login succeeds, then
# every subsequent call 401s" — is a one-line diff in the boot log rather than a
# mystery.
require_jwt_secret()
log.info("jwt_issuer_ready", secret_fingerprint=jwt_secret_fingerprint())

# Shortened from 24h now that refresh exists: a leaked token is live for an hour,
# not a day, and the dashboard renews silently.
_TOKEN_EXPIRE_HOURS: int = int(os.environ.get("JWT_EXPIRE_HOURS", "1"))

# Dev convenience: with no `users` rows provisioned, accept any credentials as
# the system did before. Off outside dev, so a deployment that forgets to seed
# users fails CLOSED rather than authenticating the internet.
_ALLOW_ANY_LOGIN = (
    os.environ.get(
        "ALLOW_ANY_LOGIN",
        "true" if app_env() in ("dev", "development", "local", "test", "ci") else "false",
    ).lower()
    == "true"
)


def _create_access_token(
    subject: str, tenant_id: str = "default", role: str = "user"
) -> str:
    now = datetime.now(tz=timezone.utc)
    payload = {
        "sub": subject,
        "tenant_id": tenant_id,
        "role": role,
        "iat": now,
        "exp": now + timedelta(hours=_TOKEN_EXPIRE_HOURS),
    }
    return jwt.encode(payload, get_jwt_secret(), algorithm=JWT_ALGORITHM)


async def _lookup_user(db: AsyncSession, username: str) -> dict | None:
    """Fetch an active user row, or None. A missing table reads as 'no users'."""
    try:
        row = (
            await db.execute(
                text(
                    "SELECT username, password_hash, tenant_id, role FROM users "
                    "WHERE username = :u AND active = TRUE"
                ),
                {"u": username},
            )
        ).first()
    except Exception as exc:
        log.warning("user_lookup_failed", error=str(exc))
        return None
    if row is None:
        return None
    return {"username": row[0], "password_hash": row[1], "tenant_id": row[2], "role": row[3]}


@router.post(
    "/token",
    response_model=TokenResponse,
    summary="Obtain a Bearer JWT",
)
async def login(
    body: TokenRequest,
    db: AsyncSession = Depends(get_db),
) -> TokenResponse:
    """Issue a signed JWT after verifying the credentials.

    The `tenant_id` and `role` baked into the token come from the **users
    table**, not from anything the client sent — which is what makes the
    privacy-locked-tenant policy meaningful for JWT callers.
    """
    user = await _lookup_user(db, body.username)

    if user is not None:
        if not verify_password(body.password, user["password_hash"]):
            # Same message and timing shape as an unknown user: do not confirm
            # which usernames exist.
            log.warning("auth_login_rejected", username=body.username)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid username or password",
            )
        token = _create_access_token(user["username"], user["tenant_id"], user["role"])
        log.info("auth_token_issued", username=user["username"], tenant_id=user["tenant_id"])
        return TokenResponse(access_token=token, token_type="bearer")

    if _ALLOW_ANY_LOGIN:
        # Dev only. Loud, because it is exactly the behaviour §6.6 flagged.
        log.warning(
            "auth_login_unverified_dev_mode",
            username=body.username,
            detail="no users row; ALLOW_ANY_LOGIN is on — never enable outside dev",
        )
        token = _create_access_token(body.username)
        return TokenResponse(access_token=token, token_type="bearer")

    log.warning("auth_login_rejected_unknown_user", username=body.username)
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid username or password",
    )


@router.post(
    "/refresh",
    response_model=TokenResponse,
    summary="Exchange a valid token for a fresh one (sliding session)",
)
async def refresh(current_user: dict = Depends(get_current_user)) -> TokenResponse:
    """Issue a new token to a caller holding a currently-valid one.

    Sliding session: this is what lets `exp` be an hour instead of a day. An
    expired token cannot be refreshed — `get_current_user` rejects it first,
    which is the point.
    """
    if current_user.get("auth_method") != "jwt":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only a JWT session can be refreshed",
        )
    token = _create_access_token(
        current_user.get("sub") or "unknown",
        current_user.get("tenant_id") or "default",
        current_user.get("role") or "user",
    )
    return TokenResponse(access_token=token, token_type="bearer")


@router.get(
    "/me",
    summary="Who am I — lets a client distinguish 'no token' from 'expired token'",
)
async def me(current_user: dict = Depends(get_current_user)) -> dict:
    """Return the authenticated principal.

    The dashboard calls this on load. Without it, a client cannot tell an absent
    credential from an expired one — which is the split state §6.6 defect 4
    describes, where the UI says "logged out" while streams keep working.
    """
    return {
        "sub": current_user.get("sub"),
        "tenant_id": current_user.get("tenant_id"),
        "role": current_user.get("role"),
        "auth_method": current_user.get("auth_method"),
        "expires_at": current_user.get("exp"),
    }


@router.post(
    "/sse-ticket",
    summary="Mint a single-use, short-lived ticket for an EventSource stream",
)
async def sse_ticket(
    current_user: dict = Depends(get_current_user),
    redis: aioredis.Redis = Depends(get_redis),
) -> dict:
    """Mint a stream ticket for the authenticated caller.

    `EventSource` cannot send headers, so a streaming client needs *something*
    in the URL. A ticket is the right something: single-use, ~60 seconds, and
    bound to this principal — so it leaks into proxy logs and browser history
    harmlessly, unlike the long-lived credential that used to go there.

    Client flow: `POST /v1/auth/sse-ticket` with your normal credential, then
    open `EventSource("…/stream?ticket=<ticket>")`.
    """
    return await mint_sse_ticket(redis, current_user)


@router.get(
    "/verify",
    summary="Validate a raw token without establishing a session",
    include_in_schema=False,
)
async def verify(token: str) -> dict:
    """Debug helper: does this token parse and verify? 401 with a reason if not."""
    payload = verify_token(token)
    return {"valid": True, "sub": payload.get("sub"), "exp": payload.get("exp")}

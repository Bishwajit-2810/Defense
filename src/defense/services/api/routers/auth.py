"""Authentication endpoints — signup, token issuance, refresh, identity, SSE tickets.

Registration (`POST /v1/auth/signup`) is what makes the rest of this module
reachable without a psql session: the dashboard could verify a password but
nothing could *create* one, so every deployment either seeded `users` by hand or
lived on the `ALLOW_ANY_LOGIN` dev path. `GET /v1/auth/config` tells the login
screen which of those it is looking at.


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
from defense.libs.common.config import get_settings

config = get_settings()
from datetime import datetime, timedelta, timezone

import redis.asyncio as aioredis
import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from jose import jwt
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from defense.services.api.deps import get_current_user, get_db, get_redis, verify_token
from defense.libs.auth import hash_password, mint_sse_ticket, verify_password
from defense.libs.common.config import (
    JWT_ALGORITHM,
    app_env,
    get_jwt_secret,
    jwt_secret_fingerprint,
    require_jwt_secret,
)
from defense.services.api.models import (
    MIN_PASSWORD_LENGTH,
    SignupRequest,
    SignupResponse,
    TokenRequest,
    TokenResponse,
)

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
_TOKEN_EXPIRE_HOURS: int = config.jwt_expire_hours

# Dev convenience: with no `users` rows provisioned, accept any credentials as
# the system did before. Off outside dev, so a deployment that forgets to seed
# users fails CLOSED rather than authenticating the internet.
_ALLOW_ANY_LOGIN = get_settings().allow_any_login.lower() == "true" if get_settings().allow_any_login else app_env() in ("dev", "development", "local", "test", "ci")

# Self-service signup. Open in dev so a fresh checkout can create an account from
# the dashboard; closed elsewhere, because "anyone on the internet can mint a
# tenant-scoped account" is not a default anybody should inherit by accident.
#
# The bootstrap exception below is what keeps that closed default usable: a
# deployment with an EMPTY users table always accepts one signup, so production
# gets its first admin from the login screen instead of from a psql session.
_ALLOW_SIGNUP = get_settings().allow_signup.lower() == "true" if get_settings().allow_signup else app_env() in ("dev", "development", "local", "test", "ci")

# Signups land here. Never taken from the request body — see SignupRequest: a
# client that picks its own tenant picks whose data it can read.
_SIGNUP_TENANT_ID = get_settings().signup_tenant_id


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
    """Fetch an active user row, or None. A missing table reads as 'no users'.

    The match is case-insensitive because signup stores usernames case-folded. A
    literal ``username = :u`` would mean someone who registered as "Alice" — and
    was stored as "alice" — fails this lookup on login, then falls through to
    either the ``ALLOW_ANY_LOGIN`` dev path (a token with no real password check)
    or a 401 on their own correct password. Comparing on ``lower()`` also still
    matches any mixed-case row seeded before signup existed.
    """
    try:
        row = (
            await db.execute(
                text(
                    "SELECT username, password_hash, tenant_id, role FROM users "
                    "WHERE lower(username) = :u AND active = TRUE"
                ),
                {"u": (username or "").strip().lower()},
            )
        ).first()
    except Exception as exc:
        log.warning("user_lookup_failed", error=str(exc))
        # Roll back before returning: a failed statement aborts the request's
        # Postgres transaction, so anything else this request runs on the same
        # session would fail with "current transaction is aborted" rather than
        # taking the degraded path this `return None` is here to enable.
        try:
            await db.rollback()
        except Exception:
            pass
        return None
    if row is None:
        return None
    return {"username": row[0], "password_hash": row[1], "tenant_id": row[2], "role": row[3]}


async def _count_users(db: AsyncSession) -> int | None:
    """How many users exist, or None when ``users`` is unreadable.

    The None case is a real state, not a paranoia branch: on a checkout where
    ``deploy/init-db.sql`` has not been run there is no table, and signup must say
    so plainly (503 with the fix) rather than 500 on a raw asyncpg error.
    """
    try:
        row = (await db.execute(text("SELECT COUNT(*) FROM users"))).first()
    except Exception as exc:
        log.warning("user_count_failed", error=str(exc))
        # Same reason as _lookup_user: a failed statement aborts this request's
        # transaction, so anything the handler runs next on this session fails
        # with "current transaction is aborted" instead of taking this path.
        try:
            await db.rollback()
        except Exception:
            pass
        return None
    return int(row[0]) if row else 0


@router.get(
    "/config",
    summary="What the login screen is allowed to offer (unauthenticated)",
)
async def auth_config(db: AsyncSession = Depends(get_db)) -> dict:
    """Advertise the auth policy so the UI can render honestly.

    Without this the dashboard has to *guess* whether to show a signup form, and
    a guess is wrong in both directions: offering registration on a deployment
    that refuses it, or hiding it on one that needs a first admin. The password
    rule comes from the same constant the request model enforces, so the hint the
    user reads cannot drift from the rule the server applies.

    Deliberately unauthenticated — a login screen has no credential yet — and
    deliberately thin: booleans and a length, no user list, no user count.
    """
    count = await _count_users(db)
    return {
        # `count is not None` is load-bearing: with no users table every signup
        # can only 503, so advertising it as available would put a form on screen
        # whose sole outcome is an error. `bootstrap` stays false for the same
        # reason — an unknown user count is not a promise of admin rights.
        "signup_enabled": count is not None and (_ALLOW_SIGNUP or count == 0),
        "bootstrap": count == 0,  # first account becomes admin
        "min_password_length": MIN_PASSWORD_LENGTH,
        "allow_any_login": _ALLOW_ANY_LOGIN,
        "users_table_ready": count is not None,
    }


@router.post(
    "/signup",
    response_model=SignupResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register a user and return a Bearer JWT",
)
async def signup(
    body: SignupRequest,
    db: AsyncSession = Depends(get_db),
) -> SignupResponse:
    """Create an account, then log it straight in.

    Three properties worth stating, because each is a way this endpoint could
    have quietly undermined the auth work it sits next to:

    * **The password is never stored.** Only ``pbkdf2_sha256$…`` from
      ``libs.auth.hash_password``, which is the same format ``/v1/auth/token``
      verifies against — so a signed-up user's login is a *real* password check,
      not the ``ALLOW_ANY_LOGIN`` dev path.
    * **Tenant and role are assigned here**, from server config, never from the
      body. The privacy-locked-tenant guarantee is keyed on ``tenant_id``; a
      self-chosen one would hand a caller any tenant it named.
    * **Duplicates lose the race, not the row.** ``ON CONFLICT DO NOTHING``
      means two concurrent signups for one username cannot overwrite an existing
      password hash — the second gets a 409, and the first account is untouched.
    """
    count = await _count_users(db)
    if count is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "The users table is unavailable, so accounts cannot be created. "
                "Run deploy/init-db.sql against the API's database."
            ),
        )

    is_bootstrap = count == 0
    if not _ALLOW_SIGNUP and not is_bootstrap:
        log.warning("signup_rejected_disabled", username=body.username)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Self-service signup is disabled on this deployment. Ask an "
                "administrator for an account or an API key."
            ),
        )

    # The first account administers the deployment; everyone after is a plain
    # user. Two simultaneous first signups could both read count == 0 and both
    # land as admin — a one-request window on an empty database, which is a
    # better trade than making the bootstrap path require a pre-seeded row.
    role = "admin" if is_bootstrap else "user"

    try:
        result = await db.execute(
            text(
                "INSERT INTO users (username, password_hash, tenant_id, role, active) "
                "VALUES (:u, :h, :t, :r, TRUE) "
                "ON CONFLICT (username) DO NOTHING"
            ),
            {
                "u": body.username,
                "h": hash_password(body.password),
                "t": _SIGNUP_TENANT_ID,
                "r": role,
            },
        )
    except Exception as exc:
        log.error("signup_insert_failed", username=body.username, error=str(exc))
        try:
            await db.rollback()
        except Exception:
            pass
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Could not create the account right now. Try again.",
        ) from exc

    if result.rowcount == 0:
        # Taken. This is one of the few places where confirming a username exists
        # is unavoidable — a registration form cannot function otherwise — so it
        # is stated once here and nowhere in the login path, which stays silent.
        log.info("signup_rejected_duplicate", username=body.username)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Username '{body.username}' is already taken",
        )

    # Commit before handing back a token: the token asserts an account exists, so
    # the row must be durable first, not merely pending on this session.
    await db.commit()

    log.info(
        "signup_succeeded",
        username=body.username,
        tenant_id=_SIGNUP_TENANT_ID,
        role=role,
        bootstrap=is_bootstrap,
    )
    return SignupResponse(
        access_token=_create_access_token(body.username, _SIGNUP_TENANT_ID, role),
        token_type="bearer",
        username=body.username,
        tenant_id=_SIGNUP_TENANT_ID,
        role=role,
    )


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

    if _ALLOW_ANY_LOGIN and await _count_users(db) == 0:
        # Dev only, and only while the deployment has NO accounts at all.
        #
        # That second condition is new, and it is what makes signup mean anything
        # in dev. The flag exists for "no users provisioned yet"; the check was
        # "no row for *this* username", so once one account existed, anybody could
        # still log in as any other name and receive a signed token. Now the
        # escape hatch closes the moment a real account exists.
        log.warning(
            "auth_login_unverified_dev_mode",
            username=body.username,
            detail="empty users table; ALLOW_ANY_LOGIN is on — never enable outside dev",
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

"""Authentication endpoints — token issuance."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import structlog
from fastapi import APIRouter
from jose import jwt

from models import TokenRequest, TokenResponse

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/v1/auth", tags=["auth"])

_JWT_SECRET: str = os.environ.get("JWT_SECRET", "change-me")
_JWT_ALGORITHM: str = "HS256"
_TOKEN_EXPIRE_HOURS: int = 24


def _create_access_token(subject: str) -> str:
    now = datetime.now(tz=timezone.utc)
    payload = {
        "sub": subject,
        "iat": now,
        "exp": now + timedelta(hours=_TOKEN_EXPIRE_HOURS),
    }
    return jwt.encode(payload, _JWT_SECRET, algorithm=_JWT_ALGORITHM)


@router.post(
    "/token",
    response_model=TokenResponse,
    summary="Obtain a Bearer JWT",
)
async def login(body: TokenRequest) -> TokenResponse:
    """Issue a signed JWT for the given credentials.

    MVP: accepts any username/password pair.
    Phase 2 will verify against the users table.
    """
    log.info("auth_token_issued", username=body.username)
    token = _create_access_token(subject=body.username)
    return TokenResponse(access_token=token, token_type="bearer")

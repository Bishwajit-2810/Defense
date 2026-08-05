"""Authentication endpoints — token issuance."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import structlog
from fastapi import APIRouter
from jose import jwt

from libs.common.config import (
    JWT_ALGORITHM,
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

_TOKEN_EXPIRE_HOURS: int = int(os.environ.get("JWT_EXPIRE_HOURS", "24"))


def _create_access_token(subject: str) -> str:
    now = datetime.now(tz=timezone.utc)
    payload = {
        "sub": subject,
        "iat": now,
        "exp": now + timedelta(hours=_TOKEN_EXPIRE_HOURS),
    }
    return jwt.encode(payload, get_jwt_secret(), algorithm=JWT_ALGORITHM)


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

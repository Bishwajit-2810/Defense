"""Regression tests (§6.6): a token must be verified as a token on EVERY transport.

Three defects, all with the same shape — a credential the server declined to
look at:

  1. ``EventSource`` cannot set headers, so the dashboard appends the session
     credential as ``?api_key=``. Server-side that was treated as an opaque API
     key and accepted for being non-empty, so an **expired** token and a token
     signed with the **wrong secret** both authenticated on all four SSE
     streams. Expiry was unenforceable there, and the principal collapsed to the
     shared ``api_key_user`` — losing tenant claims and bucketing every stream
     into one rate-limit key.
  2. ``get_current_user`` built ``{"sub": ..., "auth_method": "jwt", **payload}``
     — the spread came LAST, so any claim in the token won. ``auth_method`` is
     how downstream code tells a human session from a service call, and it was
     client-writable, as was ``tenant_id``.
  3. The secret had three independent declarations, each captured at import.

The API-key path has since been closed too (§5.6 / P1.1): keys are looked up by
hash in ``api_keys`` and the tenant comes from that row, never from anything the
client sent. An unknown key still falls through to the permissive MVP behaviour
**in dev only** — see ``tests/test_auth_hardening.py`` for that boundary.
"""

import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, '/home/bk/code/defense/src/defense')
sys.path.insert(0, '/home/bk/code/defense/src/defense/services/api')

import pytest
from fastapi import HTTPException
from jose import jwt

from libs.common.config import (  # noqa: E402
    JWT_ALGORITHM,
    JWT_DEV_DEFAULT_SECRET,
    get_jwt_secret,
    jwt_secret_fingerprint,
    require_jwt_secret,
)
from defense.services.api.deps import _looks_like_jwt, get_current_user  # noqa: E402


class _NoRowsDb:
    """DB stub: no api_keys row, no users row."""

    async def execute(self, *args, **kwargs):
        class _R:
            def first(self_inner):
                return None
        return _R()


class _NoTicketRedis:
    """Redis stub with no stored SSE tickets."""

    async def get(self, key):
        return None

    async def delete(self, key):
        return 0

    async def set(self, key, value, ex=None):
        return True


async def _auth(**kwargs):
    """Call get_current_user with the dependency-injected args filled in."""
    kwargs.setdefault("credentials", None)
    kwargs.setdefault("api_key", None)
    kwargs.setdefault("api_key_query", None)
    kwargs.setdefault("sse_ticket", None)
    kwargs.setdefault("db", _NoRowsDb())
    kwargs.setdefault("redis", _NoTicketRedis())
    return await get_current_user(**kwargs)


def _token(secret=None, expires_in_hours=1, **claims) -> str:
    now = datetime.now(tz=timezone.utc)
    payload = {
        "sub": "alice",
        "iat": now,
        "exp": now + timedelta(hours=expires_in_hours),
        **claims,
    }
    return jwt.encode(payload, secret or get_jwt_secret(), algorithm=JWT_ALGORITHM)


# ---------------------------------------------------------------------------
# Defect 1 — the SSE transport
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_valid_token_via_query_param_authenticates_as_itself():
    """Not as the anonymous shared api_key_user — tenant claims must survive."""
    principal = await _auth(api_key_query=_token(tenant_id="acme"))
    assert principal["sub"] == "alice"
    assert principal["auth_method"] == "jwt"
    assert principal["tenant_id"] == "acme"


@pytest.mark.asyncio
async def test_expired_token_via_query_param_is_rejected():
    expired = _token(expires_in_hours=-48)
    with pytest.raises(HTTPException) as exc:
        await _auth(api_key_query=expired)
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_forged_token_via_query_param_is_rejected():
    forged = _token(secret="not-the-server-secret")
    with pytest.raises(HTTPException) as exc:
        await _auth(api_key_query=forged)
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_expired_token_via_api_key_header_is_rejected():
    """The X-API-Key header is the same hole as the query param."""
    with pytest.raises(HTTPException) as exc:
        await _auth(api_key=_token(expires_in_hours=-1))
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_plain_api_key_still_works():
    """A credential that is not JWT-shaped falls through to the MVP key path."""
    principal = await _auth(api_key_query="demo")
    assert principal["auth_method"] == "api_key"
    assert principal["sub"] == "api_key_user"


@pytest.mark.parametrize(
    "credential,expected",
    [
        ("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJhIn0.sig", True),
        ("demo", False),
        ("sk-live-abc123", False),
        ("a.b.c", False),        # three parts but not a JOSE header
        ("eyJhbGciOiJIUzI1NiJ9..sig", False),  # empty payload segment
        ("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJhIn0", False),  # only two parts
    ],
)
def test_looks_like_jwt(credential, expected):
    assert _looks_like_jwt(credential) is expected


# ---------------------------------------------------------------------------
# Defect 2 — claim precedence
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_client_cannot_set_auth_method_claim():
    """`auth_method` distinguishes a human session from a service call."""
    hostile = _token(auth_method="internal-service")
    principal = await _auth(api_key_query=hostile)
    assert principal["auth_method"] == "jwt"


@pytest.mark.asyncio
async def test_unlisted_claims_are_dropped_from_the_principal():
    hostile = _token(is_admin=True, privacy_locked=False, api_key="x")
    principal = await _auth(api_key_query=hostile)
    assert "is_admin" not in principal
    assert "privacy_locked" not in principal
    assert "api_key" not in principal


# ---------------------------------------------------------------------------
# Defect 3 — one source of truth for the secret
# ---------------------------------------------------------------------------

def test_issuer_and_verifier_read_the_same_secret(monkeypatch):
    """Both sides read through libs.common.config, per call — so rotation works."""
    monkeypatch.setenv("JWT_SECRET", "rotated-secret")
    from defense.libs.common.config import get_settings
    get_settings.cache_clear()
    assert get_jwt_secret() == "rotated-secret"
    before = jwt_secret_fingerprint()
    monkeypatch.setenv("JWT_SECRET", "rotated-again")
    assert jwt_secret_fingerprint() != before


def test_fingerprint_never_leaks_the_secret(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "super-secret-value")
    fp = jwt_secret_fingerprint()
    assert "super-secret-value" not in fp
    assert len(fp) == 12


def test_placeholder_secret_is_refused_outside_dev(monkeypatch):
    monkeypatch.delenv("JWT_SECRET", raising=False)
    monkeypatch.setenv("APP_ENV", "production")
    from defense.libs.common.config import get_settings
    get_settings.cache_clear()
    with pytest.raises(RuntimeError, match="JWT_SECRET"):
        require_jwt_secret()


def test_placeholder_secret_is_tolerated_in_dev(monkeypatch):
    monkeypatch.delenv("JWT_SECRET", raising=False)
    monkeypatch.setenv("APP_ENV", "dev")
    from defense.libs.common.config import get_settings
    get_settings.cache_clear()
    assert require_jwt_secret() == JWT_DEV_DEFAULT_SECRET

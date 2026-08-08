"""Credential primitives — password hashing, API-key hashing, SSE tickets.

Pure functions plus small Redis/DB helpers, kept out of ``services/api/deps.py``
so they can be unit-tested without a FastAPI app.

Three things here close PROJECT_ASSESSMENT §5.6 and the remainder of §6.6:

* **API keys are looked up by hash**, and the tenant comes from that row — never
  from a client-supplied token body. Previously any non-empty key authenticated
  and carried no tenant, so every API-key caller resolved to tenant ``default``
  and the privacy-locked-tenant guarantee could not bind to them at all.
* **Passwords are verified**, so ``POST /v1/auth/token`` stops issuing a signed
  24-hour token to anybody who asks.
* **SSE tickets** replace the ``?api_key=`` bearer credential. ``EventSource``
  genuinely cannot send headers, so the query parameter existed for a reason —
  but a long-lived credential in a URL lands in proxy logs, browser history and
  Referer headers. A single-use, ~60-second, stream-scoped ticket does the same
  job without any of that.

Hashing uses ``hashlib.pbkdf2_hmac`` from the standard library: no new
dependency, and adequate for a system whose threat model is a capstone
deployment. A production system should prefer argon2/bcrypt via ``passlib``.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from defense.libs.common.config import get_settings

config = get_settings()
import secrets
from typing import Any

# ---------------------------------------------------------------------------
# API keys
# ---------------------------------------------------------------------------

def hash_api_key(raw: str) -> str:
    """SHA-256 of an API key, for lookup in ``api_keys.key_hash``.

    Plain SHA-256 (not a slow KDF) is deliberate here: API keys are
    high-entropy random strings we generate, not user-chosen passwords, so
    brute-forcing the hash is not the threat. Lookup happens on every request
    and must stay cheap.
    """
    return hashlib.sha256(raw.strip().encode("utf-8")).hexdigest()


def generate_api_key(prefix: str = "dk") -> tuple[str, str]:
    """Return ``(raw_key, key_hash)``. The raw key is shown once and not stored."""
    raw = f"{prefix}_{secrets.token_urlsafe(32)}"
    return raw, hash_api_key(raw)


# ---------------------------------------------------------------------------
# Passwords
# ---------------------------------------------------------------------------

_PBKDF2_ITERATIONS = 200_000
_ALGO = "pbkdf2_sha256"


def hash_password(password: str, *, iterations: int = _PBKDF2_ITERATIONS) -> str:
    """Hash a password as ``pbkdf2_sha256$iterations$salt_hex$hash_hex``."""
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"{_ALGO}${iterations}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Constant-time verification. Any malformed stored value verifies False.

    Never raises: a corrupt row must read as "wrong password", not as a 500 that
    tells an attacker the account exists.
    """
    try:
        algo, iterations, salt_hex, hash_hex = stored.split("$")
        if algo != _ALGO:
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iterations)
        )
    except (ValueError, AttributeError, TypeError):
        return False
    return hmac.compare_digest(digest.hex(), hash_hex)


# ---------------------------------------------------------------------------
# SSE tickets
# ---------------------------------------------------------------------------
# `EventSource` cannot set headers, so the dashboard appended its credential as
# `?api_key=…`. Even now that such a credential is verified as a JWT when it
# looks like one (§6.6 defect 1), putting a long-lived credential in a URL is
# wrong: URLs leak into proxy logs, browser history, and Referer headers.
#
# A ticket is single-use, short-lived, and bound to the principal that minted it.
# Redeeming deletes it, so a leaked URL is worthless within a minute and worthless
# immediately after first use.

_TICKET_PREFIX = "sse:ticket:"
_TICKET_TTL_SECONDS = config.sse_ticket_ttl


def _ticket_key(ticket: str) -> str:
    # Store the HASH, so a Redis dump does not hand over live tickets.
    return f"{_TICKET_PREFIX}{hashlib.sha256(ticket.encode()).hexdigest()}"


async def mint_sse_ticket(redis: Any, principal: dict, ttl: int | None = None) -> dict:
    """Create a single-use SSE ticket for ``principal``. Returns ticket + TTL."""
    import json  # noqa: PLC0415

    ticket = secrets.token_urlsafe(32)
    ttl = ttl or _TICKET_TTL_SECONDS
    payload = {
        "sub": principal.get("sub"),
        "tenant_id": principal.get("tenant_id"),
        "role": principal.get("role"),
        "auth_method": "sse_ticket",
    }
    await redis.set(_ticket_key(ticket), json.dumps(payload), ex=ttl)
    return {"ticket": ticket, "expires_in": ttl}


async def redeem_sse_ticket(redis: Any, ticket: str) -> dict | None:
    """Consume a ticket and return its principal, or None if invalid/expired.

    Deletes before returning, so a ticket cannot be replayed — even in the race
    between two concurrent connections, only one `delete` reports success.
    """
    import json  # noqa: PLC0415

    if not ticket:
        return None
    key = _ticket_key(ticket)
    try:
        # getdel ensures atomic read and delete.
        raw = await redis.getdel(key)
        if raw is None:
            return None
    except Exception:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


__all__ = [
    "generate_api_key",
    "hash_api_key",
    "hash_password",
    "mint_sse_ticket",
    "redeem_sse_ticket",
    "verify_password",
]

"""Tests for §5.6 / P1.1 and the §6.6 remainder — the parts that fail CLOSED.

§5.6 called the local⇄Groq privacy policy "the best design decision in the
project" and then showed it was unenforceable:

  * any non-empty API key authenticated and carried **no tenant**, so
    ``check_llm_backend_policy`` resolved every API-key caller to tenant
    ``"default"`` — a tenant with no policy row, i.e. no lock;
  * ``tenant_id`` was merged wholesale from the JWT body, so a self-signed token
    could name any tenant;
  * and the policy check ``return``-ed on any DB error, i.e. **permitted egress
    to Groq precisely when it could not verify the policy**.

The last one is the important one: a privacy guarantee that evaporates when the
database hiccups is not a guarantee. These tests pin the closed behaviour.
"""

import sys

sys.path.insert(0, "/home/bk/code/defense")
sys.path.insert(0, "/home/bk/code/defense/services/api")

import pytest
from fastapi import HTTPException

from libs.auth import (
    generate_api_key,
    hash_api_key,
    hash_password,
    mint_sse_ticket,
    redeem_sse_ticket,
    verify_password,
)
from deps import _principal_from_api_key, check_llm_backend_policy, get_current_user


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_api_key_lookup():
    """`deps._API_KEY_TABLE_USABLE` latches false on the first lookup failure so
    a missing table costs one log line rather than one per request. That makes it
    process-global state a test can poison for later tests, so reset it here."""
    import deps

    deps._API_KEY_TABLE_USABLE = True
    yield
    deps._API_KEY_TABLE_USABLE = True

class _Db:
    """Returns a preset row for the first query, then None."""

    def __init__(self, row=None, raises=False):
        self._row = row
        self._raises = raises
        self.queries: list[str] = []

    async def execute(self, stmt, params=None):
        self.queries.append(str(stmt))
        if self._raises:
            raise RuntimeError("database is down")
        row, self._row = self._row, None

        class _R:
            def first(self_inner):
                return row

        return _R()


class _Redis:
    def __init__(self):
        self.store: dict[str, str] = {}

    async def set(self, key, value, ex=None):
        self.store[key] = value
        return True

    async def get(self, key):
        return self.store.get(key)

    async def delete(self, key):
        return 1 if self.store.pop(key, None) is not None else 0


# ---------------------------------------------------------------------------
# The policy check fails CLOSED
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_groq_is_refused_when_the_policy_cannot_be_read():
    """The defect: this used to `return`, permitting egress on a DB error."""
    with pytest.raises(HTTPException) as exc:
        await check_llm_backend_policy(
            _Db(raises=True), {"tenant_id": "acme"}, {"llm_backend": "groq"}
        )
    assert exc.value.status_code == 503
    assert "policy" in exc.value.detail.lower()


@pytest.mark.asyncio
async def test_privacy_locked_tenant_is_refused_groq():
    with pytest.raises(HTTPException) as exc:
        await check_llm_backend_policy(
            _Db(row=(True,)), {"tenant_id": "locked"}, {"llm_backend": "groq"}
        )
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_unlocked_tenant_may_use_groq():
    await check_llm_backend_policy(
        _Db(row=(False,)), {"tenant_id": "open"}, {"llm_backend": "groq"}
    )


@pytest.mark.asyncio
async def test_local_backend_never_consults_the_policy():
    """No egress, no question to ask — and a DB outage must not block local runs."""
    db = _Db(raises=True)
    await check_llm_backend_policy(db, {"tenant_id": "acme"}, {"llm_backend": "local"})
    await check_llm_backend_policy(db, {"tenant_id": "acme"}, {})
    assert db.queries == []


# ---------------------------------------------------------------------------
# API keys: hashed storage, tenant from the row
# ---------------------------------------------------------------------------

def test_generated_key_is_high_entropy_and_its_hash_matches():
    raw, digest = generate_api_key()
    assert len(raw) > 32
    assert hash_api_key(raw) == digest
    assert raw not in digest      # the raw key is not recoverable from the hash


@pytest.mark.asyncio
async def test_tenant_comes_from_the_api_keys_row_not_the_client():
    db = _Db(row=("acme", "admin", "ci-runner"))
    principal = await _principal_from_api_key(db, "some-raw-key")
    assert principal["tenant_id"] == "acme"
    assert principal["role"] == "admin"
    assert principal["auth_method"] == "api_key"


@pytest.mark.asyncio
async def test_unknown_api_key_returns_none():
    assert await _principal_from_api_key(_Db(row=None), "nope") is None


@pytest.mark.asyncio
async def test_key_is_looked_up_by_hash_never_by_value():
    """A leaked database must not yield usable credentials."""
    db = _Db(row=("acme", "user", "k"))
    await _principal_from_api_key(db, "super-secret-key")
    assert all("super-secret-key" not in q for q in db.queries)


@pytest.mark.asyncio
async def test_unknown_key_is_rejected_when_the_mvp_fallback_is_off(monkeypatch):
    """Outside dev, a deployment that forgot to provision keys fails CLOSED."""
    import deps

    monkeypatch.setattr(deps, "_ALLOW_UNKNOWN_API_KEYS", False)
    with pytest.raises(HTTPException) as exc:
        await get_current_user(
            credentials=None, api_key="unregistered", api_key_query=None,
            sse_ticket=None, db=_Db(row=None), redis=_Redis(),
        )
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_registered_key_authenticates_with_its_tenant(monkeypatch):
    principal = await get_current_user(
        credentials=None, api_key="registered", api_key_query=None,
        sse_ticket=None, db=_Db(row=("acme", "user", "dash")), redis=_Redis(),
    )
    assert principal["tenant_id"] == "acme"


# ---------------------------------------------------------------------------
# Passwords
# ---------------------------------------------------------------------------

def test_password_round_trip():
    stored = hash_password("correct horse battery staple")
    assert verify_password("correct horse battery staple", stored)
    assert not verify_password("wrong", stored)


def test_password_hash_is_salted():
    """Two identical passwords must not produce identical hashes."""
    assert hash_password("same") != hash_password("same")


def test_password_hash_does_not_contain_the_password():
    assert "hunter2" not in hash_password("hunter2")


@pytest.mark.parametrize("stored", ["", "garbage", "pbkdf2_sha256$notanint$aa$bb", None])
def test_malformed_stored_hash_verifies_false_rather_than_raising(stored):
    """A corrupt row must read as 'wrong password', not as a 500."""
    assert verify_password("anything", stored) is False


# ---------------------------------------------------------------------------
# SSE tickets — single-use, short-lived, not a bearer credential in a URL
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ticket_round_trip_preserves_the_principal():
    redis = _Redis()
    issued = await mint_sse_ticket(redis, {"sub": "alice", "tenant_id": "acme", "role": "user"})
    principal = await redeem_sse_ticket(redis, issued["ticket"])
    assert principal["sub"] == "alice"
    assert principal["tenant_id"] == "acme"
    assert principal["auth_method"] == "sse_ticket"


@pytest.mark.asyncio
async def test_ticket_is_single_use():
    """A URL that leaks after the stream opened must already be worthless."""
    redis = _Redis()
    issued = await mint_sse_ticket(redis, {"sub": "alice"})
    assert await redeem_sse_ticket(redis, issued["ticket"]) is not None
    assert await redeem_sse_ticket(redis, issued["ticket"]) is None


@pytest.mark.asyncio
async def test_unknown_ticket_is_refused():
    assert await redeem_sse_ticket(_Redis(), "not-a-real-ticket") is None
    assert await redeem_sse_ticket(_Redis(), "") is None


@pytest.mark.asyncio
async def test_ticket_is_stored_hashed():
    """A Redis dump must not hand over live tickets."""
    redis = _Redis()
    issued = await mint_sse_ticket(redis, {"sub": "alice"})
    assert all(issued["ticket"] not in key for key in redis.store)


@pytest.mark.asyncio
async def test_ticket_authenticates_a_stream_request():
    redis = _Redis()
    issued = await mint_sse_ticket(redis, {"sub": "alice", "tenant_id": "acme"})
    principal = await get_current_user(
        credentials=None, api_key=None, api_key_query=None,
        sse_ticket=issued["ticket"], db=_Db(), redis=redis,
    )
    assert principal["sub"] == "alice"
    assert principal["tenant_id"] == "acme"


@pytest.mark.asyncio
async def test_bad_ticket_is_a_401_not_a_fallthrough():
    """A wrong ticket must not silently degrade to some other credential path."""
    with pytest.raises(HTTPException) as exc:
        await get_current_user(
            credentials=None, api_key=None, api_key_query=None,
            sse_ticket="bogus", db=_Db(), redis=_Redis(),
        )
    assert exc.value.status_code == 401

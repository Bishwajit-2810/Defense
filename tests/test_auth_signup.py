"""Signup tests — registration must not undo the auth work it sits next to.

`POST /v1/auth/signup` is the first endpoint that *writes* an identity, so it is
the first place where getting authorization wrong creates a durable privilege
rather than a rejected request. Four properties are load-bearing:

  1. **The password is never stored.** Only a `pbkdf2_sha256$…` digest, in the
     exact format `/v1/auth/token` verifies — otherwise signup would produce
     accounts that cannot log in, or (worse) plaintext in a table.
  2. **Tenant and role come from the server.** `tenant_id` is what the
     privacy-locked-tenant guarantee is keyed on and `role` is what admin checks
     read; a body that could set either would hand both away.
  3. **A duplicate loses, and the existing row is untouched.** `ON CONFLICT DO
     NOTHING` means a second signup for a taken username cannot overwrite the
     first account's password hash.
  4. **The dev escape hatch closes once accounts exist.** `ALLOW_ANY_LOGIN` is
     for an unprovisioned deployment; while it keyed off "no row for *this*
     username", one real account still left every other name loginable with any
     password.

The DB is stubbed rather than live: these are statements about policy, and a
stub makes the SQL the endpoint issues directly inspectable.
"""

import sys

sys.path.insert(0, "/home/bk/code/defense")
sys.path.insert(0, "/home/bk/code/defense/services/api")

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from libs.auth import hash_password, verify_password  # noqa: E402
from libs.common.config import JWT_ALGORITHM, get_jwt_secret  # noqa: E402
from jose import jwt  # noqa: E402

from models import MIN_PASSWORD_LENGTH, SignupRequest, TokenRequest  # noqa: E402
from routers import auth as auth_router  # noqa: E402


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


class _Result:
    def __init__(self, row=None, rowcount=1):
        self._row = row
        self.rowcount = rowcount

    def first(self):
        return self._row


class _FakeDb:
    """Minimal async session that records the statements it is handed.

    `user_count` drives `SELECT COUNT(*)`, `insert_rowcount` drives whether the
    INSERT claimed the username (0 = ON CONFLICT fired), and `user_row` is what a
    login lookup finds. Set `fail_on` to make one statement kind raise, which is
    how the "no users table" path is reached.
    """

    def __init__(self, user_count=0, insert_rowcount=1, user_row=None, fail_on=None):
        self.user_count = user_count
        self.insert_rowcount = insert_rowcount
        self.user_row = user_row
        self.fail_on = fail_on
        self.statements = []      # [(sql, params)]
        self.committed = False
        self.rolled_back = False

    async def execute(self, statement, params=None):
        sql = str(statement)
        self.statements.append((sql, params or {}))
        kind = (
            "count" if "COUNT(*)" in sql
            else "insert" if sql.strip().upper().startswith("INSERT")
            else "select"
        )
        if self.fail_on == kind:
            raise RuntimeError('relation "users" does not exist')
        if kind == "count":
            return _Result(row=(self.user_count,))
        if kind == "insert":
            return _Result(rowcount=self.insert_rowcount)
        return _Result(row=self.user_row)

    async def commit(self):
        self.committed = True

    async def rollback(self):
        self.rolled_back = True

    # -- helpers for assertions -------------------------------------------
    def insert_params(self):
        for sql, params in self.statements:
            if sql.strip().upper().startswith("INSERT"):
                return params
        raise AssertionError("no INSERT was issued")


def _signup_body(username="analyst", password="correct-horse-battery"):
    return SignupRequest(username=username, password=password)


async def _signup(db, **kwargs):
    return await auth_router.signup(_signup_body(**kwargs), db=db)


# ---------------------------------------------------------------------------
# 1 — the password is hashed, in the format login verifies
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_password_is_never_stored_in_plain_text():
    db = _FakeDb(user_count=3)
    await _signup(db, password="a-real-password")
    params = db.insert_params()
    assert "a-real-password" not in str(params.values())
    assert params["h"].startswith("pbkdf2_sha256$")


@pytest.mark.asyncio
async def test_stored_hash_verifies_against_the_login_path():
    """The digest signup writes must be one `/v1/auth/token` accepts."""
    db = _FakeDb(user_count=3)
    await _signup(db, password="a-real-password")
    stored = db.insert_params()["h"]
    assert verify_password("a-real-password", stored) is True
    assert verify_password("not-the-password", stored) is False


@pytest.mark.asyncio
async def test_two_signups_of_the_same_password_get_different_hashes():
    """Per-user salt: identical passwords must not produce identical rows."""
    db_a, db_b = _FakeDb(user_count=1), _FakeDb(user_count=1)
    await _signup(db_a, username="ann", password="same-password-here")
    await _signup(db_b, username="bob", password="same-password-here")
    assert db_a.insert_params()["h"] != db_b.insert_params()["h"]


# ---------------------------------------------------------------------------
# 2 — tenant and role are the server's decision
# ---------------------------------------------------------------------------


def test_signup_request_has_no_tenant_or_role_field():
    """A client that names its own tenant names whose data it can read."""
    assert "tenant_id" not in SignupRequest.model_fields
    assert "role" not in SignupRequest.model_fields


def test_extra_body_fields_do_not_become_attributes():
    body = SignupRequest.model_validate(
        {
            "username": "attacker",
            "password": "password-long-enough",
            "tenant_id": "victim-tenant",
            "role": "admin",
        }
    )
    assert getattr(body, "tenant_id", None) is None
    assert getattr(body, "role", None) is None


@pytest.mark.asyncio
async def test_tenant_comes_from_server_config(monkeypatch):
    monkeypatch.setattr(auth_router, "_SIGNUP_TENANT_ID", "acme")
    db = _FakeDb(user_count=5)
    resp = await _signup(db)
    assert db.insert_params()["t"] == "acme"
    assert resp.tenant_id == "acme"


@pytest.mark.asyncio
async def test_first_account_is_admin_and_later_ones_are_not():
    first = _FakeDb(user_count=0)
    resp_first = await _signup(first)
    assert first.insert_params()["r"] == "admin"
    assert resp_first.role == "admin"

    later = _FakeDb(user_count=1)
    resp_later = await _signup(later)
    assert later.insert_params()["r"] == "user"
    assert resp_later.role == "user"


@pytest.mark.asyncio
async def test_returned_token_carries_the_assigned_tenant_and_role(monkeypatch):
    monkeypatch.setattr(auth_router, "_SIGNUP_TENANT_ID", "acme")
    resp = await _signup(_FakeDb(user_count=0))
    claims = jwt.decode(resp.access_token, get_jwt_secret(), algorithms=[JWT_ALGORITHM])
    assert claims["sub"] == "analyst"
    assert claims["tenant_id"] == "acme"
    assert claims["role"] == "admin"


# ---------------------------------------------------------------------------
# 3 — duplicates and durability
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_duplicate_username_is_409_and_does_not_overwrite():
    db = _FakeDb(user_count=2, insert_rowcount=0)   # ON CONFLICT DO NOTHING fired
    with pytest.raises(HTTPException) as exc:
        await _signup(db)
    assert exc.value.status_code == 409
    assert db.committed is False


@pytest.mark.asyncio
async def test_insert_uses_on_conflict_do_nothing():
    """The guard against a concurrent signup clobbering an existing hash."""
    db = _FakeDb(user_count=2)
    await _signup(db)
    sql = next(s for s, _ in db.statements if s.strip().upper().startswith("INSERT"))
    assert "ON CONFLICT" in sql.upper()
    assert "DO NOTHING" in sql.upper()


@pytest.mark.asyncio
async def test_row_is_committed_before_a_token_is_returned():
    """The token asserts the account exists, so the row must be durable first."""
    db = _FakeDb(user_count=0)
    resp = await _signup(db)
    assert db.committed is True
    assert resp.access_token


@pytest.mark.asyncio
async def test_missing_users_table_is_503_with_the_fix_in_the_message():
    db = _FakeDb(fail_on="count")
    with pytest.raises(HTTPException) as exc:
        await _signup(db)
    assert exc.value.status_code == 503
    assert "init-db.sql" in exc.value.detail
    assert db.rolled_back is True     # or the next statement dies "transaction is aborted"


@pytest.mark.asyncio
async def test_failed_insert_rolls_back_and_does_not_return_a_token():
    db = _FakeDb(user_count=1, fail_on="insert")
    with pytest.raises(HTTPException) as exc:
        await _signup(db)
    assert exc.value.status_code == 503
    assert db.rolled_back is True
    assert db.committed is False


# ---------------------------------------------------------------------------
# Signup gating — closed by default outside dev, with a bootstrap exception
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_signup_disabled_is_403_when_accounts_already_exist(monkeypatch):
    monkeypatch.setattr(auth_router, "_ALLOW_SIGNUP", False)
    with pytest.raises(HTTPException) as exc:
        await _signup(_FakeDb(user_count=1))
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_signup_disabled_still_allows_the_first_admin(monkeypatch):
    """Otherwise a closed-by-default deployment can never create its first user."""
    monkeypatch.setattr(auth_router, "_ALLOW_SIGNUP", False)
    resp = await _signup(_FakeDb(user_count=0))
    assert resp.role == "admin"


@pytest.mark.asyncio
async def test_auth_config_reports_what_the_login_screen_may_offer(monkeypatch):
    monkeypatch.setattr(auth_router, "_ALLOW_SIGNUP", False)

    empty = await auth_router.auth_config(db=_FakeDb(user_count=0))
    assert empty["signup_enabled"] is True     # bootstrap
    assert empty["bootstrap"] is True
    assert empty["min_password_length"] == MIN_PASSWORD_LENGTH
    assert empty["users_table_ready"] is True

    populated = await auth_router.auth_config(db=_FakeDb(user_count=4))
    assert populated["signup_enabled"] is False
    assert populated["bootstrap"] is False


@pytest.mark.asyncio
async def test_auth_config_reports_an_unusable_users_table(monkeypatch):
    """Signup must not be advertised when every attempt can only 503."""
    monkeypatch.setattr(auth_router, "_ALLOW_SIGNUP", True)
    cfg = await auth_router.auth_config(db=_FakeDb(fail_on="count"))
    assert cfg["users_table_ready"] is False
    assert cfg["signup_enabled"] is False
    assert cfg["bootstrap"] is False    # unknown count is not a promise of admin


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "username",
    [
        "ab",                    # too short
        "a" * 65,                # too long
        "has space",
        "has/slash",
        "_leading",              # must start alphanumeric
        "",
        "semi;colon",
    ],
)
def test_invalid_usernames_are_rejected(username):
    with pytest.raises(ValidationError):
        SignupRequest(username=username, password="password-long-enough")


@pytest.mark.parametrize("username", ["abc", "analyst.one", "a_b-c", "user99"])
def test_valid_usernames_are_accepted(username):
    assert SignupRequest(username=username, password="password-long-enough").username == username


def test_username_is_case_folded():
    """`users.username` is the primary key — 'Alice' and 'alice' must not be two
    accounts, or whoever registers second gets the other's login prompt."""
    assert SignupRequest(username="  ALICE  ", password="password-long-enough").username == "alice"


def test_short_password_is_rejected():
    with pytest.raises(ValidationError):
        SignupRequest(username="analyst", password="x" * (MIN_PASSWORD_LENGTH - 1))


def test_password_at_the_minimum_is_accepted():
    body = SignupRequest(username="analyst", password="x" * MIN_PASSWORD_LENGTH)
    assert len(body.password) == MIN_PASSWORD_LENGTH


# ---------------------------------------------------------------------------
# 4 — login, after signup exists
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_signed_up_user_can_log_in_with_mixed_case_username():
    """Signup stores 'alice'; typing 'Alice' at the login prompt must still work.

    A literal `username = :u` lookup sent this user down the ALLOW_ANY_LOGIN path
    (a token with no password check) or gave them a 401 on their own correct
    password.
    """
    stored = hash_password("her-real-password")
    db = _FakeDb(user_row=("alice", stored, "acme", "analyst"))
    body = TokenRequest(username="Alice", password="her-real-password")
    resp = await auth_router.login(body, db=db)
    claims = jwt.decode(resp.access_token, get_jwt_secret(), algorithms=[JWT_ALGORITHM])
    assert claims["sub"] == "alice"
    assert claims["tenant_id"] == "acme"
    assert claims["role"] == "analyst"


@pytest.mark.asyncio
async def test_lookup_compares_case_insensitively():
    db = _FakeDb(user_row=("alice", hash_password("pw"), "default", "user"))
    await auth_router._lookup_user(db, "  ALICE ")
    sql, params = db.statements[0]
    assert "lower(username)" in sql
    assert params["u"] == "alice"


@pytest.mark.asyncio
async def test_wrong_password_is_401_for_a_real_account():
    db = _FakeDb(user_row=("alice", hash_password("her-real-password"), "acme", "user"))
    with pytest.raises(HTTPException) as exc:
        await auth_router.login(TokenRequest(username="alice", password="guess"), db=db)
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_dev_any_login_closes_once_an_account_exists(monkeypatch):
    """The escape hatch is for an unprovisioned deployment, not a populated one.

    It used to key off "no row for *this* username", so one real account still
    left every other name loginable with any password — in a mode that ships on
    by default in dev.
    """
    monkeypatch.setattr(auth_router, "_ALLOW_ANY_LOGIN", True)
    db = _FakeDb(user_count=1, user_row=None)   # someone has signed up
    with pytest.raises(HTTPException) as exc:
        await auth_router.login(TokenRequest(username="nobody", password="anything"), db=db)
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_dev_any_login_still_works_on_an_empty_database(monkeypatch):
    """A fresh checkout with no users must stay usable in dev."""
    monkeypatch.setattr(auth_router, "_ALLOW_ANY_LOGIN", True)
    db = _FakeDb(user_count=0, user_row=None)
    resp = await auth_router.login(TokenRequest(username="anyone", password="anything"), db=db)
    assert resp.access_token


@pytest.mark.asyncio
async def test_unknown_user_is_401_when_any_login_is_off(monkeypatch):
    monkeypatch.setattr(auth_router, "_ALLOW_ANY_LOGIN", False)
    db = _FakeDb(user_count=0, user_row=None)
    with pytest.raises(HTTPException) as exc:
        await auth_router.login(TokenRequest(username="nobody", password="anything"), db=db)
    assert exc.value.status_code == 401

"""Tests for persisted chat history (/v1/chat/conversations).

These run the router's real SQL against the Postgres testcontainer that
conftest brings up with ``deploy/init-db.sql`` applied — the ownership scoping
and the ON DELETE CASCADE are the whole point, and a mocked session would
verify neither.

The endpoint functions are called directly with explicit ``db``/``user``
arguments rather than through TestClient: FastAPI only resolves ``Depends``
defaults when it dispatches, so this exercises the same code with a real async
session and no event-loop juggling.
"""

import uuid

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

import defense.services.api.routers.chat_history as ch


@pytest_asyncio.fixture
async def db(postgres_container):
    engine = create_async_engine(postgres_container, echo=False)
    async with AsyncSession(engine) as session:
        yield session
    await engine.dispose()


def _user(username=None, tenant="default"):
    """A distinct owner per test, so tests never see each other's rows."""
    return {
        "username": username or f"u-{uuid.uuid4().hex[:12]}",
        "tenant_id": tenant,
        "sub": "ignored-when-username-present",
    }


def _turn(role, content, meta=None):
    return ch.ChatTurnIn(role=role, content=content, meta=meta)


# ---------------------------------------------------------------------------
# Round trip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_conversation_round_trip_preserves_provenance(db):
    """Reopening a conversation must bring the trace back, not just the prose."""
    user = _user()
    meta = {
        "mode": "agent",
        "agent": "toxicity",
        "tools": [{"tool_name": "top_posts", "mcp_server": "analytics-mcp", "status": "ok"}],
        "citations": ["cm0abcdef12345678901234"],
    }

    created = await ch.create_conversation(
        ch.ConversationCreate(
            messages=[_turn("user", "which posts got the most abuse?"),
                      _turn("assistant", "Here is the briefing.", meta)]
        ),
        db=db,
        user=user,
        _rl=None,
    )

    convo = await ch.get_conversation(created["id"], db=db, user=user)

    assert convo["title"] == "which posts got the most abuse?"
    assert [m["role"] for m in convo["messages"]] == ["user", "assistant"]
    assert convo["messages"][1]["meta"]["agent"] == "toxicity"
    assert convo["messages"][1]["meta"]["tools"][0]["mcp_server"] == "analytics-mcp"


@pytest.mark.asyncio
async def test_title_is_derived_from_the_first_user_turn(db):
    user = _user()
    created = await ch.create_conversation(
        ch.ConversationCreate(messages=[_turn("user", "  sentiment   trend  please ")]),
        db=db, user=user, _rl=None,
    )
    assert created["title"] == "sentiment trend please"


@pytest.mark.asyncio
async def test_long_title_is_truncated(db):
    user = _user()
    created = await ch.create_conversation(
        ch.ConversationCreate(messages=[_turn("user", "x" * 500)]), db=db, user=user, _rl=None
    )
    assert len(created["title"]) <= ch._TITLE_MAX
    assert created["title"].endswith("…")


@pytest.mark.asyncio
async def test_empty_conversation_gets_a_placeholder_title(db):
    user = _user()
    created = await ch.create_conversation(ch.ConversationCreate(), db=db, user=user, _rl=None)
    assert created["title"] == "New chat"


# ---------------------------------------------------------------------------
# Appending & listing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_appending_turns_bumps_the_conversation_up_the_list(db):
    user = _user()
    first = await ch.create_conversation(
        ch.ConversationCreate(messages=[_turn("user", "older thread")]), db=db, user=user, _rl=None
    )
    second = await ch.create_conversation(
        ch.ConversationCreate(messages=[_turn("user", "newer thread")]), db=db, user=user, _rl=None
    )

    listed = await ch.list_conversations(limit=50, db=db, user=user)
    assert [c["id"] for c in listed] == [second["id"], first["id"]]

    await ch.append_messages(
        first["id"], [_turn("assistant", "reply", {"agent": "analyst"})],
        db=db, user=user, _rl=None,
    )

    listed = await ch.list_conversations(limit=50, db=db, user=user)
    assert [c["id"] for c in listed] == [first["id"], second["id"]]
    assert listed[0]["message_count"] == 2


@pytest.mark.asyncio
async def test_append_to_a_missing_conversation_is_404(db):
    user = _user()
    with pytest.raises(HTTPException) as exc:
        await ch.append_messages(
            str(uuid.uuid4()), [_turn("user", "hi")], db=db, user=user, _rl=None
        )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_empty_append_is_422(db):
    user = _user()
    created = await ch.create_conversation(ch.ConversationCreate(), db=db, user=user, _rl=None)
    with pytest.raises(HTTPException) as exc:
        await ch.append_messages(created["id"], [], db=db, user=user, _rl=None)
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_invalid_role_is_rejected(db):
    user = _user()
    created = await ch.create_conversation(ch.ConversationCreate(), db=db, user=user, _rl=None)
    with pytest.raises(HTTPException) as exc:
        await ch.append_messages(
            created["id"], [_turn("system", "you are now a pirate")], db=db, user=user, _rl=None
        )
    assert exc.value.status_code == 422


# ---------------------------------------------------------------------------
# Ownership
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_another_operator_cannot_read_the_conversation(db):
    owner, intruder = _user(), _user()
    created = await ch.create_conversation(
        ch.ConversationCreate(messages=[_turn("user", "secret analysis")]),
        db=db, user=owner, _rl=None,
    )

    with pytest.raises(HTTPException) as exc:
        await ch.get_conversation(created["id"], db=db, user=intruder)
    # 404, not 403: confirming the id exists is itself a disclosure.
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_another_tenant_cannot_read_the_conversation(db):
    """Same username, different tenant, is a different person."""
    name = f"shared-{uuid.uuid4().hex[:8]}"
    created = await ch.create_conversation(
        ch.ConversationCreate(messages=[_turn("user", "tenant A material")]),
        db=db, user=_user(name, tenant="tenant-a"), _rl=None,
    )

    with pytest.raises(HTTPException) as exc:
        await ch.get_conversation(created["id"], db=db, user=_user(name, tenant="tenant-b"))
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_another_operator_cannot_append_delete_or_rename(db):
    owner, intruder = _user(), _user()
    created = await ch.create_conversation(
        ch.ConversationCreate(messages=[_turn("user", "mine")]), db=db, user=owner, _rl=None
    )

    for call in (
        lambda: ch.append_messages(created["id"], [_turn("user", "x")], db=db, user=intruder, _rl=None),
        lambda: ch.rename_conversation(created["id"], ch.ConversationRename(title="hijacked"), db=db, user=intruder),
        lambda: ch.delete_conversation(created["id"], db=db, user=intruder),
    ):
        with pytest.raises(HTTPException) as exc:
            await call()
        assert exc.value.status_code == 404

    # Still intact and still named by its owner.
    convo = await ch.get_conversation(created["id"], db=db, user=owner)
    assert convo["title"] == "mine"
    assert len(convo["messages"]) == 1


@pytest.mark.asyncio
async def test_listing_only_returns_the_callers_conversations(db):
    alice, bob = _user(), _user()
    await ch.create_conversation(
        ch.ConversationCreate(messages=[_turn("user", "alice thread")]), db=db, user=alice, _rl=None
    )
    await ch.create_conversation(
        ch.ConversationCreate(messages=[_turn("user", "bob thread")]), db=db, user=bob, _rl=None
    )

    assert [c["title"] for c in await ch.list_conversations(limit=50, db=db, user=alice)] == ["alice thread"]
    assert [c["title"] for c in await ch.list_conversations(limit=50, db=db, user=bob)] == ["bob thread"]


# ---------------------------------------------------------------------------
# Rename & delete
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rename(db):
    user = _user()
    created = await ch.create_conversation(
        ch.ConversationCreate(messages=[_turn("user", "original")]), db=db, user=user, _rl=None
    )
    await ch.rename_conversation(created["id"], ch.ConversationRename(title="  Quota protest  "), db=db, user=user)
    convo = await ch.get_conversation(created["id"], db=db, user=user)
    assert convo["title"] == "Quota protest"


@pytest.mark.asyncio
async def test_delete_removes_the_turns_too(db):
    """The FK cascade is what stops deleted chats leaving their messages behind."""
    from sqlalchemy import text

    user = _user()
    created = await ch.create_conversation(
        ch.ConversationCreate(messages=[_turn("user", "q"), _turn("assistant", "a")]),
        db=db, user=user, _rl=None,
    )

    before = (await db.execute(
        text("SELECT COUNT(*) FROM chat_messages WHERE conversation_id = :i"),
        {"i": created["id"]},
    )).scalar()
    assert before == 2

    await ch.delete_conversation(created["id"], db=db, user=user)

    after = (await db.execute(
        text("SELECT COUNT(*) FROM chat_messages WHERE conversation_id = :i"),
        {"i": created["id"]},
    )).scalar()
    assert after == 0


@pytest.mark.asyncio
async def test_clear_all_only_clears_the_caller(db):
    alice, bob = _user(), _user()
    for i in range(3):
        await ch.create_conversation(
            ch.ConversationCreate(messages=[_turn("user", f"alice {i}")]), db=db, user=alice, _rl=None
        )
    await ch.create_conversation(
        ch.ConversationCreate(messages=[_turn("user", "bob keeps this")]), db=db, user=bob, _rl=None
    )

    result = await ch.clear_conversations(db=db, user=alice)

    assert result["deleted"] == 3
    assert await ch.list_conversations(limit=50, db=db, user=alice) == []
    assert len(await ch.list_conversations(limit=50, db=db, user=bob)) == 1


@pytest.mark.asyncio
async def test_delete_missing_conversation_is_404(db):
    with pytest.raises(HTTPException) as exc:
        await ch.delete_conversation(str(uuid.uuid4()), db=db, user=_user())
    assert exc.value.status_code == 404


# ---------------------------------------------------------------------------
# Degradation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_table_reports_503_not_500():
    """A deployment without the migration still chats; it just doesn't save."""

    class _BrokenDb:
        async def execute(self, *a, **kw):
            raise RuntimeError('relation "chat_conversations" does not exist')

        async def rollback(self):
            pass

    with pytest.raises(HTTPException) as exc:
        await ch.list_conversations(limit=50, db=_BrokenDb(), user=_user())

    assert exc.value.status_code == 503
    assert "init-db.sql" in exc.value.detail

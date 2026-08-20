"""Chat history — persisted conversations for the dashboard's chat surface.

    GET    /v1/chat/conversations           → list (newest first)
    POST   /v1/chat/conversations           → create
    GET    /v1/chat/conversations/{id}      → one conversation with its turns
    POST   /v1/chat/conversations/{id}/messages → append a turn
    PATCH  /v1/chat/conversations/{id}      → rename
    DELETE /v1/chat/conversations/{id}      → delete (turns cascade)
    DELETE /v1/chat/conversations           → clear all of the caller's history

Ownership is ``(tenant_id, username)`` and it is applied in the WHERE clause of
every statement, not checked after the fact — a conversation id is a guessable
opaque string, and "SELECT by id, then compare the owner" is one forgotten
branch away from serving someone else's chat.

An assistant turn stores its ``meta`` verbatim: which agent answered, which MCP
servers and tools it called, its citations. That is what makes a reopened
conversation auditable rather than merely readable.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Optional

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from defense.services.api.deps import get_current_user, get_db, rate_limit

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/v1/chat/conversations", tags=["chat"])

# A title is a sidebar label, not a document. Long enough to tell two research
# threads apart, short enough not to wrap.
_TITLE_MAX = 80
_CONTENT_MAX = 100_000
_VALID_ROLES = ("user", "assistant")

# Anything above this and the sidebar is a scrolling wall; the caller can page.
_LIST_MAX = 200


class ChatTurnIn(BaseModel):
    role: str = Field(..., description='"user" or "assistant"')
    content: str = Field("", description="Turn text (an errored turn may be empty)")
    meta: Optional[dict[str, Any]] = Field(
        None, description="Provenance for an assistant turn: agent, tools, citations"
    )


class ConversationCreate(BaseModel):
    title: Optional[str] = None
    messages: Optional[list[ChatTurnIn]] = Field(
        None, description="Optional turns to seed the conversation with"
    )


class ConversationRename(BaseModel):
    title: str = Field(..., min_length=1)


def _owner(user: dict) -> dict:
    """The identity every statement is scoped by.

    ``sub`` is the JWT subject; API-key callers carry a username too. Falling
    back to the tenant keeps key-based callers working without letting them see
    another tenant's rows.
    """
    return {
        "tenant": user.get("tenant_id") or "default",
        "username": user.get("username") or user.get("sub") or "unknown",
    }


def _derive_title(messages: list[ChatTurnIn] | None) -> str:
    """Name a conversation after its opening question.

    An LLM would write a nicer title and cost a call per conversation on a
    surface where the first line is already the best summary anyone has.
    """
    for m in messages or []:
        if m.role == "user" and m.content.strip():
            first = " ".join(m.content.split())
            return first[:_TITLE_MAX - 1] + "…" if len(first) > _TITLE_MAX else first
    return "New chat"


def _validate(turn: ChatTurnIn) -> None:
    if turn.role not in _VALID_ROLES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"invalid role {turn.role!r}; use one of {list(_VALID_ROLES)}",
        )
    if len(turn.content) > _CONTENT_MAX:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"message too long (max {_CONTENT_MAX} characters)",
        )


def _unavailable(exc: Exception) -> HTTPException:
    """History is a convenience; a deployment without the table still chats.

    A 503 that names the cause is honest. A 500 stack trace would read as "chat
    is broken" when only its history is.
    """
    log.warning("chat_history_unavailable", error=str(exc))
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=(
            "Chat history is unavailable (has deploy/init-db.sql been applied?). "
            "Chatting still works; this conversation will not be saved."
        ),
    )


async def _insert_messages(
    db: AsyncSession, conversation_id: str, turns: list[ChatTurnIn]
) -> None:
    for turn in turns:
        _validate(turn)
        await db.execute(
            text(
                "INSERT INTO chat_messages (conversation_id, role, content, meta) "
                "VALUES (:c, :r, :t, CAST(:m AS JSONB))"
            ),
            {
                "c": conversation_id,
                "r": turn.role,
                "t": turn.content,
                "m": json.dumps(turn.meta) if turn.meta else None,
            },
        )


async def _touch(db: AsyncSession, conversation_id: str) -> None:
    await db.execute(
        text("UPDATE chat_conversations SET updated_at = NOW() WHERE id = :i"),
        {"i": conversation_id},
    )


async def _assert_owned(db: AsyncSession, conversation_id: str, user: dict) -> None:
    """404 — not 403 — when the caller does not own it: the existence of another
    operator's conversation is not information this endpoint should confirm."""
    owner = _owner(user)
    row = (
        await db.execute(
            text(
                "SELECT 1 FROM chat_conversations "
                "WHERE id = :i AND tenant_id = :t AND username = :u"
            ),
            {"i": conversation_id, **{"t": owner["tenant"], "u": owner["username"]}},
        )
    ).first()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found"
        )


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #


@router.get("", summary="List the caller's conversations, newest first")
async def list_conversations(
    limit: int = Query(50, ge=1, le=_LIST_MAX),
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(get_current_user),
) -> list[dict]:
    owner = _owner(user)
    try:
        rows = (
            await db.execute(
                text(
                    "SELECT c.id, c.title, c.created_at, c.updated_at, "
                    "       COUNT(m.id) AS message_count "
                    "FROM chat_conversations c "
                    "LEFT JOIN chat_messages m ON m.conversation_id = c.id "
                    "WHERE c.tenant_id = :t AND c.username = :u "
                    "GROUP BY c.id "
                    "ORDER BY c.updated_at DESC "
                    "LIMIT :lim"
                ),
                {"t": owner["tenant"], "u": owner["username"], "lim": limit},
            )
        ).fetchall()
    except Exception as exc:
        raise _unavailable(exc) from exc

    return [
        {
            "id": r[0],
            "title": r[1],
            "created_at": r[2].isoformat() if r[2] else None,
            "updated_at": r[3].isoformat() if r[3] else None,
            "message_count": r[4],
        }
        for r in rows
    ]


@router.post("", status_code=status.HTTP_201_CREATED, summary="Start a conversation")
async def create_conversation(
    body: ConversationCreate,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(get_current_user),
    _rl: None = Depends(rate_limit),
) -> dict:
    owner = _owner(user)
    conversation_id = str(uuid.uuid4())
    title = (body.title or "").strip() or _derive_title(body.messages)

    try:
        await db.execute(
            text(
                "INSERT INTO chat_conversations (id, tenant_id, username, title) "
                "VALUES (:i, :t, :u, :ti)"
            ),
            {
                "i": conversation_id,
                "t": owner["tenant"],
                "u": owner["username"],
                "ti": title[:_TITLE_MAX],
            },
        )
        if body.messages:
            await _insert_messages(db, conversation_id, body.messages)
        await db.commit()
    except HTTPException:
        await db.rollback()
        raise
    except Exception as exc:
        await db.rollback()
        raise _unavailable(exc) from exc

    return {"id": conversation_id, "title": title, "message_count": len(body.messages or [])}


@router.get("/{conversation_id}", summary="Read a conversation and its turns")
async def get_conversation(
    conversation_id: str,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(get_current_user),
) -> dict:
    owner = _owner(user)
    try:
        head = (
            await db.execute(
                text(
                    "SELECT id, title, created_at, updated_at FROM chat_conversations "
                    "WHERE id = :i AND tenant_id = :t AND username = :u"
                ),
                {"i": conversation_id, "t": owner["tenant"], "u": owner["username"]},
            )
        ).first()
        if head is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found"
            )
        rows = (
            await db.execute(
                text(
                    "SELECT role, content, meta, created_at FROM chat_messages "
                    "WHERE conversation_id = :i ORDER BY id ASC"
                ),
                {"i": conversation_id},
            )
        ).fetchall()
    except HTTPException:
        raise
    except Exception as exc:
        raise _unavailable(exc) from exc

    return {
        "id": head[0],
        "title": head[1],
        "created_at": head[2].isoformat() if head[2] else None,
        "updated_at": head[3].isoformat() if head[3] else None,
        "messages": [
            {
                "role": r[0],
                "content": r[1],
                # JSONB comes back parsed on asyncpg and as text on some drivers.
                "meta": json.loads(r[2]) if isinstance(r[2], str) else r[2],
                "created_at": r[3].isoformat() if r[3] else None,
            }
            for r in rows
        ],
    }


@router.post("/{conversation_id}/messages", summary="Append turns to a conversation")
async def append_messages(
    conversation_id: str,
    body: list[ChatTurnIn],
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(get_current_user),
    _rl: None = Depends(rate_limit),
) -> dict:
    if not body:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="provide at least one message",
        )
    try:
        await _assert_owned(db, conversation_id, user)
        await _insert_messages(db, conversation_id, body)
        await _touch(db, conversation_id)
        await db.commit()
    except HTTPException:
        await db.rollback()
        raise
    except Exception as exc:
        await db.rollback()
        raise _unavailable(exc) from exc

    return {"conversation_id": conversation_id, "appended": len(body)}


@router.patch("/{conversation_id}", summary="Rename a conversation")
async def rename_conversation(
    conversation_id: str,
    body: ConversationRename,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(get_current_user),
) -> dict:
    owner = _owner(user)
    title = body.title.strip()[:_TITLE_MAX]
    if not title:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="title cannot be blank"
        )
    try:
        result = await db.execute(
            text(
                "UPDATE chat_conversations SET title = :ti, updated_at = NOW() "
                "WHERE id = :i AND tenant_id = :t AND username = :u"
            ),
            {"ti": title, "i": conversation_id, "t": owner["tenant"], "u": owner["username"]},
        )
        await db.commit()
    except Exception as exc:
        await db.rollback()
        raise _unavailable(exc) from exc

    if result.rowcount == 0:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found"
        )
    return {"id": conversation_id, "title": title}


@router.delete("", summary="Clear all of the caller's chat history")
async def clear_conversations(
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(get_current_user),
) -> dict:
    owner = _owner(user)
    try:
        result = await db.execute(
            text(
                "DELETE FROM chat_conversations WHERE tenant_id = :t AND username = :u"
            ),
            {"t": owner["tenant"], "u": owner["username"]},
        )
        await db.commit()
    except Exception as exc:
        await db.rollback()
        raise _unavailable(exc) from exc
    return {"deleted": result.rowcount}


@router.delete("/{conversation_id}", summary="Delete a conversation")
async def delete_conversation(
    conversation_id: str,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(get_current_user),
) -> dict:
    owner = _owner(user)
    try:
        result = await db.execute(
            text(
                "DELETE FROM chat_conversations "
                "WHERE id = :i AND tenant_id = :t AND username = :u"
            ),
            {"i": conversation_id, "t": owner["tenant"], "u": owner["username"]},
        )
        await db.commit()
    except Exception as exc:
        await db.rollback()
        raise _unavailable(exc) from exc

    if result.rowcount == 0:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found"
        )
    return {"deleted": True, "id": conversation_id}

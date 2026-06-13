"""Dead-letter queue + bounded retry for Redis Stream consumers (architecture §8).

The workers consume with ``XREADGROUP ... >`` (new messages only), so a message
left un-ACKed on failure sits in the PEL forever — never retried, never
surfaced. This helper gives a real policy:

    on failure: re-enqueue (retry) up to ``max_retries``, then dead-letter to
    ``<stream>:dlq`` with the error context, and ACK the original either way so
    the PEL can't grow unbounded.

The retry counter rides inside the payload (``_dlq_attempts``) so it survives
re-enqueue (a re-enqueued message gets a fresh stream id, so a per-id counter
would reset and loop forever). Idempotent writes downstream (architecture §8)
make at-least-once retry safe.
"""

from __future__ import annotations

import json
from typing import Any

ATTEMPTS_FIELD = "_dlq_attempts"


def _norm(fields: dict | None) -> dict[str, str]:
    """Normalize a Redis stream entry's fields to str→str (bytes or str input)."""
    out: dict[str, str] = {}
    for k, v in (fields or {}).items():
        if isinstance(k, (bytes, bytearray)):
            k = k.decode()
        if isinstance(v, (bytes, bytearray)):
            v = v.decode()
        out[k] = v
    return out


def dlq_stream(stream: str) -> str:
    return f"{stream}:dlq"


async def record_failure(
    redis: Any,
    *,
    stream: str,
    group: str,
    msg_id: Any,
    fields: dict,
    error: Any,
    max_retries: int = 3,
    retry_stream: str | None = None,
) -> str:
    """Retry-by-re-enqueue up to ``max_retries``, then dead-letter.

    Returns ``"retried"`` or ``"dead_lettered"``. Always ACKs the original
    message (``stream``/``group``/``msg_id``).
    """
    payload = _norm(fields)
    attempts = int(payload.get(ATTEMPTS_FIELD, "0")) + 1

    if attempts < max_retries:
        payload[ATTEMPTS_FIELD] = str(attempts)
        await redis.xadd(retry_stream or stream, payload)
        await redis.xack(stream, group, msg_id)
        return "retried"

    await redis.xadd(
        dlq_stream(stream),
        {
            "data": payload.get("data") or json.dumps(payload, default=str),
            "error": str(error)[:1000],
            "orig_stream": stream,
            "attempts": str(attempts),
        },
    )
    await redis.xack(stream, group, msg_id)
    return "dead_lettered"


async def replay_dlq(redis: Any, stream: str, *, count: int = 100, trim: bool = True) -> int:
    """Re-enqueue dead-lettered messages back onto their origin stream.

    Reads up to ``count`` entries from ``<stream>:dlq``, re-adds each ``data``
    payload to ``orig_stream`` (fresh attempt counter), and (by default) trims
    them from the DLQ. Returns the number replayed.
    """
    dlq = dlq_stream(stream)
    entries = await redis.xrange(dlq, count=count)
    replayed = 0
    for entry_id, fields in entries:
        f = _norm(fields)
        target = f.get("orig_stream") or stream
        data = f.get("data")
        if data is None:
            continue
        await redis.xadd(target, {"data": data})
        replayed += 1
        if trim:
            await redis.xdel(dlq, entry_id)
    return replayed

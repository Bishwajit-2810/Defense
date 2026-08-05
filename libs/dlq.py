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

# Job counters live for a day, matching the TTL the API's producer sets on
# `job:{id}:total`.
_JOB_COUNTER_TTL = 86_400


def _job_id_from_payload(payload: dict[str, str]) -> str | None:
    """Best-effort extraction of the job id from a stream entry's `data` blob.

    Every stage's message carries `job_id` at the top level of `data`; a message
    that predates it, or one that failed to parse in the first place, simply has
    no job to account against.
    """
    raw = payload.get("data")
    if not raw:
        return None
    try:
        decoded = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(decoded, dict):
        return None
    job_id = decoded.get("job_id")
    return job_id if isinstance(job_id, str) and job_id else None


async def _count_against_job(redis: Any, payload: dict[str, str], error: Any) -> None:
    """Charge a dead-lettered post to its job's `failed` counter.

    ``job:{id}:total`` is written by the producer, but ``:completed``/``:failed``
    were incremented **only** by the assembler's ``_track_job_progress`` — and a
    post that dies in Stage 1, the router or Stage 2 never reaches the assembler.
    So ``completed + failed`` could never reach ``total``: the terminal ``done``
    event on ``analysis:progress:{job_id}`` never fired, the jobs row never
    reached a terminal status, and the dashboard progress bar sat at 49/50
    forever. One LLM timeout during a live demo produced exactly that.

    Counting here, at the point of no return, is what lets the job finish. Best
    effort throughout: a failure to account for a failure must not itself raise
    and cost the caller its ACK.
    """
    job_id = _job_id_from_payload(payload)
    if not job_id:
        return
    try:
        failed_n = await redis.incr(f"job:{job_id}:failed")
        await redis.expire(f"job:{job_id}:failed", _JOB_COUNTER_TTL)
        completed = int(await redis.get(f"job:{job_id}:completed") or 0)
        raw_total = await redis.get(f"job:{job_id}:total")
        total = int(raw_total) if raw_total else None
        finished = total is not None and (completed + int(failed_n)) >= total
        await redis.publish(
            f"analysis:progress:{job_id}",
            json.dumps(
                {
                    "event": "done" if finished else "progress",
                    "job_id": job_id,
                    "post_id": (json.loads(payload.get("data") or "{}") or {}).get("post_id"),
                    "completed": completed,
                    "failed": int(failed_n),
                    "total": total,
                    "error": str(error)[:500],
                    "dead_lettered": True,
                },
                ensure_ascii=False,
            ),
        )
    except Exception:
        # The DLQ write and the ACK matter more than the bookkeeping.
        return


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

    Dead-lettering also charges the post to its job's ``failed`` counter and
    publishes a progress event — see :func:`_count_against_job`. A retry does
    not: the post is still in flight and may yet succeed.
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
    # The post will never reach the assembler, so nothing else will ever count
    # it — without this the job can never reach a terminal state.
    await _count_against_job(redis, payload, error)
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

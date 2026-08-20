"""Cooperative cancellation for analysis jobs.

A job is not a process — it is N envelopes sitting in the stage streams
(``ingestion`` → ``nlp:stage1:queue`` → ``router:queue`` → ``llm:stage2:queue``
→ ``assembler``; an analysis re-run enters at Stage 1 and so traverses four),
each picked up by whichever worker replica is free. There is nothing to kill: no
PID, no task handle, and no way to pull a message back out of a stream once
``XADD`` has accepted it.

So "Stop" is cooperative. The API sets one flag here, and the four workers ahead
of the assembler check it as they pick a message up, dropping the message instead
of doing the work. The cost of a stop is therefore bounded by the *single* post
already mid-flight in each stage, not by the rest of the batch — a 300-post job
stops after the handful currently being worked on, not after 300.

The assembler deliberately does **not** check: a post that reaches it has had
every model run on it already, so dropping it there would discard work that is
paid for either way. What the assembler does instead is refuse to write a
cancelled job's row back to ``running``/``done``.

Why a flag rather than draining the streams:

* **A consumer group's messages are not addressable by content.** Finding a
  job's envelopes would mean ``XRANGE``-ing four streams and re-writing them,
  racing every worker that is reading at the same time.
* **The flag outlives the job row.** Deleting a job has to stop it too, and the
  in-flight envelopes still name a ``job_id`` that no longer exists in Postgres.
  So the flag carries its own TTL and is deliberately *not* deleted along with
  the job's other Redis keys — see ``purge_job_keys``.

Checks are best-effort in the same sense as ``libs.progress``: a Redis failure
means the message gets processed, never that the pipeline breaks. A stop that
misses is recoverable; a stop that drops a post silently on a Redis hiccup is
not.
"""

from __future__ import annotations

from typing import Any

# Long enough that an envelope stuck behind a slow batch still sees the flag,
# and matching the TTL the assembler puts on the per-job progress counters.
CANCEL_TTL = 86_400

_CANCEL_KEY = "job:{job_id}:cancelled"

# Per-job Redis state that a delete should clear. The cancel flag is NOT in
# here on purpose: it is what keeps a deleted job's in-flight posts from being
# analysed, so it has to survive the delete and age out on its own TTL.
PURGEABLE_KEY_TEMPLATES: tuple[str, ...] = (
    "job:{job_id}:total",
    "job:{job_id}:completed",
    "job:{job_id}:failed",
    "job:{job_id}:stage_events",
    "job:{job_id}:stage_seq",
)


def cancel_key(job_id: str) -> str:
    return _CANCEL_KEY.format(job_id=job_id)


async def mark_cancelled(redis: Any, job_id: str, *, reason: str = "cancelled") -> bool:
    """Raise the stop flag for *job_id*. Returns True if it went out.

    The caller decides what to do on False — for the API that means reporting
    the failure rather than telling the operator the job stopped when the
    workers were never told.
    """
    if not job_id:
        return False
    try:
        await redis.set(cancel_key(job_id), reason, ex=CANCEL_TTL)
        return True
    except Exception:
        return False


async def is_cancelled(redis: Any, job_id: str | None) -> bool:
    """True if *job_id* has been stopped.

    Falsy ``job_id`` is not an error — posts XADDed by hand or replayed carry no
    job and can never be cancelled. A Redis failure reads as "not cancelled":
    processing one extra post is the safe direction to fail in.
    """
    if not job_id:
        return False
    try:
        return bool(await redis.exists(cancel_key(job_id)))
    except Exception:
        return False


async def clear_cancelled(redis: Any, job_id: str) -> bool:
    """Lower the stop flag for *job_id*. Returns True if it went out.

    Resume calls this BEFORE it re-enqueues anything: the workers drop any
    envelope whose job carries the flag, so re-enqueueing first would feed the
    remaining posts straight into the check that throws them away.

    It also revives any envelope from the stopped run that is still sitting in a
    stream. That is the intended reading of resume — finish this job — and it is
    harmless either way: a post is upserted by ``post_id``, so processing one
    twice overwrites its own result rather than duplicating it.
    """
    if not job_id:
        return False
    try:
        await redis.delete(cancel_key(job_id))
        return True
    except Exception:
        return False


async def purge_job_keys(redis: Any, job_id: str) -> int:
    """Delete a job's progress counters and trace buffer. Returns keys removed.

    Leaves the cancel flag alone (see ``PURGEABLE_KEY_TEMPLATES``).
    """
    if not job_id:
        return 0
    keys = [t.format(job_id=job_id) for t in PURGEABLE_KEY_TEMPLATES]
    try:
        return int(await redis.delete(*keys) or 0)
    except Exception:
        return 0

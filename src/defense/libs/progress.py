"""Per-stage progress events for the dashboard's live per-post trace.

The pipeline already reports two things: aggregate queue depth
(``/v1/pipeline/stats``, computed from Redis stream/group state) and a terminal
per-job event published by the assembler. Neither tells you *where one post is
right now* or *what a layer produced* — the stage boundaries are invisible from
outside, because a post's only trace is a message sitting in a stream.

This helper closes that gap. Each worker publishes one small event as it picks a
post up and another as it hands the post on, onto the same channel the job SSE
endpoint already relays:

    analysis:progress:{job_id}

``GET /v1/analysis/{job_id}/stream`` forwards any event whose ``event`` field is
not ``done``/``error`` verbatim, so these arrive at the browser with no API
change. The dashboard's Trace tab renders them in order, which is what makes
"Stage 1 finished, here is its output, Stage 2 started" observable rather than
inferred.

Design constraints:

* **Never fail the pipeline.** Publishing is best-effort and every call is
  wrapped; a Redis hiccup or a missing ``job_id`` degrades to no event, never to
  a dropped post. Progress reporting is not worth a retry.
* **Stay small.** ``detail`` is display-ready scalars, not the stage's full
  output — the canonical result is already fetchable from
  ``GET /v1/analysis/{post_id}`` once the post lands. Big payloads on a pub/sub
  channel would cost real throughput on a 100k-thread batch.
* **Replayable.** Pub/sub has no backlog, and the first stage event is published
  by ingestion *during* the POST that creates the job — before the browser can
  possibly hold the job id and subscribe. So every event is also appended to a
  capped list, ``job:{job_id}:stage_events``, which the SSE endpoint replays on
  connect. That makes the trace correct for a late subscriber and survives a page
  reload. Each event carries a monotonic ``seq`` so a client can drop the
  duplicate when a frame arrives both replayed and live.
* **Timestamped.** Every frame carries wall-clock ``ts``. Beyond the trace, this
  buffer is the only per-job record of *activity*: ``jobs.updated_at`` is written
  by the assembler alone, so it stays at the creation time for the entire minutes
  a post spends in Stage 1 and Stage 2, and an idleness check against it calls a
  perfectly healthy job stalled.
"""

from __future__ import annotations

import json
import time
from typing import Any

from defense.contracts.events import emit_pipeline_event, PostIngested, PostAnalyzed, PostRouted, PostEnriched, ResultAssembled

# Stage keys, in pipeline order. The dashboard renders its rail from this list,
# so keep it in sync with the stream chain in architecture.md §3.
STAGES: tuple[str, ...] = ("ingest", "stage1", "router", "stage2", "assembler")

# Truncation guard for anything free-form that lands in `detail` (rule reason
# strings, error text). Keeps a single frame comfortably small.
_MAX_STR = 300

# Replay buffer: enough for a multi-post job's worth of frames, expired so a
# finished job's trace does not linger in Redis.
_BUFFER_KEY = "job:{job_id}:stage_events"
_SEQ_KEY = "job:{job_id}:stage_seq"
_BUFFER_MAX = 400
_BUFFER_TTL = 3600


def buffer_key(job_id: str) -> str:
    return _BUFFER_KEY.format(job_id=job_id)


async def replay_events(redis: Any, job_id: str) -> list[dict]:
    """Return the buffered stage events for a job, oldest first.

    Used by the SSE endpoint to bring a newly-connected client up to date.
    Returns an empty list on any failure — a missing replay degrades the trace,
    it must not break the stream.
    """
    if not job_id:
        return []
    try:
        raw = await redis.lrange(buffer_key(job_id), 0, -1)
    except Exception:
        return []

    out: list[dict] = []
    for item in raw or []:
        if isinstance(item, (bytes, bytearray)):
            item = item.decode()
        try:
            out.append(json.loads(item))
        except (json.JSONDecodeError, TypeError):
            continue
    return out


def _clip(value: Any) -> Any:
    """Shrink a value to something safe to put on a pub/sub channel."""
    if isinstance(value, str):
        return value if len(value) <= _MAX_STR else value[:_MAX_STR] + "…"
    if isinstance(value, (list, tuple)):
        return [_clip(v) for v in value[:12]]
    if isinstance(value, dict):
        return {k: _clip(v) for k, v in list(value.items())[:24]}
    return value


def stage_event(
    stage: str,
    status: str,
    *,
    job_id: str | None,
    post_id: str | None,
    ms: float | None = None,
    detail: dict | None = None,
) -> dict:
    """Build a stage event. Separated from publishing so it is easy to test."""
    event: dict[str, Any] = {
        # Consumed by _sse_generator as the SSE event name. Deliberately not
        # "done" — that value terminates the job stream.
        "event": "stage",
        "stage": stage,
        "status": status,
        "job_id": job_id,
        "post_id": post_id,
        # Wall-clock, epoch seconds. `ms` is a duration; this is *when*, and the
        # difference matters outside the trace: the jobs row is only written by
        # the assembler, so a job whose post is legitimately spending four
        # minutes in Stage 2 has an `updated_at` frozen at creation and reads as
        # stalled. The newest frame is the job's real proof of life.
        "ts": round(time.time(), 3),
    }
    if ms is not None:
        event["ms"] = round(float(ms), 1)
    if detail:
        event["detail"] = {k: _clip(v) for k, v in detail.items()}
    return event


async def publish_stage(
    redis: Any,
    stage: str,
    status: str,
    *,
    job_id: str | None,
    post_id: str | None = None,
    ms: float | None = None,
    detail: dict | None = None,
    log: Any = None,
) -> bool:
    """Publish one stage event. Returns True if it went out.

    A falsy ``job_id`` is not an error: posts pushed straight onto a stream
    (replay, manual XADD) legitimately have no job to report against, and the
    pipeline must run identically for them. Such calls are simply skipped.
    """
    if not job_id:
        return False

    try:
        payload = stage_event(
            stage, status, job_id=job_id, post_id=post_id, ms=ms, detail=detail
        )

        # Monotonic per-job sequence: lets a client dedupe a frame it receives
        # both from the replay buffer and live.
        try:
            payload["seq"] = int(await redis.incr(_SEQ_KEY.format(job_id=job_id)))
            await redis.expire(_SEQ_KEY.format(job_id=job_id), _BUFFER_TTL)
        except Exception:
            payload["seq"] = None

        body = json.dumps(payload, ensure_ascii=False, default=str)

        key = buffer_key(job_id)
        await redis.rpush(key, body)
        await redis.ltrim(key, -_BUFFER_MAX, -1)
        await redis.expire(key, _BUFFER_TTL)

        await redis.publish(f"analysis:progress:{job_id}", body)

        # Event Sourcing - Dual Write to the global event log
        if post_id:
            try:
                if stage == "ingest":
                    await emit_pipeline_event(redis, PostIngested(post_id=post_id, job_id=job_id, hash=detail.get("hash", "") if detail else "", platform=detail.get("platform", "") if detail else ""))
                elif stage == "stage1":
                    await emit_pipeline_event(redis, PostAnalyzed(post_id=post_id, job_id=job_id, stage1_result=detail or {}))
                elif stage == "router":
                    await emit_pipeline_event(redis, PostRouted(post_id=post_id, job_id=job_id, use_llm=detail.get("use_llm", False) if detail else False, reasons=detail.get("reasons", []) if detail else []))
                elif stage == "stage2":
                    await emit_pipeline_event(redis, PostEnriched(post_id=post_id, job_id=job_id, stage2_result=detail or {}))
                elif stage == "assembler":
                    await emit_pipeline_event(redis, ResultAssembled(post_id=post_id, job_id=job_id, valid=True))
            except Exception:
                pass

        return True
    except Exception as exc:  # noqa: BLE001 — progress must never break the flow
        if log is not None:
            log.debug(
                "stage_progress_publish_failed",
                stage=stage,
                job_id=job_id,
                error=str(exc),
            )
        return False

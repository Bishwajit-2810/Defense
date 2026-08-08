"""Unit tests for libs/dlq.py — retry-then-dead-letter for stream consumers (§8)."""

import asyncio
import json
import sys

sys.path.insert(0, '/home/bk/code/defense/src/defense')

from libs.dlq import ATTEMPTS_FIELD, dlq_stream, record_failure, replay_dlq


class FakeRedis:
    """In-memory stand-in: streams as lists of (id, fields), plus the
    key/counter and pub/sub surface the job accounting uses."""

    def __init__(self, **initial_keys):
        self.streams: dict[str, list] = {}
        self.acked: list = []
        self.keys: dict[str, int] = dict(initial_keys)
        self.published: list[tuple[str, str]] = []
        self._seq = 0

    async def xadd(self, stream, fields):
        self._seq += 1
        eid = f"{self._seq}-0"
        self.streams.setdefault(stream, []).append((eid, dict(fields)))
        return eid

    async def xack(self, stream, group, msg_id):
        self.acked.append((stream, group, msg_id))

    async def xrange(self, stream, count=100):
        return list(self.streams.get(stream, []))[:count]

    async def xdel(self, stream, eid):
        self.streams[stream] = [(i, f) for (i, f) in self.streams.get(stream, []) if i != eid]

    async def incr(self, key):
        self.keys[key] = int(self.keys.get(key, 0)) + 1
        return self.keys[key]

    async def expire(self, key, ttl):
        return True

    async def get(self, key):
        return self.keys.get(key)

    async def publish(self, channel, message):
        self.published.append((channel, message))


def test_retry_then_dead_letter():
    r = FakeRedis()
    stream, group = "nlp:stage1:queue", "g"
    fields = {"data": '{"post_id":"p1"}'}

    # attempt 1 and 2 → retried (re-enqueued with incremented attempt count)
    out1 = asyncio.run(record_failure(r, stream=stream, group=group, msg_id="1-0",
                                      fields=fields, error="boom", max_retries=3))
    assert out1 == "retried"
    requeued = r.streams[stream][-1][1]
    assert requeued[ATTEMPTS_FIELD] == "1"

    out2 = asyncio.run(record_failure(r, stream=stream, group=group, msg_id="2-0",
                                      fields=requeued, error="boom", max_retries=3))
    assert out2 == "retried"
    requeued2 = r.streams[stream][-1][1]
    assert requeued2[ATTEMPTS_FIELD] == "2"

    # attempt 3 → dead-lettered
    out3 = asyncio.run(record_failure(r, stream=stream, group=group, msg_id="3-0",
                                      fields=requeued2, error="boom", max_retries=3))
    assert out3 == "dead_lettered"
    dlq = r.streams[dlq_stream(stream)]
    assert len(dlq) == 1
    assert dlq[0][1]["orig_stream"] == stream
    assert dlq[0][1]["error"] == "boom"
    # original always acked
    assert len(r.acked) == 3


# ---------------------------------------------------------------------------
# §5.7 / §9.7 — a post that dies before the assembler must not stall its job
# ---------------------------------------------------------------------------
# `job:{id}:total` is written by the producer, but `:completed`/`:failed` were
# incremented ONLY by the assembler's `_track_job_progress` — and Stage-1 /
# router / Stage-2 failures dead-letter without ever reaching the assembler. So
# `completed + failed` could never reach `total`: the terminal `done` event on
# `analysis:progress:{job_id}` never fired, the jobs row never reached a
# terminal status, and the dashboard progress bar sat at 49/50 indefinitely.
# One LLM timeout during a live demo produced exactly that.

def _dead_letter(redis, *, job_id="job-1", post_id="p1", completed=0, total=None):
    fields = {"data": json.dumps({"post_id": post_id, "job_id": job_id})}
    if completed:
        redis.keys[f"job:{job_id}:completed"] = completed
    if total is not None:
        redis.keys[f"job:{job_id}:total"] = total
    return asyncio.run(
        record_failure(
            redis, stream="llm:stage2:queue", group="g", msg_id="1-0",
            fields=fields, error="LLM timeout", max_retries=1,
        )
    )


def test_dead_letter_counts_against_the_job():
    r = FakeRedis()
    assert _dead_letter(r, total=50, completed=49) == "dead_lettered"
    assert r.keys["job:job-1:failed"] == 1


def test_dead_lettering_the_last_post_publishes_the_terminal_event():
    """49 completed + 1 dead-lettered out of 50 must finish the job."""
    r = FakeRedis()
    _dead_letter(r, total=50, completed=49)

    assert len(r.published) == 1
    channel, raw = r.published[0]
    assert channel == "analysis:progress:job-1"
    event = json.loads(raw)
    assert event["event"] == "done"
    assert (event["completed"], event["failed"], event["total"]) == (49, 1, 50)
    assert event["dead_lettered"] is True


def test_dead_lettering_a_middle_post_publishes_progress_not_done():
    r = FakeRedis()
    _dead_letter(r, total=50, completed=10)
    assert json.loads(r.published[0][1])["event"] == "progress"


def test_a_retry_does_not_count_against_the_job():
    """The post is still in flight and may yet succeed."""
    r = FakeRedis()
    fields = {"data": json.dumps({"post_id": "p1", "job_id": "job-1"})}
    out = asyncio.run(
        record_failure(r, stream="s", group="g", msg_id="1-0",
                       fields=fields, error="boom", max_retries=3)
    )
    assert out == "retried"
    assert "job:job-1:failed" not in r.keys
    assert r.published == []


def test_message_without_a_job_id_is_still_dead_lettered():
    """Ingest paths with no job attached must not break the DLQ."""
    r = FakeRedis()
    out = asyncio.run(
        record_failure(r, stream="s", group="g", msg_id="1-0",
                       fields={"data": '{"post_id":"p1"}'}, error="boom",
                       max_retries=1)
    )
    assert out == "dead_lettered"
    assert r.published == []


def test_unparseable_payload_does_not_break_dead_lettering():
    r = FakeRedis()
    out = asyncio.run(
        record_failure(r, stream="s", group="g", msg_id="1-0",
                       fields={"data": "not json at all"}, error="boom",
                       max_retries=1)
    )
    assert out == "dead_lettered"
    assert len(r.streams[dlq_stream("s")]) == 1


def test_replay_dlq_reenqueues_and_trims():
    r = FakeRedis()
    stream = "assembler:queue"
    asyncio.run(r.xadd(dlq_stream(stream), {"data": '{"x":1}', "orig_stream": stream}))
    asyncio.run(r.xadd(dlq_stream(stream), {"data": '{"x":2}', "orig_stream": stream}))

    n = asyncio.run(replay_dlq(r, stream))
    assert n == 2
    assert len(r.streams[stream]) == 2          # re-enqueued to origin
    assert len(r.streams[dlq_stream(stream)]) == 0  # trimmed

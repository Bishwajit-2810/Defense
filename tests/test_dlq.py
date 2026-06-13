"""Unit tests for libs/dlq.py — retry-then-dead-letter for stream consumers (§8)."""

import asyncio
import sys

sys.path.insert(0, '/home/bk/code/defense')

from libs.dlq import ATTEMPTS_FIELD, dlq_stream, record_failure, replay_dlq


class FakeRedis:
    """In-memory stand-in: streams as lists of (id, fields)."""

    def __init__(self):
        self.streams: dict[str, list] = {}
        self.acked: list = []
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


def test_replay_dlq_reenqueues_and_trims():
    r = FakeRedis()
    stream = "assembler:queue"
    asyncio.run(r.xadd(dlq_stream(stream), {"data": '{"x":1}', "orig_stream": stream}))
    asyncio.run(r.xadd(dlq_stream(stream), {"data": '{"x":2}', "orig_stream": stream}))

    n = asyncio.run(replay_dlq(r, stream))
    assert n == 2
    assert len(r.streams[stream]) == 2          # re-enqueued to origin
    assert len(r.streams[dlq_stream(stream)]) == 0  # trimmed

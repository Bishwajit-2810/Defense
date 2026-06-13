"""Unit tests for the pipeline live-flow stats (routers/pipeline.py)."""

import asyncio
import sys

sys.path.insert(0, '/home/bk/code/defense')
sys.path.insert(0, '/home/bk/code/defense/services/api')

from routers import pipeline as P


class _Res:
    def __init__(self, row):
        self._row = row

    def mappings(self):
        return self

    def first(self):
        return self._row


class FakeRedis:
    def __init__(self, lens=None, groups=None, kv=None):
        self.lens = lens or {}
        self.groups = groups or {}
        self.kv = kv or {}

    async def xlen(self, stream):
        return self.lens.get(stream, 0)

    async def xinfo_groups(self, stream):
        if stream not in self.groups:
            raise Exception("ERR no such key")
        return self.groups[stream]

    async def get(self, key):
        return self.kv.get(key)


class FakeDB:
    def __init__(self, n):
        self.n = n

    async def execute(self, sql):
        return _Res({"n": self.n})


def test_stage_stats_reads_lag_pending_dlq():
    r = FakeRedis(
        lens={"nlp:stage1:queue": 10, "nlp:stage1:queue:dlq": 2},
        groups={"nlp:stage1:queue": [{"name": "stage1-nlp-group", "pending": 3, "lag": 7}]},
    )
    s = asyncio.run(P._stage_stats(r, "nlp:stage1:queue", "stage1-nlp-group"))
    assert s == {"backlog": 7, "in_flight": 3, "dlq": 2, "total": 10}


def test_stage_stats_missing_stream_is_zero():
    s = asyncio.run(P._stage_stats(FakeRedis(), "router:queue", "router-workers"))
    assert s == {"backlog": 0, "in_flight": 0, "dlq": 0, "total": 0}


def test_collect_stats_shape_and_totals():
    r = FakeRedis(
        lens={
            "nlp:stage1:queue": 5, "nlp:stage1:queue:dlq": 1,
            "router:queue": 2,
            "llm:stage2:queue:dlq": 4,
        },
        groups={
            "nlp:stage1:queue": [{"name": "stage1-nlp-group", "pending": 2, "lag": 3}],
            "router:queue": [{"name": "router-workers", "pending": 1, "lag": 1}],
        },
        kv={"stats:total_processed": "100", "stats:llm_routed": "7"},
    )
    out = asyncio.run(P.collect_stats(r, FakeDB(42)))

    assert [s["key"] for s in out["stages"]] == ["ingestion", "stage1", "router", "stage2", "assembler"]
    assert out["completed"] == 42
    assert out["total_processed"] == 100
    assert out["llm_routed"] == 7
    # totals aggregate across stages: in_flight 2+1, backlog 3+1, dlq 1+4
    assert out["in_flight_total"] == 3
    assert out["backlog_total"] == 4
    assert out["dlq_total"] == 5

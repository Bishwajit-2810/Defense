"""Unit tests for ingestion near-duplicate reuse (architecture §3.2).

Uses asyncio.run inside sync tests so no pytest-asyncio plugin is required
(keeps the project's `uv sync --extra dev` + pytest invocation sufficient).
"""

import asyncio
import os
import sys

sys.path.insert(0, '/home/bk/code/defense/src/defense')
sys.path.insert(0, '/home/bk/code/defense/src/defense/services/ingestion')
os.environ.setdefault("MODEL_STUB_MODE", "true")

from services.ingestion.service import (
    NEAR_DUP_THRESHOLD,
    _fetch_source_analysis,
    _find_near_duplicate,
)


class _Mappings:
    def __init__(self, row):
        self._row = row

    def first(self):
        return self._row


class _Result:
    def __init__(self, row=None, rowcount=0):
        self._row = row
        self.rowcount = rowcount

    def mappings(self):
        return _Mappings(self._row)


class FakeSession:
    """Minimal async session stub returning a canned row / rowcount."""

    def __init__(self, row=None, rowcount=0):
        self._row = row
        self._rowcount = rowcount
        self.calls = []

    async def execute(self, sql, params=None):
        self.calls.append((str(sql), params))
        return _Result(self._row, self._rowcount)


def test_near_dup_hit_above_threshold():
    sess = FakeSession(row={"post_id": "src-1", "score": 0.991})
    out = asyncio.run(_find_near_duplicate(sess, "a duplicated caption", "camp-1"))
    assert out == ("src-1", 0.991)
    assert "campaign_id" in sess.calls[0][0]  # campaign-scoped query


def test_near_dup_below_threshold_is_ignored():
    sess = FakeSession(row={"post_id": "src-1", "score": NEAR_DUP_THRESHOLD - 0.1})
    assert asyncio.run(_find_near_duplicate(sess, "different enough", "camp-1")) is None


def test_near_dup_no_rows():
    sess = FakeSession(row=None)
    assert asyncio.run(_find_near_duplicate(sess, "nothing analyzed yet", None)) is None


def test_fetch_source_analysis_reads_the_stub_flag_explicitly():
    """The old row-copy left `embedding_is_stub` out of its INSERT column list,
    so a copy of an honestly-flagged stub row claimed to be a real semantic
    vector (§13.3). The flag is now selected and carried deliberately."""
    sess = FakeSession(row={
        "result": {"post_id": "src-1", "overall_sentiment": "negative"},
        "emb": "[0.1,0.2,0.3]",
        "is_stub": True,
    })
    out = asyncio.run(_fetch_source_analysis(sess, "src-1"))
    assert out["embedding_is_stub"] is True
    assert out["embedding"] == [0.1, 0.2, 0.3]
    assert out["result"]["overall_sentiment"] == "negative"
    assert "embedding_is_stub" in sess.calls[0][0]


def test_fetch_source_analysis_returns_none_when_there_is_nothing_to_reuse():
    assert asyncio.run(_fetch_source_analysis(FakeSession(row=None), "src")) is None

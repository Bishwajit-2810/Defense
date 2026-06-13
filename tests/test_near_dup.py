"""Unit tests for ingestion near-duplicate reuse (architecture §3.2).

Uses asyncio.run inside sync tests so no pytest-asyncio plugin is required
(keeps the project's `uv sync --extra dev` + pytest invocation sufficient).
"""

import asyncio
import os
import sys

sys.path.insert(0, '/home/bk/code/defense')
sys.path.insert(0, '/home/bk/code/defense/services/ingestion')
os.environ.setdefault("MODEL_STUB_MODE", "true")

from services.ingestion.service import (
    NEAR_DUP_THRESHOLD,
    _find_near_duplicate,
    _reuse_analysis,
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


def test_reuse_analysis_rowcount():
    assert asyncio.run(_reuse_analysis(FakeSession(rowcount=1), "src", "new")) is True
    assert asyncio.run(_reuse_analysis(FakeSession(rowcount=0), "src", "new")) is False

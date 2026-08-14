"""Comment analysis is not gated by the post-level router.

The whole comment ensemble — XLM-R, DistilBERT and the LLM stance pass — lives
in Stage 2. The router used to send bypassed posts straight to the assembler,
so on those posts every comment kept its Stage-1 label alone and the UI's
per-comment model comparison showed three empty columns, with nothing on screen
saying why. Measured on the live database: a post with 549 analysed comments,
`llm_used: false`, no ensemble block, and a method breakdown of
`{stub: 399, fast: 81, llm: 59, emoji: 10}` — not one of the three Stage-2
labellers had seen it.

Every post reaches Stage 2 now. What the gate still decides is POST-LEVEL work
(summary / post-type / insight), which is what `estimated_llm_share` measures —
so these tests pin both halves: everyone gets the comment lane, only the routed
get the post-level tasks, and the counter still separates them.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest

from defense.services.workers.router import router as R


class _Redis:
    """Records what the router emitted, per stream."""

    def __init__(self):
        self.streams: dict[str, list[dict]] = {}
        self.counters: dict[str, int] = {}

    async def xadd(self, stream, fields):
        self.streams.setdefault(stream, []).append(json.loads(fields["data"]))

    async def incr(self, key):
        self.counters[key] = self.counters.get(key, 0) + 1

    async def publish(self, *a, **k):
        return None

    async def rpush(self, *a, **k):
        return None

    async def expire(self, *a, **k):
        return None

    def pipeline(self, *a, **k):
        return self

    async def execute(self, *a, **k):
        return None


def _envelope(stage1: dict, options: dict | None = None) -> dict:
    return {
        b"data": json.dumps({
            "post_id": "p1",
            "job_id": None,
            "stage1_result": stage1,
            "normalized_post": {"caption": "x", "photo_urls": []},
            "options": options or {},
        }).encode()
    }


#: Stage-1 output confident enough that no routing rule fires.
_CONFIDENT = {
    "confidence": 0.95,
    "post_type": "news",
    "post_type_confidence": 0.95,
    "toxicity_score": 0.0,
    "photo_urls": [],
    "caption_chars": 50,
    "script": "bengali",
    "is_banglish": False,
    "post_summary": "একটি সারাংশ",
    "comment_analysis": {"analyzed": 2, "comments": [{"id": "c1", "text": "hi"}]},
}

#: Stage-1 output that trips the confidence gate.
_UNCERTAIN = dict(_CONFIDENT, confidence=0.1)


@pytest.mark.asyncio
async def test_a_bypassed_post_still_reaches_stage_2_for_its_comments():
    redis = _Redis()
    await R._process_message(redis, b"1-1", _envelope(_CONFIDENT))

    assert R.STAGE2_QUEUE in redis.streams, (
        "a post with no post-level work still needs the comment ensemble"
    )
    assert R.ASSEMBLER_QUEUE not in redis.streams, (
        "the router no longer short-circuits to the assembler"
    )

    sent = redis.streams[R.STAGE2_QUEUE][0]
    flags = sent["task_flags"]
    assert flags["post_level_routed"] is False
    # ...and none of the expensive post-level tasks were asked for.
    assert flags["want_summary"] is False
    assert flags["want_post_type"] is False
    assert flags["want_insight"] is False


@pytest.mark.asyncio
async def test_a_routed_post_gets_the_post_level_tasks_too():
    redis = _Redis()
    await R._process_message(redis, b"1-1", _envelope(_UNCERTAIN))

    sent = redis.streams[R.STAGE2_QUEUE][0]
    assert sent["task_flags"]["post_level_routed"] is True


@pytest.mark.asyncio
async def test_the_llm_share_counter_still_counts_only_post_level_routing():
    """`estimated_llm_share` is built on this counter. If it incremented for
    every post — which now all reach Stage 2 — the measurement would silently
    become the constant 1.0, which is exactly the regression this pass fixed."""
    redis = _Redis()
    await R._process_message(redis, b"1-1", _envelope(_CONFIDENT))
    await R._process_message(redis, b"1-2", _envelope(_CONFIDENT))
    await R._process_message(redis, b"1-3", _envelope(_UNCERTAIN))

    assert redis.counters[R.STAT_TOTAL] == 3
    assert redis.counters.get(R.STAT_LLM, 0) == 1, "only the uncertain post was routed"


@pytest.mark.asyncio
async def test_the_target_language_survives_a_bypass():
    """The comment lane summarises in the post's language, so the flag it reads
    has to be populated even when no post-level task ran."""
    redis = _Redis()
    stage1 = dict(_CONFIDENT, language="bn")
    await R._process_message(redis, b"1-1", _envelope(stage1))

    assert redis.streams[R.STAGE2_QUEUE][0]["task_flags"]["target_lang"] == "bn"

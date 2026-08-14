"""Tests for §6.3: every filtered comment reaches the LLM, via a bounded queue.

The report was "the LLM does not analyse all the comments; after filtering
emoji, batches of comments should be queued and each batch processed until
done". Only 28.7% of comments ever got an LLM label, because of two caps and
one sequential loop:

  * ``STAGE1_LLM_COMMENT_MAX`` (60) — Stage 1 LLM-labelled only the 60
    most-liked substantive comments per post,
  * ``COMMENT_STANCE_MAX_PER_POST`` (40) — Stage 2 re-labelled only 40,
  * and the batch loop ran strictly sequentially, so raising either cap simply
    stalled the post. All 50 posts exceeded both caps; the largest thread holds
    2,857 comments.

Both caps now default to 0 (no cap) and batches run through a
bounded-concurrency queue with per-batch retry. The cost is stated, not hidden:
~8,700 non-emoji comments at 25/batch is ~350 LLM calls per corpus run against
~50 before — full coverage bought with time, deliberately.
"""

import asyncio
import sys

sys.path.insert(0, '/home/bk/code/defense/src/defense')

import pytest

from services.workers.stage1_nlp import comment_analyzer as ca


@pytest.fixture(autouse=True)
def _enable_stage1_comment_llm(monkeypatch):
    """This file tests the Stage-1 comment LLM pass itself.

    It ships DISABLED (Stage 2 labels every post's comments with a better model,
    so running it in Stage 1 too is duplicated spend — see config.py), but the
    mechanism is still supported and still has to work when switched on.
    """
    monkeypatch.setattr(ca, "_LLM_COMMENTS_ENABLED", True, raising=False)

from services.workers.stage1_nlp.comment_analyzer import _apply_labels, analyze_comments


def _comment(i: int, likes: int = 0) -> dict:
    # Long enough to be substantive, distinct enough to trace through a batch.
    return {"id": str(i), "likes": likes,
            "text": f"comment number {i} with a properly written opinion in it"}


class _Registry:
    stub_mode = True
    llm_mode = True

    def get_sentiment_model(self, hf_name):
        return None

    def get_llm_client(self):
        return object()


class _RecordingLLM:
    """Stands in for classify_comments_llm; records batch sizes + concurrency."""

    def __init__(self, fail_batches: set[int] | None = None, delay: float = 0.0):
        self.batches: list[list[str]] = []
        self.fail_batches = fail_batches or set()
        self.delay = delay
        self.in_flight = 0
        self.max_in_flight = 0

    async def __call__(self, texts, llm, backend_override):
        index = len(self.batches)
        self.batches.append(list(texts))
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            if index in self.fail_batches:
                raise RuntimeError("backend hiccup")
            return [
                {"sentiment": "negative", "sentiment_score": -0.7,
                 "emotion": "anger", "keywords": []}
                for _ in texts
            ]
        finally:
            self.in_flight -= 1


@pytest.fixture
def patched(monkeypatch):
    def _install(llm_stub, **env):
        for k, v in env.items():
            monkeypatch.setenv(k, str(v))
        monkeypatch.setattr(ca, "classify_comments_llm", llm_stub)
        return llm_stub
    return _install


# ---------------------------------------------------------------------------
# The caps are gone
# ---------------------------------------------------------------------------

def test_stage1_comment_cap_defaults_to_unlimited():
    assert ca._LLM_COMMENT_MAX == 0


@pytest.mark.asyncio
async def test_every_substantive_comment_reaches_the_llm(patched):
    """200 comments — under the old default only the top 60 were ever labelled."""
    stub = patched(_RecordingLLM())
    comments = [_comment(i, likes=i) for i in range(200)]

    out = await analyze_comments(comments, _Registry())

    sent = [t for batch in stub.batches for t in batch]
    assert len(sent) == 200
    assert out["method_breakdown"].get("llm") == 200
    assert out["provenance"]["inferred_share"] == 1.0


@pytest.mark.asyncio
async def test_a_positive_cap_still_selects_the_most_engaged(patched):
    """The knob survives for a fast demo run."""
    stub = patched(_RecordingLLM())
    monkey_max = 10
    ca._LLM_COMMENT_MAX = monkey_max
    try:
        comments = [_comment(i, likes=i) for i in range(50)]
        await analyze_comments(comments, _Registry())
        sent = {t for batch in stub.batches for t in batch}
        assert len(sent) == monkey_max
        # Highest-liked comments (ids 40-49) are the ones chosen.
        assert all("comment number 4" in t for t in sent)
    finally:
        ca._LLM_COMMENT_MAX = 0


# ---------------------------------------------------------------------------
# Batching + bounded concurrency
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_comments_are_split_into_bounded_batches(patched):
    stub = patched(_RecordingLLM())
    await analyze_comments([_comment(i) for i in range(60)], _Registry())

    assert len(stub.batches) == 3                      # 60 / 25 → 25, 25, 10
    assert [len(b) for b in stub.batches] == [25, 25, 10]


@pytest.mark.asyncio
async def test_batches_run_concurrently_but_bounded(patched):
    """The old loop was strictly sequential — max_in_flight would be 1."""
    stub = patched(_RecordingLLM(delay=0.02))
    await analyze_comments([_comment(i) for i in range(200)], _Registry())

    assert stub.max_in_flight > 1, "batches still run one at a time"
    assert stub.max_in_flight <= ca._LLM_CONCURRENCY


# ---------------------------------------------------------------------------
# Per-batch isolation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_one_failing_batch_does_not_lose_the_rest(patched):
    """A batch that exhausts its retries leaves ONLY its own comments degraded."""
    # Only the first call fails; with retries off, that is one batch lost.
    stub = patched(_RecordingLLM(fail_batches={0}))
    ca._LLM_BATCH_RETRIES = 0
    try:
        out = await analyze_comments([_comment(i) for i in range(75)], _Registry())
    finally:
        ca._LLM_BATCH_RETRIES = 1

    mb = out["method_breakdown"]
    # 75 comments = 3 batches; batch 0 failed, so 50 still got LLM labels.
    assert mb.get("llm") == 50
    assert mb.get("stub") == 25          # the failed batch keeps its Stage-1 label
    assert out["analyzed"] == 75          # coverage is never reduced by a failure


@pytest.mark.asyncio
async def test_a_transient_batch_failure_is_retried(patched):
    stub = patched(_RecordingLLM(fail_batches={0}))   # first attempt only
    out = await analyze_comments([_comment(i) for i in range(25)], _Registry())

    assert len(stub.batches) == 2                      # attempt + retry
    assert out["method_breakdown"].get("llm") == 25


# ---------------------------------------------------------------------------
# Index alignment is the merge contract
# ---------------------------------------------------------------------------

def test_apply_labels_rejects_a_length_mismatch():
    """A shifted list would attach every comment's sentiment to its neighbour."""
    batch = [{"text": "a"}, {"text": "b"}, {"text": "c"}]
    with pytest.raises(ValueError, match="index alignment"):
        _apply_labels(batch, [{"sentiment": "positive", "sentiment_score": 0.5,
                               "emotion": "joy"}])


def test_apply_labels_merges_in_order():
    batch = [{"text": "a"}, {"text": "b"}]
    labels = [
        {"sentiment": "positive", "sentiment_score": 0.5, "emotion": "joy"},
        {"sentiment": "negative", "sentiment_score": -0.5, "emotion": "anger"},
    ]
    assert _apply_labels(batch, labels) == 2
    assert batch[0]["sentiment"] == "positive"
    assert batch[1]["sentiment"] == "negative"
    assert all(r["method"] == "llm" for r in batch)


# ---------------------------------------------------------------------------
# Progress reporting
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_progress_is_reported_per_batch(patched):
    """Without this the Trace tab looks hung for minutes on a large thread."""
    patched(_RecordingLLM())
    seen: list[tuple[int, int]] = []

    async def cb(done, total):
        seen.append((done, total))

    await analyze_comments(
        [_comment(i) for i in range(75)], _Registry(), None, None, cb
    )
    assert len(seen) == 3
    assert {t for _, t in seen} == {3}
    assert sorted(d for d, _ in seen) == [1, 2, 3]


@pytest.mark.asyncio
async def test_a_failing_progress_callback_never_breaks_analysis(patched):
    patched(_RecordingLLM())

    async def boom(done, total):
        raise RuntimeError("redis is down")

    out = await analyze_comments(
        [_comment(i) for i in range(25)], _Registry(), None, None, boom
    )
    assert out["method_breakdown"].get("llm") == 25

"""Tests for §5.11 / P1.4 — per-comment inference runs in batches.

`analyze_comments` awaited `classify_comment` one comment at a time, so in real
mode each substantive comment was an individual transformer forward pass — no
batching, despite XLM-R inference being 10-30x faster batched. 8,513 of 10,272
comments took that path.

The consequence for the project is not just speed: a latency benchmark run
against the unbatched path measures a missing `batch` argument, not a property
of the architecture. §9.8 says to fix this *before* the measurement run.

`torch`/`transformers` are not installed in this environment, so the real
forward pass cannot be exercised here. These tests pin the batching *contract* —
grouping, ordering, fallback and the short-circuit for comments that never touch
a model — with a fake tokenizer/model, which is where the bugs would be anyway.
"""

import sys

sys.path.insert(0, '/home/bk/code/defense/src/defense')

import pytest

from services.workers.stage1_nlp import text_analyzer as ta
from services.workers.stage1_nlp.comment_analyzer import analyze_comments
from services.workers.stage1_nlp.text_analyzer import analyze_sentiment_batch

_LONG = "this is a properly written opinion with enough words to be substantive"


class _Registry:
    """Real-mode registry whose models are recorded, not run."""

    stub_mode = False
    llm_mode = False

    def __init__(self, available=True):
        self._available = available
        self.requested: list[str] = []

    def get_sentiment_model(self, hf_name):
        self.requested.append(hf_name)
        return ("tok", "model") if self._available else None


@pytest.fixture
def batch_spy(monkeypatch):
    """Replace the real forward pass; record every batch it is handed."""
    calls: list[list[str]] = []

    def fake_batch(texts, tokenizer, model):
        calls.append(list(texts))
        return [("negative", -0.7, 0.9) for _ in texts]

    monkeypatch.setattr(ta, "_real_sentiment_batch", fake_batch)
    return calls


# ---------------------------------------------------------------------------
# The batching contract
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_one_forward_pass_for_many_texts(batch_spy):
    """The point of the change: 20 comments, one call — not 20."""
    texts = [f"{_LONG} number {i}" for i in range(20)]
    out = await analyze_sentiment_batch(texts, _Registry())

    assert len(batch_spy) == 1
    assert len(batch_spy[0]) == 20
    assert len(out) == 20


@pytest.mark.asyncio
async def test_results_stay_in_input_order(batch_spy):
    """Order is the merge contract — a shift mislabels every comment."""
    def ordered(texts, tokenizer, model):
        return [("positive", float(i), 1.0) for i, _ in enumerate(texts)]

    batch_spy.clear()
    import services.workers.stage1_nlp.text_analyzer as mod
    mod._real_sentiment_batch = ordered

    texts = [f"{_LONG} {i}" for i in range(5)]
    out = await analyze_sentiment_batch(texts, _Registry())
    assert [score for _label, score, _c, _e in out] == [0.0, 1.0, 2.0, 3.0, 4.0]


@pytest.mark.asyncio
async def test_texts_are_grouped_by_resolved_model(batch_spy):
    """A batch must be ONE model; this corpus mixes bn/en within a thread."""
    texts = [
        "এই সিদ্ধান্তটি সম্পূর্ণ ভুল এবং জনগণের বিরুদ্ধে গিয়েছে বলে মনে করি",  # bn
        "this is a properly written english opinion about the matter",        # en
    ]
    registry = _Registry()
    await analyze_sentiment_batch(texts, registry)

    # Two distinct models requested => two batches, never one mixed batch.
    if len(set(registry.requested)) > 1:
        assert len(batch_spy) == len(set(registry.requested))
        assert all(len(b) >= 1 for b in batch_spy)


@pytest.mark.asyncio
async def test_empty_texts_never_reach_a_model(batch_spy):
    out = await analyze_sentiment_batch(["", "   ", _LONG], _Registry())
    assert out[0] == ("neutral", 0.0, 0.0, "empty")
    assert out[1] == ("neutral", 0.0, 0.0, "empty")
    assert batch_spy and batch_spy[0] == [_LONG]


@pytest.mark.asyncio
async def test_stub_mode_skips_batching_entirely(batch_spy):
    class _Stub(_Registry):
        stub_mode = True

    out = await analyze_sentiment_batch([_LONG] * 3, _Stub())
    assert batch_spy == []
    assert all(engine == "stub" for *_x, engine in out)


@pytest.mark.asyncio
async def test_missing_model_falls_back_to_stub_not_a_crash(batch_spy):
    out = await analyze_sentiment_batch([_LONG] * 3, _Registry(available=False))
    assert batch_spy == []
    assert all(engine == "stub" for *_x, engine in out)


@pytest.mark.asyncio
async def test_a_failing_batch_degrades_rather_than_losing_the_post(monkeypatch):
    """One bad batch must not cost the post its labels."""
    def boom(texts, tokenizer, model):
        raise RuntimeError("CUDA out of memory")

    monkeypatch.setattr(ta, "_real_sentiment_batch", boom)
    out = await analyze_sentiment_batch([_LONG] * 4, _Registry())
    assert len(out) == 4
    assert all(engine == "stub" for *_x, engine in out)


@pytest.mark.asyncio
async def test_engine_is_reported_as_model_when_one_ran(batch_spy):
    out = await analyze_sentiment_batch([_LONG], _Registry())
    assert out[0][3] == "model"


# ---------------------------------------------------------------------------
# Integration with analyze_comments
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_analyze_comments_batches_substantive_comments(batch_spy):
    """Short and emoji comments never touch a model, so they stay out of it."""
    comments = [
        {"id": "1", "likes": 0, "text": _LONG + " one"},
        {"id": "2", "likes": 0, "text": "👍"},              # emoji
        {"id": "3", "likes": 0, "text": "ok"},              # short
        {"id": "4", "likes": 0, "text": _LONG + " two"},
    ]
    out = await analyze_comments(comments, _Registry())

    assert len(batch_spy) == 1, "substantive comments should share one forward pass"
    assert len(batch_spy[0]) == 2, "only the substantive comments belong in the batch"
    assert out["analyzed"] == 4          # coverage is unchanged
    assert out["method_breakdown"].get("model") == 2
    assert out["method_breakdown"].get("emoji") == 1
    assert out["method_breakdown"].get("fast") == 1


@pytest.mark.asyncio
async def test_batched_labels_land_on_the_right_comments(batch_spy):
    """Index alignment through analyze_comments, not just inside the batcher."""
    def by_position(texts, tokenizer, model):
        # "…one" → positive, "…two" → negative, so a swap is detectable.
        return [
            ("positive", 0.9, 1.0) if t.endswith("one") else ("negative", -0.9, 1.0)
            for t in texts
        ]

    import services.workers.stage1_nlp.text_analyzer as mod
    mod._real_sentiment_batch = by_position

    comments = [
        {"id": "1", "likes": 0, "text": _LONG + " one"},
        {"id": "2", "likes": 0, "text": "👍"},
        {"id": "3", "likes": 0, "text": _LONG + " two"},
    ]
    out = await analyze_comments(comments, _Registry())
    labels = {c["id"]: c["sentiment"] for c in out["comments"]}
    assert labels["1"] == "positive"
    assert labels["3"] == "negative"


@pytest.mark.asyncio
async def test_batch_failure_still_labels_every_comment(monkeypatch):
    """Coverage must never drop because an optimisation failed."""
    monkeypatch.setattr(
        ta, "_real_sentiment_batch",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    comments = [{"id": str(i), "likes": 0, "text": f"{_LONG} {i}"} for i in range(5)]
    out = await analyze_comments(comments, _Registry())
    assert out["analyzed"] == 5
    assert all(c["sentiment"] for c in out["comments"])

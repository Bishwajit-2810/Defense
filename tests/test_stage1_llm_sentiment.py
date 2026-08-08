"""Regression tests: a Stage-1 LLM that answers nothing must not read as neutral.

Ollama turns ``response_format={"type":"json_object"}`` into grammar-constrained
decoding, and a small model (gemma3:4b) can satisfy that grammar with the empty
object ``{}``. Every default downstream then looked like a judgement: sentiment
"neutral", score 0.00, toxicity 0.0, no keywords, confidence 83% — on a post that
is plainly negative. Two defences, one per layer:

  * ``libs/llm/client.py`` retries such a reply unconstrained (the same model then
    answers properly), and
  * ``llm_analyzer`` treats a missing sentiment as a failed analysis and raises,
    so the caller falls back to the deterministic path instead of inventing one.
"""

import sys

sys.path.insert(0, '/home/bk/code/defense/src/defense')

import pytest

from libs.llm.client import LLMClient, _is_degenerate_json
from services.workers.stage1_nlp.llm_analyzer import (
    analyze_text_llm,
    classify_comments_llm,
)

# A well-formed Stage-1 reply for the Bangla post that triggered this bug.
_GOOD_JSON = """{"language":"bn","sentiment":"negative","sentiment_score":-0.85,
"emotion":"anger","topics":["politics"],"intents":["express_grievance"],
"toxicity_score":0.65,"hate_speech_score":0.1,
"entities":[{"text":"তারেক রহমান","label":"PERSON"}],"keywords":["আওয়ামী লীগ"]}"""


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class _FakeCompletion:
    """Minimal stand-in for an OpenAI ChatCompletion."""

    def __init__(self, content: str, model: str = "gemma3:4b"):
        message = type("M", (), {"content": content})()
        self.choices = [type("C", (), {"message": message})()]
        self.usage = type(
            "U", (), {"prompt_tokens": 500, "completion_tokens": 2, "total_tokens": 502}
        )()
        self.model = model


class _FakeLLM:
    """Records every chat() call and replays a scripted list of contents."""

    def __init__(self, *contents: str):
        self._contents = list(contents)
        self.calls: list[dict] = []

    async def chat(self, **kwargs):
        self.calls.append(kwargs)
        content = self._contents[min(len(self.calls) - 1, len(self._contents) - 1)]
        return {"content": content, "model": "gemma3:4b", "backend": "local"}


# ---------------------------------------------------------------------------
# Degeneracy detection
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("content", ["{}", "  {}  ", "[]", "null", "", None])
def test_empty_but_valid_json_is_degenerate(content):
    assert _is_degenerate_json(content) is True


@pytest.mark.parametrize("content", ['{"sentiment":"negative"}', "[1]", "not json"])
def test_json_with_content_or_unparseable_is_not_degenerate(content):
    # Unparseable output is left to the caller's own parser/fallback.
    assert _is_degenerate_json(content) is False


# ---------------------------------------------------------------------------
# Client-level retry
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_json_mode_empty_reply_retries_unconstrained(monkeypatch):
    monkeypatch.setenv("LLM_BACKEND", "local")
    llm = LLMClient()
    seen: list[dict | None] = []

    async def fake_call(*, response_format, **kwargs):
        seen.append(response_format)
        # Constrained → `{}`; unconstrained → a real answer (observed behaviour).
        return _FakeCompletion("{}" if response_format is not None else _GOOD_JSON)

    monkeypatch.setattr(llm, "_call_api", fake_call)
    result = await llm.chat(
        role="stage1",
        messages=[{"role": "user", "content": "x"}],
        response_format={"type": "json_object"},
    )

    assert seen == [{"type": "json_object"}, None]  # retried without the grammar
    assert "negative" in result["content"]


@pytest.mark.asyncio
async def test_useful_json_mode_reply_is_not_retried(monkeypatch):
    monkeypatch.setenv("LLM_BACKEND", "local")
    llm = LLMClient()
    calls = []

    async def fake_call(**kwargs):
        calls.append(kwargs)
        return _FakeCompletion(_GOOD_JSON)

    monkeypatch.setattr(llm, "_call_api", fake_call)
    await llm.chat(
        role="stage1",
        messages=[{"role": "user", "content": "x"}],
        response_format={"type": "json_object"},
    )
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# Analyzer strictness
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("content", ["{}", '{"topics":["politics"]}', '{"sentiment":"mixed"}'])
async def test_missing_sentiment_raises_instead_of_returning_neutral(content):
    with pytest.raises(ValueError, match="no usable sentiment"):
        await analyze_text_llm("কিছু বাংলা টেক্সট", _FakeLLM(content))


@pytest.mark.asyncio
async def test_valid_reply_is_passed_through():
    out = await analyze_text_llm("কিছু বাংলা টেক্সট", _FakeLLM(_GOOD_JSON))
    assert out["sentiment"] == "negative"
    assert out["sentiment_score"] == -0.85
    assert out["toxicity_score"] == 0.65


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "label,raw,expected",
    [
        ("negative", 0.0, -0.5),    # label/number contradiction → keep the label's sign
        ("positive", 0.0, 0.5),
        ("negative", -0.7, -0.7),   # coherent scores pass through untouched
        ("neutral", 0.0, 0.0),
    ],
)
async def test_score_contradicting_its_label_is_repaired(label, raw, expected):
    content = f'{{"sentiment":"{label}","sentiment_score":{raw}}}'
    out = await analyze_text_llm("text", _FakeLLM(content))
    assert out["sentiment_score"] == expected


@pytest.mark.asyncio
async def test_confidence_tracks_reported_strength():
    """A flat 0.9 made empty answers look as trustworthy as decisive ones."""
    weak = await analyze_text_llm("t", _FakeLLM('{"sentiment":"neutral","sentiment_score":0.0}'))
    strong = await analyze_text_llm("t", _FakeLLM('{"sentiment":"negative","sentiment_score":-1.0}'))
    assert weak["sentiment_confidence"] < strong["sentiment_confidence"]
    assert weak["sentiment_confidence"] == 0.5


# ---------------------------------------------------------------------------
# Comment batches
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_unanswered_comment_slots_stay_none():
    """None keeps the caller's heuristic label; neutral would fake a verdict."""
    content = (
        '{"labels":[{"i":1,"s":"negative","e":"anger"},'
        '{"i":2,"s":"bogus","e":"anger"},'
        '{"i":3}]}'
    )
    out = await classify_comments_llm(["a", "b", "c"], _FakeLLM(content))
    assert out[0]["sentiment"] == "negative"
    assert out[1] is None
    assert out[2] is None


@pytest.mark.asyncio
async def test_empty_comment_batch_reply_falls_back_wholesale():
    out = await classify_comments_llm(["a", "b"], _FakeLLM("{}"))
    assert out == [None, None]

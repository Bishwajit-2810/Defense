"""Regression tests (§6.1): a summary that stops at the token ceiling must never
be returned — or cached — as though it were complete.

``LLMClient.chat()`` never inspected ``choice.finish_reason``, so a completion
that stopped because it hit ``max_tokens`` came back indistinguishable from a
finished one. The truncation is language-correlated — Bangla costs far more
tokens per character than English under these tokenizers, so the same "2-3
sentences" instruction overruns a 512-token ceiling in Bangla and fits in
English — and it was durable, because the half summary was written to the 7-day
response cache and persisted with the canonical result.

Three defences, one per layer:

  * ``libs/llm/client.py`` reports ``finish_reason``/``truncated`` and
    auto-continues a length-truncated free-text reply,
  * ``_trim_to_sentence`` cuts a still-truncated reply back to its last complete
    sentence so it never ends mid-word, and
  * ``_run_summary`` flags it (``post_summary_truncated``) and refuses to cache it.
"""

import sys

sys.path.insert(0, '/home/bk/code/defense/src/defense')

import pytest

from libs.llm.client import LLMClient
from services.workers.stage2_llm import worker as stage2_worker
from services.workers.stage2_llm.worker import _trim_to_sentence

# A Bangla summary cut off mid-sentence — the shape the owner reported.
_PARTIAL_BN = "পোস্টটিতে রাজনৈতিক পরিস্থিতি নিয়ে আলোচনা করা হয়েছে। মন্তব্যকারীরা"
_REST_BN = "মূলত ক্ষোভ প্রকাশ করেছেন।"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class _FakeCompletion:
    """Minimal stand-in for an OpenAI ChatCompletion carrying a finish_reason."""

    def __init__(self, content: str, finish_reason: str = "stop", model: str = "qwen2.5:7b"):
        message = type("M", (), {"content": content})()
        self.choices = [
            type("C", (), {"message": message, "finish_reason": finish_reason})()
        ]
        self.usage = type(
            "U", (), {"prompt_tokens": 400, "completion_tokens": 512, "total_tokens": 912}
        )()
        self.model = model


class _FakeRedis:
    """Just enough Redis for the usage counters _run_summary increments."""

    def __init__(self):
        self.counters: dict[str, int] = {}

    async def incr(self, key):
        self.counters[key] = self.counters.get(key, 0) + 1

    async def incrby(self, key, amount):
        self.counters[key] = self.counters.get(key, 0) + amount


# ---------------------------------------------------------------------------
# Client: finish_reason is surfaced
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_completed_reply_is_not_flagged_truncated(monkeypatch):
    monkeypatch.setenv("LLM_BACKEND", "local")
    llm = LLMClient()

    async def fake_call(**kwargs):
        return _FakeCompletion("a complete summary.", finish_reason="stop")

    monkeypatch.setattr(llm, "_call_api", fake_call)
    result = await llm.chat(role="stage2", messages=[{"role": "user", "content": "x"}])

    assert result["finish_reason"] == "stop"
    assert result["truncated"] is False
    assert result["continuations"] == 0


@pytest.mark.asyncio
async def test_length_truncated_reply_is_continued_and_concatenated(monkeypatch):
    """The actual recovery of "the later part" the report asks for."""
    monkeypatch.setenv("LLM_BACKEND", "local")
    monkeypatch.setenv("LLM_MAX_CONTINUATIONS", "2")
    llm = LLMClient()
    calls: list[dict] = []

    async def fake_call(**kwargs):
        calls.append(kwargs)
        # First call stops at the ceiling; the continuation finishes the thought.
        if len(calls) == 1:
            return _FakeCompletion(_PARTIAL_BN, finish_reason="length")
        return _FakeCompletion(_REST_BN, finish_reason="stop")

    monkeypatch.setattr(llm, "_call_api", fake_call)
    result = await llm.chat(role="stage2", messages=[{"role": "user", "content": "x"}])

    assert len(calls) == 2
    assert result["continuations"] == 1
    assert result["truncated"] is False
    assert _PARTIAL_BN in result["content"] and _REST_BN in result["content"]
    # The continuation must carry the partial answer as context, or the model
    # restarts from the top and the two halves overlap.
    cont_messages = calls[1]["messages"]
    assert cont_messages[-2] == {"role": "assistant", "content": _PARTIAL_BN}
    # Token spend is the sum across both calls, not just the first.
    assert result["usage"]["total_tokens"] == 912 * 2


@pytest.mark.asyncio
async def test_continuation_budget_is_bounded_and_still_reports_truncated(monkeypatch):
    monkeypatch.setenv("LLM_BACKEND", "local")
    monkeypatch.setenv("LLM_MAX_CONTINUATIONS", "2")
    llm = LLMClient()
    calls: list[dict] = []

    async def fake_call(**kwargs):
        calls.append(kwargs)
        return _FakeCompletion(_PARTIAL_BN, finish_reason="length")

    monkeypatch.setattr(llm, "_call_api", fake_call)
    result = await llm.chat(role="stage2", messages=[{"role": "user", "content": "x"}])

    assert len(calls) == 3           # the original + 2 continuations, then stop
    assert result["continuations"] == 2
    assert result["truncated"] is True   # never silently — the caller can act on it


@pytest.mark.asyncio
async def test_json_mode_reply_is_not_continued(monkeypatch):
    """A JSON reply cannot be continued token-wise into valid JSON."""
    monkeypatch.setenv("LLM_BACKEND", "local")
    llm = LLMClient()
    calls: list[dict] = []

    async def fake_call(**kwargs):
        calls.append(kwargs)
        return _FakeCompletion('{"post_type":"political', finish_reason="length")

    monkeypatch.setattr(llm, "_call_api", fake_call)
    result = await llm.chat(
        role="stage2",
        messages=[{"role": "user", "content": "x"}],
        response_format={"type": "json_object"},
    )

    assert len(calls) == 1
    assert result["truncated"] is True


# ---------------------------------------------------------------------------
# Sentence-boundary trim
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "text,expected",
    [
        # Bangla danda — the terminator this corpus actually uses.
        (_PARTIAL_BN, "পোস্টটিতে রাজনৈতিক পরিস্থিতি নিয়ে আলোচনা করা হয়েছে।"),
        ("The post discusses the political situation. Commenters mostly expr",
         "The post discusses the political situation."),
        ("Already complete.", "Already complete."),
        # Trailing whitespace from a cut-off generation is stripped, not trimmed away.
        ("Already complete.  \n", "Already complete."),
        # Refuses to gut the text: trimming here would discard >50%, so a reply
        # that is mostly one long unterminated run is kept whole.
        ("A. this is a much longer unterminated tail that dominates the reply",
         "A. this is a much longer unterminated tail that dominates the reply"),
        # No terminator at all — kept whole rather than emptied.
        ("no terminator anywhere", "no terminator anywhere"),
    ],
)
def test_trim_to_sentence(text, expected):
    assert _trim_to_sentence(text) == expected


# ---------------------------------------------------------------------------
# Worker: a truncated summary is flagged and never cached
# ---------------------------------------------------------------------------

def _patch_cache(monkeypatch) -> list[tuple]:
    """Replace the Redis response cache with an always-miss recording stub."""
    writes: list[tuple] = []

    async def fake_get(*args, **kwargs):
        return None

    async def fake_set(redis, backend, model, task, content_hash, value):
        writes.append((task, value))

    monkeypatch.setattr(stage2_worker, "get_cached", fake_get)
    monkeypatch.setattr(stage2_worker, "set_cached", fake_set)
    return writes


class _ScriptedLLM:
    def __init__(self, response: dict):
        self._response = response

    async def chat(self, **kwargs):
        return dict(self._response)


@pytest.mark.asyncio
async def test_truncated_summary_is_flagged_and_not_cached(monkeypatch):
    writes = _patch_cache(monkeypatch)
    llm = _ScriptedLLM({
        "content": _PARTIAL_BN,
        "model": "qwen2.5:7b",
        "backend": "local",
        "finish_reason": "length",
        "truncated": True,
        "continuations": 2,
        "usage": {"total_tokens": 900},
    })

    result = await stage2_worker._run_summary(
        llm, _FakeRedis(),
        {"caption": "কিছু একটা", "language": "bn", "post_id": "p1"},
        {}, "stage2", None,
    )

    assert result["post_summary_truncated"] is True
    # Trimmed to the last complete sentence — never ends mid-word.
    assert result["post_summary"].endswith("।")
    # The 7-day cache must not learn the half summary.
    assert writes == []


@pytest.mark.asyncio
async def test_complete_summary_is_cached_and_unflagged(monkeypatch):
    writes = _patch_cache(monkeypatch)
    llm = _ScriptedLLM({
        "content": "একটি সম্পূর্ণ সারসংক্ষেপ।",
        "model": "qwen2.5:7b",
        "backend": "local",
        "finish_reason": "stop",
        "truncated": False,
        "continuations": 0,
        "usage": {"total_tokens": 400},
    })

    result = await stage2_worker._run_summary(
        llm, _FakeRedis(),
        {"caption": "কিছু একটা", "language": "bn", "post_id": "p1"},
        {}, "stage2", None,
    )

    assert result["post_summary_truncated"] is False
    assert [t for t, _ in writes] == ["summary"]

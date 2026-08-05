"""Regression tests (§5.3 / §9.4): a comment label must say how it was produced.

"Per-comment sentiment over the whole embedded thread" is claim #1, but in the
shipped configuration the majority of those labels are not model output:

  * the fast path (17.1% of comments) is an emoji list plus a 14-word
    Bangla/Banglish lexicon, and
  * the rest of the non-LLM labels come from ``_stub_sentiment``, which for text
    without seed words returns a label derived from
    ``sum(ord(c) for c in text[:50]) % 100`` — deterministic and reproducible,
    and not sentiment.

``classify_comment`` nonetheless tagged every substantive comment ``"model"``
regardless of ``MODEL_STUB_MODE``, so ``method_breakdown`` claimed 8,513 model
inferences in a run where zero models were loaded. The method now reflects the
engine that actually ran, and ``provenance`` summarises the mix so a
``sentiment_breakdown`` chart can state where its numbers came from.
"""

import sys

sys.path.insert(0, '/home/bk/code/defense')

import pytest

from libs.labels import label_provenance
from services.workers.stage1_nlp.comment_analyzer import (
    analyze_comments,
    classify_comment,
)


class _Registry:
    """Stand-in ModelRegistry. `sentiment_model` present == real mode."""

    def __init__(self, stub_mode=True, has_model=False):
        self.stub_mode = stub_mode
        self.llm_mode = False
        self._has_model = has_model

    def get_sentiment_model(self, hf_name):
        if not self._has_model:
            return None

        class _Tok:
            pass

        return (_Tok(), _Tok())


# A substantive comment (long enough to miss the short/emoji fast path).
_SUBSTANTIVE = "এই সিদ্ধান্তটি সম্পূর্ণ ভুল এবং জনগণের বিরুদ্ধে গিয়েছে বলে আমি মনে করি"


@pytest.mark.asyncio
async def test_stub_mode_does_not_claim_a_model_inference():
    result = await classify_comment(_SUBSTANTIVE, _Registry(stub_mode=True))
    assert result["method"] == "stub"


@pytest.mark.asyncio
async def test_missing_model_in_real_mode_reports_stub_not_model():
    """The other half of the lie: real mode with no model loaded."""
    result = await classify_comment(
        _SUBSTANTIVE, _Registry(stub_mode=False, has_model=False)
    )
    assert result["method"] == "stub"


@pytest.mark.asyncio
async def test_short_comment_still_takes_the_fast_path():
    result = await classify_comment("valo 👍", _Registry(stub_mode=True))
    assert result["method"] == "fast"


@pytest.mark.asyncio
async def test_emoji_only_comment_is_tagged_emoji_not_fast():
    """§6.2: an emoji reaction and a one-word comment are different things."""
    result = await classify_comment("👍👍❤️", _Registry(stub_mode=True))
    assert result["method"] == "emoji"


@pytest.mark.asyncio
async def test_method_breakdown_has_no_phantom_model_bucket():
    comments = [
        {"id": "1", "text": _SUBSTANTIVE, "likes": 3},
        {"id": "2", "text": "👍", "likes": 0},
    ]
    out = await analyze_comments(comments, _Registry(stub_mode=True))

    mb = out["method_breakdown"]
    # A zeroed "model" key reads as "we ran models and they found nothing".
    assert "model" not in mb
    assert mb == {"stub": 1, "emoji": 1}


@pytest.mark.asyncio
async def test_provenance_is_reported_alongside_the_breakdowns():
    comments = [
        {"id": "1", "text": _SUBSTANTIVE, "likes": 3},
        {"id": "2", "text": "👍", "likes": 0},
    ]
    out = await analyze_comments(comments, _Registry(stub_mode=True))

    prov = out["provenance"]
    assert prov["total"] == 2
    assert prov["inferred"] == 0          # nothing here came from a model
    assert prov["heuristic"] == 2
    assert prov["inferred_share"] == 0.0


@pytest.mark.asyncio
async def test_empty_thread_provenance_is_well_formed():
    out = await analyze_comments([], _Registry())
    assert out["provenance"] == {
        "total": 0, "inferred": 0, "heuristic": 0,
        "inferred_share": 0.0, "by_method": {},
    }


def test_label_provenance_counts_llm_and_model_as_inferred():
    prov = label_provenance({"fast": 10, "stub": 30, "model": 40, "llm": 20})
    assert prov["total"] == 100
    assert prov["inferred"] == 60
    assert prov["heuristic"] == 40
    assert prov["inferred_share"] == 0.6


def test_label_provenance_treats_emoji_and_failed_as_not_inferred():
    prov = label_provenance({"emoji": 5, "failed": 5, "llm": 10})
    assert prov["inferred"] == 10
    assert prov["heuristic"] == 10

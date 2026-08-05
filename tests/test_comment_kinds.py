"""Tests for §6.2: emoji-only comments are separated, not discarded.

The owner's report was "many comments are nothing but emoji; they should be
filtered". 1,759 of 10,272 comments (17.1%) took the "fast" path, and that path
conflated two genuinely different things — emoji-only reactions and short text
comments — labelling both with an emoji list plus a 14-word lexicon, from which
73.8% came out neutral. They were then counted in `sentiment_breakdown`
alongside model/LLM-labelled comments and were indistinguishable there.

The filtering is real (emoji-only comments never enter an LLM batch — they are
the cheapest possible tokens to waste), but their SENTIMENT is kept: ❤️ and 🤬
are signal the crowd actually gave, and this corpus is emoji-heavy. They are
separated into their own count and series rather than dropped.
"""

import importlib
import sys

sys.path.insert(0, '/home/bk/code/defense')

import pytest

from services.workers.stage1_nlp import comment_analyzer as ca
from services.workers.stage1_nlp.comment_analyzer import (
    KIND_EMOJI,
    KIND_SHORT,
    KIND_SUBSTANTIVE,
    _comment_kind,
    analyze_comments,
)


class _Registry:
    stub_mode = True
    llm_mode = False

    def get_sentiment_model(self, hf_name):
        return None


_SUBSTANTIVE = "এই সিদ্ধান্তটি সম্পূর্ণ ভুল এবং জনগণের বিরুদ্ধে গিয়েছে"


# ---------------------------------------------------------------------------
# The three-way classifier
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "text,expected",
    [
        ("❤️❤️❤️", KIND_EMOJI),
        ("👍", KIND_EMOJI),
        ("🤬🤬 !!!", KIND_EMOJI),        # punctuation is not a word token
        ("   ", KIND_EMOJI),
        ("", KIND_EMOJI),
        ("valo", KIND_SHORT),
        ("valo bhai", KIND_SHORT),
        ("ok 👍", KIND_SHORT),
        (_SUBSTANTIVE, KIND_SUBSTANTIVE),
        ("this is a properly written opinion", KIND_SUBSTANTIVE),
    ],
)
def test_comment_kind(text, expected):
    assert _comment_kind(text) == expected


# ---------------------------------------------------------------------------
# Emoji reactions are kept, counted separately, and kept out of LLM batches
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_emoji_reactions_keep_their_sentiment():
    """Discarding them would throw away information the crowd gave."""
    out = await analyze_comments(
        [
            {"id": "1", "text": "❤️❤️", "likes": 5},
            {"id": "2", "text": "🤬", "likes": 2},
        ],
        _Registry(),
    )
    labels = {c["id"]: c["sentiment"] for c in out["comments"]}
    assert labels["1"] == "positive"
    assert labels["2"] == "negative"


@pytest.mark.asyncio
async def test_reaction_only_is_counted_and_split_out():
    out = await analyze_comments(
        [
            {"id": "1", "text": "❤️", "likes": 0},
            {"id": "2", "text": "❤️", "likes": 0},
            {"id": "3", "text": _SUBSTANTIVE, "likes": 0},
        ],
        _Registry(),
    )
    assert out["reaction_only"] == 2
    # The all-comments series still counts everything...
    assert sum(out["sentiment_breakdown"].values()) == 3
    # ...while the substantive series is written comments only.
    assert sum(out["sentiment_breakdown_substantive"].values()) == 1


@pytest.mark.asyncio
async def test_emoji_comments_are_excluded_from_llm_batches():
    """The filtering the report actually asks for."""
    seen: list[list[str]] = []

    async def fake_classify(texts, llm, backend_override):
        seen.append(list(texts))
        return [
            {"sentiment": "negative", "sentiment_score": -0.7,
             "emotion": "anger", "keywords": []}
            for _ in texts
        ]

    class _LLMRegistry(_Registry):
        llm_mode = True

        def get_llm_client(self):
            return object()

    original = ca.classify_comments_llm
    ca.classify_comments_llm = fake_classify
    try:
        await analyze_comments(
            [
                {"id": "1", "text": "❤️❤️❤️", "likes": 99},   # most-liked, but emoji
                {"id": "2", "text": _SUBSTANTIVE, "likes": 1},
            ],
            _LLMRegistry(),
        )
    finally:
        ca.classify_comments_llm = original

    assert seen == [[_SUBSTANTIVE]], "an emoji-only comment reached the LLM"


@pytest.mark.asyncio
async def test_every_comment_still_gets_a_label():
    """Filtering bounds the LLM spend; it must not reduce coverage."""
    comments = [
        {"id": str(i), "text": t, "likes": 0}
        for i, t in enumerate(["❤️", "valo", _SUBSTANTIVE, "🤬🤬"])
    ]
    out = await analyze_comments(comments, _Registry())
    assert out["analyzed"] == 4
    assert len(out["comments"]) == 4
    assert all(c["sentiment"] for c in out["comments"])


# ---------------------------------------------------------------------------
# The laughing-emoji decision is named, not accidental
# ---------------------------------------------------------------------------

def test_laughter_is_negative_by_default_on_this_corpus():
    assert ca._LAUGH_SENTIMENT == "negative"


@pytest.mark.asyncio
async def test_laughter_agrees_between_the_sentiment_and_emotion_tables():
    """🤣 counted as negative for sentiment but mapped to NO emotion, so a
    mocking comment came out `negative` / `neutral` — an asymmetry between two
    tables that was an accident rather than a decision."""
    out = await analyze_comments([{"id": "1", "text": "🤣🤣🤣", "likes": 0}], _Registry())
    c = out["comments"][0]
    assert c["sentiment"] == "negative"
    assert c["emotion"] == "disgust"     # not "neutral"


def test_laughter_polarity_is_configurable(monkeypatch):
    """The switch the report asks for, so this stays a documented decision."""
    monkeypatch.setenv("COMMENT_LAUGH_SENTIMENT", "positive")
    reloaded = importlib.reload(ca)
    try:
        assert reloaded._LAUGH_SENTIMENT == "positive"
        assert "🤣" in reloaded._POS_EMOJI
        assert "🤣" not in reloaded._NEG_EMOJI
        assert "🤣" in reloaded._EMOTION_EMOJI["joy"]
    finally:
        monkeypatch.delenv("COMMENT_LAUGH_SENTIMENT", raising=False)
        importlib.reload(ca)

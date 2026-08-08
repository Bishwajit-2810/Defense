"""Tests for the §5.13 "smaller items worth a line each".

Individually minor, and each one a case of an output that could not describe
itself accurately:

  * **theme like-weighting** — keywords were weighted by raw likes, so one
    946-like comment outweighed 946 ordinary ones and "themes" was effectively
    the keyword list of the single most-liked comment wearing the label of a
    cross-comment aggregate;
  * **comment emotion** is always the free heuristic, even in real mode where a
    transformer emotion head is loaded and used for the *post* — true, documented
    in a docstring, and invisible in the output until now;
  * **`_clip_sentiment`** derived its label from `argmax` over three classes and
    its score from `pos_prob - neg_prob`, so the two could disagree (a "neutral"
    verdict carrying a positive score).

The laughing-emoji asymmetry from the same list is covered in
``tests/test_comment_kinds.py``; the missing `pytest-asyncio` and the pointless
f-string were fixed during the original review.
"""

import sys

sys.path.insert(0, '/home/bk/code/defense/src/defense')

import pytest

from services.workers.stage1_nlp.comment_analyzer import _extract_themes, analyze_comments


class _Registry:
    stub_mode = True
    llm_mode = False

    def get_sentiment_model(self, hf_name):
        return None


# ---------------------------------------------------------------------------
# Theme weighting must not collapse to "the most-liked comment"
# ---------------------------------------------------------------------------

def test_one_viral_comment_does_not_dictate_the_themes():
    """The corpus's real shape: one 946-like comment against many unliked ones."""
    comments = [{"likes": 946, "keywords": ["viral"]}]
    comments += [{"likes": 0, "keywords": ["common"]} for _ in range(20)]

    themes = _extract_themes(comments)
    # Under raw-like weighting "viral" scored 946 vs "common" 20 and won outright.
    assert themes[0] == "common", f"themes still dominated by one comment: {themes}"
    assert "viral" in themes, "a popular comment should still register"


def test_engagement_still_counts_for_something():
    """Sub-linear, not zero: a liked comment outranks an equal-count unliked one."""
    comments = [
        {"likes": 500, "keywords": ["popular"]},
        {"likes": 500, "keywords": ["popular"]},
        {"likes": 0, "keywords": ["quiet"]},
        {"likes": 0, "keywords": ["quiet"]},
    ]
    assert _extract_themes(comments)[0] == "popular"


def test_a_keyword_in_many_comments_beats_one_liked_comment():
    comments = [{"likes": 900, "keywords": ["one"]}]
    comments += [{"likes": 2, "keywords": ["many"]} for _ in range(10)]
    assert _extract_themes(comments)[0] == "many"


def test_negative_or_missing_likes_do_not_explode():
    themes = _extract_themes([
        {"likes": None, "keywords": ["a"]},
        {"likes": -5, "keywords": ["b"]},
        {"keywords": ["c"]},
    ])
    assert set(themes) == {"a", "b", "c"}


def test_no_keywords_yields_no_themes():
    assert _extract_themes([{"likes": 10, "keywords": []}]) == []


# ---------------------------------------------------------------------------
# Comment emotion provenance
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_comment_emotion_declares_itself_heuristic():
    """`emotion_breakdown` used to look like model output. Now it can say."""
    out = await analyze_comments(
        [{"id": "1", "likes": 0, "text": "এই সিদ্ধান্তটি সম্পূর্ণ ভুল এবং অন্যায়"}],
        _Registry(),
    )
    assert out["comments"][0]["emotion_method"] == "heuristic"


@pytest.mark.asyncio
async def test_emoji_comments_also_carry_emotion_provenance():
    out = await analyze_comments([{"id": "1", "likes": 0, "text": "😡😡"}], _Registry())
    assert out["comments"][0]["emotion_method"] == "heuristic"


# ---------------------------------------------------------------------------
# CLIP label/score consistency
# ---------------------------------------------------------------------------

def _clip_with_probs(pos, neg, neu):
    """Drive _clip_sentiment with fixed probabilities, no torch involved."""
    from services.workers.stage1_nlp import vision_analyzer as va

    class _FakeTensor:
        def __init__(self, values):
            self._v = values

        def squeeze(self):
            return self

        def tolist(self):
            return self._v

    class _FakeTorch:
        @staticmethod
        def softmax(logits, dim=-1):
            return _FakeTensor(logits._v if isinstance(logits, _FakeTensor) else logits)

        class no_grad:
            def __enter__(self_inner):
                return None

            def __exit__(self_inner, *a):
                return False

    class _Outputs:
        logits_per_image = _FakeTensor([pos, neg, neu])

    import sys as _sys
    import types

    fake_torch = types.ModuleType("torch")
    fake_torch.softmax = _FakeTorch.softmax
    fake_torch.no_grad = _FakeTorch.no_grad
    fake_pil = types.ModuleType("PIL")
    fake_image = types.ModuleType("PIL.Image")
    fake_image.open = lambda *_a, **_k: type("I", (), {"convert": lambda s, m: s})()
    fake_pil.Image = fake_image

    saved = {k: _sys.modules.get(k) for k in ("torch", "PIL", "PIL.Image")}
    _sys.modules["torch"] = fake_torch
    _sys.modules["PIL"] = fake_pil
    _sys.modules["PIL.Image"] = fake_image
    try:
        return va._clip_sentiment(b"", processor=lambda **k: {}, model=lambda **k: _Outputs())
    finally:
        for k, v in saved.items():
            if v is None:
                _sys.modules.pop(k, None)
            else:
                _sys.modules[k] = v


@pytest.mark.parametrize(
    "pos,neg,neu,expected_label",
    [
        (0.8, 0.1, 0.1, "positive"),
        (0.1, 0.8, 0.1, "negative"),
        (0.2, 0.2, 0.6, "neutral"),
        # The disagreement case: argmax says "neutral" while pos-neg is clearly
        # positive. Label and score must agree, and the score is the primary.
        (0.35, 0.05, 0.60, "positive"),
    ],
)
def test_clip_label_and_score_never_disagree(pos, neg, neu, expected_label):
    label, score = _clip_with_probs(pos, neg, neu)
    assert label == expected_label
    if label == "positive":
        assert score > 0.1
    elif label == "negative":
        assert score < -0.1
    else:
        assert -0.1 <= score <= 0.1

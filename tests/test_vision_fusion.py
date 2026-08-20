"""Regression tests (§5.2 / §9.3): an image that produced no verdict must not
be reported as a neutral one, and must not consume its fusion weight.

44 of 50 posts carry images and "multimodal text+image sentiment fusion" is
claim #2 of the project, but the image term had never contributed a non-zero
value in any runnable configuration. Four causes, and the fourth is the one that
made the other three invisible:

  * ``analyze_image`` caught a fetch failure and returned ``_stub_vision_result()``
    — a hardcoded ``neutral`` / ``0.0`` — while ``_build_result`` labelled the run
    ``vision_model: "SigLIP"`` whenever ``MODEL_STUB_MODE=false``. A real-mode run
    therefore reported *SigLIP produced neutral* for an image it never saw.

Two arithmetic defects in the same leg:

  * the documented ``image × 0.7 + OCR-text × 0.3`` rule for null-caption posts
    had an OCR term that was always ``0.0`` (the worker analysed the null
    caption, never the OCR text), so every image-only post's score was silently
    multiplied by 0.7 — enough to push a weak negative across the ±0.1 neutral
    boundary;
  * likewise ``0.6 × text + 0.4 × image`` shrank a real text signal by 40%
    whenever the image produced nothing.
"""

import sys

sys.path.insert(0, '/home/bk/code/defense/src/defense')

import pytest

from services.workers.stage1_nlp import vision_analyzer as vision
from services.workers.stage1_nlp.fusion import fuse_sentiment


class _Registry:
    def __init__(self, stub_mode=False, clip=True):
        self.stub_mode = stub_mode
        self._clip = clip

    def get_clip_processor(self):
        return object() if self._clip else None

    def get_clip_model(self):
        return object() if self._clip else None


# ---------------------------------------------------------------------------
# A failure is labelled as one
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fetch_failure_is_not_a_neutral_verdict(monkeypatch):
    async def boom(url):
        raise RuntimeError("UnsupportedProtocol: missing an 'http://' protocol")

    monkeypatch.setattr(vision, "_fetch_image_bytes", boom)
    result = await vision.analyze_image("posts/c/p/abc.jpg", _Registry())

    assert result["status"] == vision.STATUS_FETCH_FAILED
    # The absence of a verdict must not be spelled "neutral".
    assert result["image_sentiment"] is None
    assert result["image_sentiment_score"] is None


@pytest.mark.asyncio
async def test_missing_clip_model_is_labelled(monkeypatch):
    async def bytes_ok(url):
        return b"\xff\xd8\xff"

    monkeypatch.setattr(vision, "_fetch_image_bytes", bytes_ok)
    monkeypatch.setattr(vision, "_ocr_image", lambda b: "কিছু লেখা")
    result = await vision.analyze_image("posts/c/p/abc.jpg", _Registry(clip=False))

    assert result["status"] == vision.STATUS_MODEL_UNAVAILABLE
    assert result["image_sentiment"] is None
    # OCR is independent of the sentiment model and survives its absence.
    assert result["ocr_text"] == "কিছু লেখা"


@pytest.mark.asyncio
async def test_stub_mode_is_labelled_stub(monkeypatch):
    result = await vision.analyze_image("posts/c/p/abc.jpg", _Registry(stub_mode=True))
    assert result["status"] == vision.STATUS_STUB
    assert result["image_sentiment"] is None


def test_endpoint_without_scheme_gets_one(monkeypatch):
    """`.env` shipped MINIO_ENDPOINT=minio:9000 — httpx raised before fetching.

    The endpoint is patched on the settings object, not in the environment:
    `_resolve_image_url` reads `config`, which is built once at import, so
    `setenv` here would leave the assertion reading whatever the developer's
    `.env` happens to say rather than what this test set.
    """
    monkeypatch.setattr(vision.config, "minio_endpoint", "minio:9000")
    monkeypatch.setattr(vision.config, "minio_bucket", "defense")
    url = vision._resolve_image_url("posts/c/p/abc.jpg")
    assert url == "http://minio:9000/defense/posts/c/p/abc.jpg"


def test_absolute_url_is_left_alone(monkeypatch):
    monkeypatch.setattr(vision.config, "minio_endpoint", "http://minio:9000")
    assert (
        vision._resolve_image_url("https://cdn.example/x.jpg")
        == "https://cdn.example/x.jpg"
    )


# ---------------------------------------------------------------------------
# Fusion renormalises over the terms that are actually present
# ---------------------------------------------------------------------------

_NEG_TEXT = {"sentiment": "negative", "sentiment_score": -0.5}


def test_captioned_post_is_not_shrunk_by_a_dead_image_term():
    """0.6 × text + 0.4 × nothing used to drag −0.5 to −0.30."""
    label, score = fuse_sentiment(
        text_result=_NEG_TEXT,
        image_result={"status": vision.STATUS_FETCH_FAILED, "image_sentiment": None},
        reaction_breakdown={},
        caption="একটি ক্যাপশন",
    )
    assert score == -0.5
    assert label == "negative"


def test_captioned_post_with_a_real_image_still_uses_both_weights():
    label, score = fuse_sentiment(
        text_result=_NEG_TEXT,
        image_result={
            "status": vision.STATUS_OK,
            "image_sentiment": "positive",
            "image_sentiment_score": 0.5,
        },
        reaction_breakdown={},
        caption="একটি ক্যাপশন",
    )
    assert score == pytest.approx(0.6 * -0.5 + 0.4 * 0.5)


def test_null_caption_post_is_not_multiplied_by_point_seven():
    """The boundary case named in §5.2: −0.15 must not become −0.105."""
    label, score = fuse_sentiment(
        text_result=None,
        image_result={
            "status": vision.STATUS_OK,
            "image_sentiment": "negative",
            "image_sentiment_score": -0.15,
        },
        reaction_breakdown={},
        caption=None,
    )
    assert score == pytest.approx(-0.15)
    assert label == "negative"   # was flipped to "neutral" by the lost 0.3


def test_null_caption_post_uses_the_ocr_term_when_there_is_one():
    """The documented image×0.7 + OCR×0.3 rule, with a real OCR term."""
    label, score = fuse_sentiment(
        text_result={"sentiment": "positive", "sentiment_score": 0.8},
        image_result={
            "status": vision.STATUS_OK,
            "image_sentiment": "negative",
            "image_sentiment_score": -0.4,
        },
        reaction_breakdown={},
        caption=None,
    )
    assert score == pytest.approx(0.7 * -0.4 + 0.3 * 0.8)


def test_no_signal_at_all_is_neutral_zero():
    label, score = fuse_sentiment(
        text_result=None,
        image_result={"status": vision.STATUS_STUB, "image_sentiment": None},
        reaction_breakdown={},
        caption=None,
    )
    assert (label, score) == ("neutral", 0.0)


def test_legacy_image_result_without_status_is_still_honoured():
    """Callers predating the STATUS_* contract keep working."""
    _, score = fuse_sentiment(
        text_result=None,
        image_result={"image_sentiment": "positive", "image_sentiment_score": 0.6},
        reaction_breakdown={},
        caption=None,
    )
    assert score == pytest.approx(0.6)


def test_reaction_nudge_still_applies_after_renormalisation():
    _, score = fuse_sentiment(
        text_result={"sentiment": "neutral", "sentiment_score": 0.0},
        image_result=None,
        reaction_breakdown={"like": 10, "angry": 50, "sad": 40},
        caption="ক্যাপশন",
    )
    assert score < 0.0

"""Real mode must DEGRADE, and must say that it degraded.

Found while attempting §9.8's end-to-end real-mode run. Two defects, both
instances of patterns this document already catalogues:

  1. **`get_lang_detector` re-raised** where every sibling getter returns None.
     One missing optional dependency (`fasttext`) therefore killed the whole
     real-mode pipeline at the first post — so §9.8 could not even start on a
     fresh checkout, and the failure looked like a pipeline bug rather than a
     missing package.
  2. **`engine: "models"` reported the INTENDED path, not the executed one.**
     With every optional dependency absent, a run reported `engine: "models"`
     while producing entirely heuristic output. That is §5.2's defect exactly
     (provenance recording the code path that *was intended* rather than the one
     that ran), one layer over.

`torch`/`transformers` are genuinely absent here, so these tests run against the
real failure — which is the right place to test it.
"""

import sys

sys.path.insert(0, '/home/bk/code/defense/src/defense')

import pytest

from services.workers.stage1_nlp.models import ModelRegistry
from services.workers.stage1_nlp.text_analyzer import _real_language, analyze_text


@pytest.fixture
def real_mode(monkeypatch):
    monkeypatch.setenv("MODEL_STUB_MODE", "false")
    monkeypatch.setenv("STAGE1_LLM", "false")
    # The condition under test is "real mode, weights unavailable". Without
    # these the loaders reach the Hugging Face hub and the file spends minutes
    # downloading the very models it is asserting the absence of.
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "1")
    from defense.libs.common.config import get_settings
    get_settings.cache_clear()
    return ModelRegistry()


# ---------------------------------------------------------------------------
# Degrade, don't die
# ---------------------------------------------------------------------------

def test_missing_lang_detector_returns_none_rather_than_raising(real_mode):
    """It used to raise — the only getter that could kill a post."""
    assert real_mode.get_lang_detector() is None


def test_every_model_getter_returns_none_when_unavailable(real_mode):
    """Consistency is the point: no getter may be the one that raises."""
    getters = [
        real_mode.get_lang_detector,
        real_mode.get_emotion_pipeline,
        real_mode.get_toxicity_pipeline,
        real_mode.get_clip_processor,
        real_mode.get_clip_model,
        real_mode.get_ner_model,
        real_mode.get_keyword_model,
        real_mode.get_embedding_model,
    ]
    for getter in getters:
        assert getter() is None, f"{getter.__name__} did not degrade"
    assert real_mode.get_sentiment_model("cardiffnlp/twitter-xlm-roberta-base-sentiment") is None


@pytest.mark.asyncio
async def test_real_mode_analyses_a_post_end_to_end_without_models(real_mode):
    """The §9.8 blocker: this used to raise ModuleNotFoundError."""
    result = await analyze_text("এই সিদ্ধান্তটি সম্পূর্ণ ভুল এবং অন্যায়", real_mode)
    assert result["sentiment"] is not None
    assert result["engine"] == "models"


def test_failure_is_logged_once_not_once_per_post(real_mode):
    """A 10k-post batch must not emit 10k identical import errors."""
    # structlog, not caplog: these modules log through structlog, which is
    # unconfigured under pytest and therefore never reaches the stdlib handlers
    # caplog installs. capture_logs() sees the events whatever the config is.
    from structlog.testing import capture_logs

    with capture_logs() as events:
        for _ in range(5):
            real_mode.get_lang_detector()
    fasttext_errors = [
        e for e in events if "fastText unavailable" in str(e.get("event", ""))
    ]
    assert len(fasttext_errors) == 1, (
        f"expected exactly one fastText error, got {len(fasttext_errors)}"
    )


# ---------------------------------------------------------------------------
# ...and say so
# ---------------------------------------------------------------------------

def test_degraded_components_starts_empty(real_mode):
    assert real_mode.degraded_components() == []


def test_degraded_components_records_each_failure(real_mode):
    real_mode.get_lang_detector()
    real_mode.get_emotion_pipeline()
    real_mode.get_embedding_model()

    degraded = real_mode.degraded_components()
    assert "language" in degraded
    assert "emotion" in degraded
    assert "embedding" in degraded


@pytest.mark.asyncio
async def test_language_method_reports_the_fallback(real_mode):
    """`language_confidence` alone cannot say a heuristic produced it."""
    result = await analyze_text("এই সিদ্ধান্তটি সম্পূর্ণ ভুল", real_mode)
    assert result["language_method"] == "script_heuristic"


def test_language_falls_back_when_the_detector_is_none():
    lang, script, banglish, conf, method = _real_language("এই একটি বাংলা বাক্য", None)
    assert method == "script_heuristic"
    assert lang == "bn"
    # Deliberately moderate: a script heuristic must not feed the router's
    # confidence gate a number it has not earned.
    assert 0.0 < conf < 1.0


def test_language_falls_back_when_the_detector_raises():
    class _Broken:
        def predict(self, *a, **k):
            raise RuntimeError("model file is corrupt")

    _lang, _script, _bl, _conf, method = _real_language("some text here", _Broken())
    assert method == "script_heuristic"


@pytest.mark.asyncio
async def test_result_says_models_AND_which_parts_degraded(real_mode):
    """The §5.2 lesson: never let intended and executed look identical."""
    from services.workers.stage1_nlp import worker as stage1

    post = {
        "id": "p1", "campaignId": "c1", "postType": "TEXT",
        "caption": "এই সিদ্ধান্তটি সম্পূর্ণ ভুল",
        "comments": [], "photoUrls": [], "engagement": {"commentCount": 0},
        "url": "https://facebook.com/x", "platformPostId": "1",
    }
    text_result = await analyze_text(post["caption"], real_mode)
    result = stage1._build_result(
        post=post, text_result=text_result, image_result=None,
        comment_analysis={"analyzed": 0, "comments": []},
        post_summary=None,
        post_summary_lang=None,
        post_summary_grounding=None,
        overall_sentiment="negative", sentiment_score=-0.5, stage1_ms=1.0,
        degraded_components=real_mode.degraded_components(),
    )
    proc = result["processing"]
    # Both facts are present, and they disagree — which is the honest state.
    assert proc["nlp_engine"] == "models"
    assert proc["degraded_components"], "a fully-degraded run claimed to be clean"


def test_degraded_components_defaults_to_empty_for_stub_runs():
    """Stub mode is not "degraded" — it is a chosen configuration."""
    from services.workers.stage1_nlp import worker as stage1

    post = {
        "id": "p1", "campaignId": "c1", "postType": "TEXT", "caption": "hi",
        "comments": [], "photoUrls": [], "engagement": {"commentCount": 0},
        "url": "https://facebook.com/x", "platformPostId": "1",
    }
    result = stage1._build_result(
        post=post, text_result={"engine": "stub", "sentiment": "neutral"},
        image_result=None, comment_analysis={"analyzed": 0, "comments": []},
        post_summary=None, post_summary_lang=None, post_summary_grounding=None,
        overall_sentiment="neutral", sentiment_score=0.0, stage1_ms=1.0,
    )
    assert result["processing"]["degraded_components"] == []

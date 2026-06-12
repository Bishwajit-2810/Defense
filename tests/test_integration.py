"""
End-to-end integration test for the defense pipeline.

Tests the full flow: raw post -> normalize -> stage1_nlp (stub) -> router ->
stage2_llm (stub) -> assembler/builder -> schema-valid JSON output.

Does NOT require running services or databases.
"""

import json
import sys
import os

sys.path.insert(0, "/home/bk/code/defense")

import pytest

from libs.schemas import validate_input, validate_output, assert_valid_output
from libs.common import platform_from_url, content_hash, compute_coverage
from services.ingestion.normalizer import normalize_post
from services.workers.stage1_nlp.fusion import fuse_sentiment
from services.workers.router.rules import should_use_llm, get_task_flags
from services.workers.assembler.builder import build_canonical_result

# ---------------------------------------------------------------------------
# Load all 50 sample posts once at module level
# ---------------------------------------------------------------------------

with open("/home/bk/code/defense/posts_with_details.json") as _f:
    SAMPLE_POSTS = json.load(_f)

FIRST_POST = SAMPLE_POSTS[0]
SECOND_POST = SAMPLE_POSTS[1]


# ---------------------------------------------------------------------------
# Shared helper: make_stub_stage1
# ---------------------------------------------------------------------------

def make_stub_stage1(normalized_post: dict) -> dict:
    """Build a minimal but schema-satisfying stage1 result for a normalized post."""
    media_type = normalized_post.get("media_type", "TEXT")
    is_photo = media_type in ("PHOTO", "PHOTO_TEXT")
    caption = normalized_post.get("caption") or ""
    n_comments = len(normalized_post.get("comments", []))
    comment_count = normalized_post.get("engagement", {}).get("comment_count", 0)
    coverage = compute_coverage(n_comments, comment_count)
    return {
        "language": "bn",
        "overall_sentiment": "neutral",
        "sentiment_score": 0.0,
        "text_sentiment": "neutral" if caption else None,
        "image_sentiment": "neutral" if is_photo else None,
        "emotion": {"primary": "neutral", "scores": {"neutral": 0.8}},
        "intents": [],
        "topics": ["politics"],
        "entities": [],
        "brand_mentions": [],
        "keywords": ["bangladesh"],
        "toxicity_score": 0.1,
        "hate_speech_score": 0.05,
        "image_analysis": (
            {
                "ocr_text": None,
                "image_sentiment": "neutral",
                "image_sentiment_score": 0.0,
                "description": None,
            }
            if is_photo
            else None
        ),
        "comment_analysis": {
            "analyzed": n_comments,
            "coverage": coverage,
            "sentiment_breakdown": {
                "positive": 0,
                "negative": 0,
                "neutral": n_comments,
            },
            "themes": [],
            "top_keywords": [],
            "representative_comments": [],
        },
        "confidence": {
            "overall": 0.8,
            "sentiment": 0.75,
            "language": 0.9,
            "topics": 0.7,
        },
        "processing": {"stage1_ms": 100.0},
    }


# ---------------------------------------------------------------------------
# TestNormalizePipeline
# ---------------------------------------------------------------------------

class TestNormalizePipeline:
    """Tests for the ingestion normalizer over all 50 sample posts."""

    def test_normalize_all_50_posts(self):
        """Normalizing every sample post must not raise."""
        for post in SAMPLE_POSTS:
            normalize_post(post)  # must not raise

    def test_platform_always_facebook_for_sample(self):
        """All 50 sample posts have facebook.com URLs -> platform == 'facebook'."""
        for post in SAMPLE_POSTS:
            normalized = normalize_post(post)
            assert normalized["platform"] == "facebook", (
                f"Post {post['id']} expected platform='facebook', "
                f"got '{normalized['platform']}'"
            )

    def test_baseline_preserved(self):
        """baseline_sentiment is kept exactly as-received from upstream."""
        for post in SAMPLE_POSTS:
            normalized = normalize_post(post)
            assert normalized["baseline_sentiment"] == post.get("sentiment"), (
                f"Post {post['id']}: baseline_sentiment was mutated"
            )

    def test_baseline_viral_preserved(self):
        """baseline_viral_potential is kept exactly as-received from upstream."""
        for post in SAMPLE_POSTS:
            normalized = normalize_post(post)
            assert normalized["baseline_viral_potential"] == post.get("viralPotential"), (
                f"Post {post['id']}: baseline_viral_potential was mutated"
            )

    def test_comment_sentiment_null(self):
        """All normalized comments must have sentiment == None (rule #3)."""
        for post in SAMPLE_POSTS:
            normalized = normalize_post(post)
            for comment in normalized.get("comments", []):
                assert comment["sentiment"] is None, (
                    f"Post {post['id']}: comment {comment['id']} has non-null sentiment"
                )

    def test_coverage_computed(self):
        """Coverage field is present in normalized engagement and > 0 when comments exist."""
        for post in SAMPLE_POSTS:
            normalized = normalize_post(post)
            eng = normalized.get("engagement", {})
            assert "coverage" in eng, f"Post {post['id']} missing coverage in engagement"
            stored = post["engagement"].get("storedCommentRows", 0)
            total = post["engagement"].get("commentCount", 0)
            if stored > 0 and total > 0:
                assert eng["coverage"] > 0, (
                    f"Post {post['id']}: expected coverage > 0, got {eng['coverage']}"
                )

    def test_media_type_preserved(self):
        """media_type in normalized post must equal raw postType verbatim."""
        for post in SAMPLE_POSTS:
            normalized = normalize_post(post)
            assert normalized["media_type"] == post["postType"], (
                f"Post {post['id']}: media_type mismatch"
            )

    def test_content_hash_deterministic(self):
        """Same raw post produces the same content_hash across two calls."""
        for post in SAMPLE_POSTS[:10]:
            h1 = normalize_post(post)["content_hash"]
            h2 = normalize_post(post)["content_hash"]
            assert h1 == h2, f"Post {post['id']}: content_hash is not deterministic"

    def test_dedup_different_posts(self):
        """First and second sample posts must produce different content hashes."""
        h1 = normalize_post(FIRST_POST)["content_hash"]
        h2 = normalize_post(SECOND_POST)["content_hash"]
        assert h1 != h2, "Different posts unexpectedly produced identical content hashes"


# ---------------------------------------------------------------------------
# TestSentimentFusion
# ---------------------------------------------------------------------------

class TestSentimentFusion:
    """Tests for the stage1 sentiment fusion logic."""

    def test_text_only_post(self):
        """Text-only post (image_result=None) -> overall uses text sentiment directly."""
        text_result = {"sentiment": "positive", "sentiment_score": 0.7}
        overall, score = fuse_sentiment(text_result, None, {}, "some caption")
        assert overall == "positive"
        assert score == pytest.approx(0.7)

    def test_photo_with_caption(self):
        """Image post with caption: text*0.6 + image*0.4."""
        text_result = {"sentiment": "positive", "sentiment_score": 0.6}
        image_result = {"image_sentiment": "negative", "image_sentiment_score": -0.5}
        overall, score = fuse_sentiment(text_result, image_result, {}, "a caption")
        expected = round(0.6 * 0.6 + 0.4 * (-0.5), 4)
        assert score == pytest.approx(expected)
        # With expected = 0.16, which is > 0.1, the label should be positive
        assert overall == "positive"

    def test_reaction_nudge_negative(self):
        """ANGRY+SAD > 40% of total reactions nudges a neutral/positive result negative."""
        text_result = {"sentiment": "neutral", "sentiment_score": 0.05}
        # SAD=50, ANGRY=50, LIKE=10 -> negative ratio = 100/110 ~ 0.91
        reaction_breakdown = {"sad": 50, "angry": 50, "like": 10}
        overall, score = fuse_sentiment(text_result, None, reaction_breakdown, "text")
        # nudged down from 0.05 -> must be <= 0.05 (nudge pushes negative)
        assert score < 0.05

    def test_photo_null_caption(self):
        """Image post with null caption: image*0.7 + OCR-text*0.3."""
        image_result = {"image_sentiment": "positive", "image_sentiment_score": 0.8}
        # text_result represents OCR path; caption is None
        text_result = {"sentiment": "neutral", "sentiment_score": 0.0}
        overall, score = fuse_sentiment(text_result, image_result, {}, None)
        expected = round(0.7 * 0.8 + 0.3 * 0.0, 4)
        assert score == pytest.approx(expected)
        assert overall == "positive"

    def test_text_only_negative(self):
        """Text-only negative post passes through without modification."""
        text_result = {"sentiment": "negative", "sentiment_score": -0.8}
        overall, score = fuse_sentiment(text_result, None, {}, "sad text")
        assert overall == "negative"
        assert score == pytest.approx(-0.8)

    def test_reaction_nudge_only_fires_above_threshold(self):
        """Reaction nudge does NOT fire when negative ratio is exactly at threshold."""
        text_result = {"sentiment": "positive", "sentiment_score": 0.5}
        # 40% SAD/ANGRY exactly at threshold (not above)
        reaction_breakdown = {"sad": 40, "like": 60}
        overall, score = fuse_sentiment(text_result, None, reaction_breakdown, "text")
        # No nudge: score stays 0.5
        assert score == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# TestRouterRules
# ---------------------------------------------------------------------------

class TestRouterRules:
    """Tests for the router routing rules."""

    def test_low_confidence_routes_to_llm(self):
        """overall_confidence=0.4 triggers the low-confidence rule -> use_llm=True."""
        use_llm, reasons = should_use_llm(
            {"overall_confidence": 0.4, "post_type": "news"},
            {},
        )
        assert use_llm is True
        assert any("low_confidence" in r for r in reasons)

    def test_high_confidence_skips_llm(self):
        """All high-confidence, post_type set, no summary -> use_llm=False."""
        use_llm, reasons = should_use_llm(
            {
                "overall_confidence": 0.9,
                "toxicity_score": 0.1,
                "post_type": "news",
                "photo_urls": [],
            },
            {},
        )
        assert use_llm is False
        assert reasons == []

    def test_want_summary_routes_to_llm(self):
        """options={want_summary: True} triggers routing to LLM."""
        use_llm, reasons = should_use_llm(
            {
                "overall_confidence": 0.9,
                "post_type": "news",
                "photo_urls": [],
            },
            {"want_summary": True},
        )
        assert use_llm is True
        assert any("want_summary" in r for r in reasons)

    def test_high_toxicity_routes_to_llm(self):
        """toxicity_score=0.8 triggers high-toxicity rule -> use_llm=True."""
        use_llm, reasons = should_use_llm(
            {
                "overall_confidence": 0.9,
                "toxicity_score": 0.8,
                "post_type": "news",
                "photo_urls": [],
            },
            {},
        )
        assert use_llm is True
        assert any("high_toxicity" in r for r in reasons)

    def test_routing_rate_sample(self):
        """With confidence=0.9, post_type set, no summary: <20% of 50 posts route to LLM."""
        routed_to_llm = 0
        for post in SAMPLE_POSTS:
            partial = {
                "overall_confidence": 0.9,
                "toxicity_score": 0.05,
                "post_type": "news",
                "photo_urls": [],
                "caption": "",
                "language": "bn",
            }
            use_llm, _ = should_use_llm(partial, {})
            if use_llm:
                routed_to_llm += 1

        rate = routed_to_llm / len(SAMPLE_POSTS)
        assert rate < 0.20, (
            f"Expected <20% of posts routed to LLM, got {rate:.0%} ({routed_to_llm}/50)"
        )

    def test_unclassified_post_type_routes_to_llm(self):
        """post_type=None triggers the unclassified rule -> use_llm=True."""
        use_llm, reasons = should_use_llm(
            {"overall_confidence": 0.9, "photo_urls": []},
            {},
        )
        assert use_llm is True
        assert any("post_type" in r for r in reasons)

    def test_get_task_flags_want_summary(self):
        """get_task_flags returns want_summary=True when options request it."""
        flags = get_task_flags(
            {"overall_confidence": 0.9, "post_type": "news", "topics": ["a", "b"]},
            {"want_summary": True},
        )
        assert flags["want_summary"] is True

    def test_get_task_flags_no_summary_by_default(self):
        """get_task_flags returns want_summary=False when nothing triggers it."""
        flags = get_task_flags(
            {
                "overall_confidence": 0.9,
                "post_type": "news",
                "topics": ["a", "b"],
                "photo_urls": [],
                "image_sentiment": "neutral",
            },
            {},
        )
        assert flags["want_summary"] is False


# ---------------------------------------------------------------------------
# TestBuilderEndToEnd
# ---------------------------------------------------------------------------

class TestBuilderEndToEnd:
    """Tests for build_canonical_result producing schema-valid output."""

    # Fixture helpers --------------------------------------------------------

    @staticmethod
    def _get_post_by_type(media_type: str, needs_caption: bool = True):
        """Return the first raw post matching media_type and caption presence."""
        for post in SAMPLE_POSTS:
            has_caption = bool(post.get("caption"))
            if post["postType"] == media_type:
                if needs_caption and has_caption:
                    return post
                if not needs_caption:
                    return post
        raise ValueError(f"No post found for media_type={media_type}, needs_caption={needs_caption}")

    @staticmethod
    def _build_result(raw_post, stage2_result=None):
        normalized = normalize_post(raw_post)
        stage1 = make_stub_stage1(normalized)
        return normalized, stage1, build_canonical_result(normalized, stage1, stage2_result)

    # Tests ------------------------------------------------------------------

    def test_schema_valid_output(self):
        """build_canonical_result on first sample post passes validate_output."""
        _, _, result = self._build_result(FIRST_POST)
        valid, errors = validate_output(result)
        assert valid is True, f"Output schema errors: {errors}"

    def test_baseline_not_overwritten(self):
        """baseline_sentiment and baseline_viral_potential match the raw post."""
        _, _, result = self._build_result(FIRST_POST)
        assert result["baseline_sentiment"] == FIRST_POST.get("sentiment")
        assert result["baseline_viral_potential"] == FIRST_POST.get("viralPotential")

    def test_comment_analysis_present(self):
        """comment_analysis block contains analyzed, coverage, sentiment_breakdown."""
        _, _, result = self._build_result(FIRST_POST)
        ca = result["comment_analysis"]
        assert "analyzed" in ca
        assert "coverage" in ca
        assert "sentiment_breakdown" in ca
        sb = ca["sentiment_breakdown"]
        assert "positive" in sb
        assert "negative" in sb
        assert "neutral" in sb

    def test_text_only_image_null(self):
        """TEXT post produces image_analysis=None and image_sentiment=None."""
        text_post = self._get_post_by_type("TEXT")
        _, _, result = self._build_result(text_post)
        assert result["image_analysis"] is None, "TEXT post should have image_analysis=None"
        assert result["image_sentiment"] is None, "TEXT post should have image_sentiment=None"

    def test_photo_post_image_present(self):
        """PHOTO or PHOTO_TEXT post produces a non-null image_analysis block."""
        photo_post = self._get_post_by_type("PHOTO_TEXT")
        _, _, result = self._build_result(photo_post)
        assert result["image_analysis"] is not None, (
            "PHOTO_TEXT post should have non-null image_analysis"
        )

    def test_null_caption_text_sentiment_null(self):
        """PHOTO post with null/empty caption produces text_sentiment=None in result."""
        # Find a PHOTO post whose caption is null
        photo_null_caption = next(
            p for p in SAMPLE_POSTS
            if p["postType"] == "PHOTO" and not p.get("caption")
        )
        _, _, result = self._build_result(photo_null_caption)
        assert result["text_sentiment"] is None, (
            "Null-caption post should have text_sentiment=None"
        )

    def test_processing_metadata_llm_not_used(self):
        """When stage2_result=None, processing.llm_used=False."""
        _, _, result = self._build_result(FIRST_POST, stage2_result=None)
        assert result["processing"]["llm_used"] is False

    def test_processing_metadata_llm_used(self):
        """When stage2_result is provided, processing.llm_used=True."""
        stage2 = {
            "post_summary": "A test summary.",
            "post_summary_lang": "bn",
            "post_summary_source": "llm",
            "post_summary_grounding": None,
            "post_type": "political_commentary",
            "processing": {"stage2_ms": 350.0, "llm_backend": "anthropic", "llm_model": "claude-3"},
        }
        _, _, result = self._build_result(FIRST_POST, stage2_result=stage2)
        assert result["processing"]["llm_used"] is True
        assert result["processing"]["stage2_ms"] == pytest.approx(350.0)

    def test_all_50_posts_build_valid_output(self):
        """Normalize all 50 posts and build stub stage1; every result must be schema-valid."""
        for post in SAMPLE_POSTS:
            normalized = normalize_post(post)
            stage1 = make_stub_stage1(normalized)
            result = build_canonical_result(normalized, stage1, None)
            valid, errors = validate_output(result)
            assert valid is True, (
                f"Post {post['id']} produced invalid output: {errors}"
            )


# Parametrized version of all-50-posts test for maximum per-post visibility
@pytest.mark.parametrize("raw_post", SAMPLE_POSTS, ids=[p["id"] for p in SAMPLE_POSTS])
def test_parametrized_all_50_posts_build_valid_output(raw_post):
    """Each of the 50 sample posts flows through the full pipeline and passes schema validation."""
    normalized = normalize_post(raw_post)
    stage1 = make_stub_stage1(normalized)
    result = build_canonical_result(normalized, stage1, None)
    valid, errors = validate_output(result)
    assert valid is True, f"Post {raw_post['id']} schema errors: {errors}"


# ---------------------------------------------------------------------------
# TestEvalHarness
# ---------------------------------------------------------------------------

class TestEvalHarness:
    """Tests for the eval harness functions."""

    def test_eval_harness_input_validation(self):
        """run_input_validation on all 50 sample posts: every post passes."""
        from eval.harness import run_input_validation

        results = run_input_validation(SAMPLE_POSTS)

        # Find the pass_rate metric
        pass_rate_result = next(
            (r for r in results if r.metric == "pass_rate"), None
        )
        assert pass_rate_result is not None, "pass_rate metric missing from results"
        assert pass_rate_result.passed is True, (
            f"Input validation pass_rate={pass_rate_result.value:.2f} < 1.0"
        )
        assert pass_rate_result.value == pytest.approx(1.0), (
            "Not all sample posts passed input validation"
        )

    def test_eval_harness_input_validation_failed_count(self):
        """run_input_validation reports zero failures for the gold set."""
        from eval.harness import run_input_validation

        results = run_input_validation(SAMPLE_POSTS)
        failed_result = next((r for r in results if r.metric == "failed"), None)
        assert failed_result is not None
        assert failed_result.value == 0.0, (
            f"Expected 0 failed posts, got {failed_result.value}"
        )

    def test_eval_platform_detection(self):
        """run_platform_detection returns 'facebook' for all sample URLs (never 'hardcoded_facebook')."""
        from eval.harness import run_platform_detection

        results = run_platform_detection(SAMPLE_POSTS)

        # Check no_hardcoded_facebook metric
        no_hardcoded = next(
            (r for r in results if r.metric == "no_hardcoded_facebook"), None
        )
        assert no_hardcoded is not None
        assert no_hardcoded.passed is True

        # Check all platforms were detected
        detected_result = next(
            (r for r in results if r.metric == "platforms_detected"), None
        )
        assert detected_result is not None
        assert detected_result.passed is True
        assert detected_result.value == pytest.approx(float(len(SAMPLE_POSTS)))

    def test_eval_platform_detection_returns_facebook(self):
        """Every sample post URL resolves to 'facebook' (not 'other')."""
        for post in SAMPLE_POSTS:
            platform = platform_from_url(post["url"])
            assert platform == "facebook", (
                f"Post {post['id']} URL '{post['url']}' resolved to '{platform}', expected 'facebook'"
            )

    def test_eval_coverage_check(self):
        """run_coverage_check runs without error and returns informational results."""
        from eval.harness import run_coverage_check

        results = run_coverage_check(SAMPLE_POSTS)
        assert len(results) >= 2, "Expected at least 2 coverage check results"

        # Both metrics are informational and always pass
        for r in results:
            assert r.passed is True, (
                f"Coverage check metric '{r.metric}' unexpectedly failed"
            )

    def test_eval_coverage_check_over_one_count(self):
        """Coverage > 1.0 cases are counted correctly (storedCommentRows > commentCount)."""
        from eval.harness import run_coverage_check

        results = run_coverage_check(SAMPLE_POSTS)
        over_one = next((r for r in results if r.metric == "over_1_count"), None)
        assert over_one is not None

        # Manually verify
        expected_over_one = sum(
            1
            for p in SAMPLE_POSTS
            if compute_coverage(
                p["engagement"].get("storedCommentRows", 0),
                p["engagement"].get("commentCount", 0),
            )
            > 1.0
        )
        assert over_one.value == pytest.approx(float(expected_over_one))

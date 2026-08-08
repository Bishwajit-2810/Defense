"""Unit tests for libs/common/utils.py"""

import sys
sys.path.insert(0, '/home/bk/code/defense/src/defense')

import pytest

from libs.common.utils import (
    platform_from_url,
    normalize_text,
    detect_script,
    content_hash,
    compute_coverage,
    coverage_anomaly,
    is_banglish,
)


# ---------------------------------------------------------------------------
# platform_from_url
# ---------------------------------------------------------------------------

class TestPlatformFromUrl:
    def test_facebook_www(self):
        assert platform_from_url("https://www.facebook.com/xyz") == "facebook"

    def test_facebook_fb_short(self):
        assert platform_from_url("https://fb.com/xyz") == "facebook"

    def test_instagram(self):
        assert platform_from_url("https://www.instagram.com/p/abc") == "instagram"

    def test_twitter(self):
        assert platform_from_url("https://twitter.com/user") == "x"

    def test_x_com(self):
        assert platform_from_url("https://x.com/user") == "x"

    def test_telegram(self):
        assert platform_from_url("https://t.me/channel") == "telegram"

    def test_youtube(self):
        assert platform_from_url("https://youtube.com/watch?v=abc") == "youtube"

    def test_other(self):
        assert platform_from_url("https://example.com/post") == "other"

    def test_empty_string(self):
        # Any non-matching URL returns "other"
        assert platform_from_url("") == "other"

    def test_facebook_no_www(self):
        assert platform_from_url("https://facebook.com/page/123") == "facebook"

    def test_instagram_no_www(self):
        assert platform_from_url("https://instagram.com/reel/abc") == "instagram"

    def test_youtu_be_short(self):
        assert platform_from_url("https://youtu.be/dQw4w9WgXcQ") == "youtube"

    def test_telegram_me(self):
        assert platform_from_url("https://telegram.me/group") == "telegram"


# ---------------------------------------------------------------------------
# normalize_text
# ---------------------------------------------------------------------------

class TestNormalizeText:
    def test_strips_whitespace(self):
        assert normalize_text("  hello  ") == "hello"

    def test_none_returns_empty(self):
        assert normalize_text(None) == ""

    def test_empty_string_returns_empty(self):
        assert normalize_text("") == ""

    def test_inner_whitespace_preserved(self):
        assert normalize_text("  hello world  ") == "hello world"

    def test_unicode_nfc(self):
        # NFC normalization should produce a stable form
        result = normalize_text("hello")
        assert result == "hello"

    def test_bengali_text_stripped(self):
        result = normalize_text("  আমি বাংলায়  ")
        assert result == "আমি বাংলায়"


# ---------------------------------------------------------------------------
# detect_script
# ---------------------------------------------------------------------------

class TestDetectScript:
    def test_bengali(self):
        assert detect_script("আমি বাংলায় গান গাই") == "bengali"

    def test_latin(self):
        assert detect_script("hello world") == "latin"

    def test_mixed(self):
        # Contains both Bengali and Latin
        assert detect_script("আমি hello") == "mixed"

    def test_empty_returns_other(self):
        assert detect_script("") == "other"

    def test_digits_only_returns_other(self):
        # Digits are neither Bengali Unicode range nor Latin letters
        assert detect_script("12345") == "other"

    def test_none_returns_other(self):
        assert detect_script(None) == "other"


# ---------------------------------------------------------------------------
# content_hash
# ---------------------------------------------------------------------------

class TestContentHash:
    def _make_post(self, post_id="p1", caption="test", comments=None):
        return {
            "id": post_id,
            "caption": caption,
            "comments": comments or [],
        }

    def test_same_input_same_hash(self):
        post = self._make_post()
        assert content_hash(post) == content_hash(post)

    def test_different_id_different_hash(self):
        post_a = self._make_post(post_id="p1")
        post_b = self._make_post(post_id="p2")
        assert content_hash(post_a) != content_hash(post_b)

    def test_different_caption_different_hash(self):
        post_a = self._make_post(caption="hello")
        post_b = self._make_post(caption="world")
        assert content_hash(post_a) != content_hash(post_b)

    def test_different_comments_different_hash(self):
        post_a = self._make_post(comments=[{"id": "c1", "text": "foo"}])
        post_b = self._make_post(comments=[{"id": "c1", "text": "bar"}])
        assert content_hash(post_a) != content_hash(post_b)

    def test_returns_hex_string(self):
        post = self._make_post()
        result = content_hash(post)
        # SHA-256 hex digest is 64 characters
        assert len(result) == 64
        assert all(c in "0123456789abcdef" for c in result)

    def test_comments_sorted_by_id(self):
        # Order of comments in the list should not matter; sorted by "id"
        post_a = self._make_post(comments=[
            {"id": "c1", "text": "first"},
            {"id": "c2", "text": "second"},
        ])
        post_b = self._make_post(comments=[
            {"id": "c2", "text": "second"},
            {"id": "c1", "text": "first"},
        ])
        assert content_hash(post_a) == content_hash(post_b)

    def test_missing_caption_handled(self):
        post = {"id": "p1"}
        # Should not raise
        result = content_hash(post)
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# compute_coverage
# ---------------------------------------------------------------------------

class TestComputeCoverage:
    def test_standard_fraction(self):
        assert compute_coverage(100, 1000) == pytest.approx(0.1)

    def test_clamped_to_one(self):
        # storedCommentRows > commentCount happens (5 posts in the corpus, up to
        # 112 stored against 42 reported), but it is an upstream inconsistency,
        # not 500% coverage. It used to validate and render as "266.7%".
        assert compute_coverage(500, 100) == pytest.approx(1.0)

    def test_zero_total_returns_one(self):
        assert compute_coverage(0, 0) == pytest.approx(1.0)

    def test_full_coverage(self):
        assert compute_coverage(50, 50) == pytest.approx(1.0)

    def test_zero_analyzed(self):
        assert compute_coverage(0, 100) == pytest.approx(0.0)


class TestCoverageAnomaly:
    """The clamp must not make the discrepancy disappear — it moves it here."""

    def test_stored_exceeding_reported_is_flagged(self):
        anomaly = coverage_anomaly(112, 42)
        assert anomaly is not None
        assert anomaly["kind"] == "stored_exceeds_reported"
        assert anomaly["analyzed"] == 112
        assert anomaly["reported_comment_count"] == 42
        assert anomaly["raw_ratio"] == pytest.approx(2.6667, abs=1e-4)

    def test_normal_coverage_is_not_an_anomaly(self):
        assert coverage_anomaly(10, 100) is None

    def test_exact_match_is_not_an_anomaly(self):
        assert coverage_anomaly(50, 50) is None

    def test_zero_reported_is_not_an_anomaly(self):
        # Nothing to contradict — compute_coverage already calls this 1.0.
        assert coverage_anomaly(10, 0) is None


# ---------------------------------------------------------------------------
# is_banglish
# ---------------------------------------------------------------------------

class TestIsBanglish:
    def test_banglish_detected(self):
        # Contains multiple Banglish markers: "ami" and "bhai"
        assert is_banglish("ami tomar bhai") is True

    def test_plain_english_not_banglish(self):
        assert is_banglish("hello world") is False

    def test_single_banglish_word_not_enough(self):
        # Single match → coincidental; should return False
        assert is_banglish("ami is here") is False

    def test_empty_string_not_banglish(self):
        assert is_banglish("") is False

    def test_none_not_banglish(self):
        assert is_banglish(None) is False

    def test_bengali_script_not_banglish(self):
        # Pure Bengali script has no Latin chars
        assert is_banglish("আমি বাংলায় গান গাই") is False

    def test_multiple_markers(self):
        assert is_banglish("ami jabo na tumi koro") is True

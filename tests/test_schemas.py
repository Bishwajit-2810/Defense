"""Unit tests for libs/schemas/validator.py"""

import json
import sys
sys.path.insert(0, '/home/bk/code/defense')

import pytest

from libs.schemas import validate_input, validate_output, assert_valid_input

# ---------------------------------------------------------------------------
# Load the real gold-set sample data
# ---------------------------------------------------------------------------

with open('/home/bk/code/defense/posts_with_details.json') as f:
    SAMPLE_POSTS = json.load(f)


# ---------------------------------------------------------------------------
# Minimal valid fixtures
# ---------------------------------------------------------------------------

def _minimal_valid_post():
    """A minimal post that satisfies every required field."""
    return {
        "id": "test-post-id-001",
        "url": "https://www.facebook.com/test/123",
        "postType": "TEXT",
        "postedAt": "2024-01-01T00:00:00Z",
        "scrapedAt": "2024-01-01T01:00:00Z",
        "campaignId": "campaign-001",
        "platformPostId": "fb-post-001",
        "engagement": {
            "commentCount": 0,
            "storedCommentRows": 0,
            "totalReactions": 0,
            "shareCount": 0,
        },
        "reactionBreakdown": {},
        "comments": [],
    }


def _minimal_valid_output():
    """A minimal output result that satisfies every required field."""
    return {
        "post_id": "test-post-id-001",
        "campaign_id": "campaign-001",
        "platform": "facebook",
        "platform_post_id": "fb-post-001",
        "media_type": "TEXT",
        "language": "en",
        "overall_sentiment": "neutral",
        "sentiment_score": 0.0,
        "baseline_sentiment": None,
        "engagement": {
            "comment_count": 0,
            "stored_comments": 0,
            "total_reactions": 0,
            "share_count": 0,
        },
        "comment_analysis": {
            "analyzed": 0,
            "coverage": 1.0,
            "sentiment_breakdown": {
                "positive": 0,
                "negative": 0,
                "neutral": 0,
            },
        },
        "confidence": {
            "overall": 0.9,
            "sentiment": 0.8,
            "language": 0.95,
            "topics": 0.7,
        },
        "processing": {
            "stage1_ms": 120.5,
            "stage2_ms": None,
            "llm_used": False,
            "llm_backend": None,
            "llm_model": None,
            "schema_version": "1.0",
        },
        "created_at": "2024-01-01T01:00:00Z",
        "scraped_at": "2024-01-01T01:00:00Z",
    }


# ---------------------------------------------------------------------------
# Parametrized: all 50 sample posts must pass validate_input
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("post", SAMPLE_POSTS, ids=[p["id"] for p in SAMPLE_POSTS])
def test_all_sample_posts_pass_validate_input(post):
    valid, errors = validate_input(post)
    assert valid is True, f"Post {post['id']} failed validation: {errors}"


# ---------------------------------------------------------------------------
# validate_input — positive cases
# ---------------------------------------------------------------------------

class TestValidateInputPositive:
    def test_minimal_valid_post(self):
        post = _minimal_valid_post()
        valid, errors = validate_input(post)
        assert valid is True
        assert errors == []

    def test_returns_tuple(self):
        post = _minimal_valid_post()
        result = validate_input(post)
        assert isinstance(result, tuple)
        assert len(result) == 2

    def test_valid_post_with_comments(self):
        post = _minimal_valid_post()
        post["comments"] = [
            {"id": "c1", "text": "great post", "likes": 5},
        ]
        valid, errors = validate_input(post)
        assert valid is True

    def test_all_post_types_accepted(self):
        for ptype in ("PHOTO_TEXT", "PHOTO", "TEXT"):
            post = _minimal_valid_post()
            post["postType"] = ptype
            valid, errors = validate_input(post)
            assert valid is True, f"postType={ptype} should be valid"


# ---------------------------------------------------------------------------
# validate_input — negative cases
# ---------------------------------------------------------------------------

class TestValidateInputNegative:
    def test_missing_required_id(self):
        post = _minimal_valid_post()
        del post["id"]
        valid, errors = validate_input(post)
        assert valid is False
        assert len(errors) > 0

    def test_missing_required_engagement(self):
        post = _minimal_valid_post()
        del post["engagement"]
        valid, errors = validate_input(post)
        assert valid is False
        assert len(errors) > 0

    def test_invalid_post_type(self):
        post = _minimal_valid_post()
        post["postType"] = "VIDEO"  # not in the enum
        valid, errors = validate_input(post)
        assert valid is False
        assert len(errors) > 0

    def test_missing_required_url(self):
        post = _minimal_valid_post()
        del post["url"]
        valid, errors = validate_input(post)
        assert valid is False

    def test_missing_campaign_id(self):
        post = _minimal_valid_post()
        del post["campaignId"]
        valid, errors = validate_input(post)
        assert valid is False

    def test_engagement_missing_comment_count(self):
        post = _minimal_valid_post()
        del post["engagement"]["commentCount"]
        valid, errors = validate_input(post)
        assert valid is False

    def test_errors_are_human_readable_strings(self):
        post = _minimal_valid_post()
        del post["id"]
        valid, errors = validate_input(post)
        assert valid is False
        for err in errors:
            assert isinstance(err, str)


# ---------------------------------------------------------------------------
# assert_valid_input
# ---------------------------------------------------------------------------

class TestAssertValidInput:
    def test_valid_post_does_not_raise(self):
        post = _minimal_valid_post()
        assert_valid_input(post)  # should not raise

    def test_invalid_post_raises_value_error(self):
        post = _minimal_valid_post()
        del post["id"]
        with pytest.raises(ValueError):
            assert_valid_input(post)

    def test_error_message_contains_detail(self):
        post = _minimal_valid_post()
        del post["id"]
        with pytest.raises(ValueError) as exc_info:
            assert_valid_input(post)
        message = str(exc_info.value)
        # The message should mention schema validation
        assert "schema validation" in message.lower() or "validation" in message.lower()


# ---------------------------------------------------------------------------
# validate_output
# ---------------------------------------------------------------------------

class TestValidateOutput:
    def test_minimal_valid_output_passes(self):
        output = _minimal_valid_output()
        valid, errors = validate_output(output)
        assert valid is True, f"Expected valid output but got errors: {errors}"

    def test_output_missing_post_id_fails(self):
        output = _minimal_valid_output()
        del output["post_id"]
        valid, errors = validate_output(output)
        assert valid is False
        assert len(errors) > 0

    def test_output_missing_campaign_id_fails(self):
        output = _minimal_valid_output()
        del output["campaign_id"]
        valid, errors = validate_output(output)
        assert valid is False

    def test_output_invalid_overall_sentiment_fails(self):
        output = _minimal_valid_output()
        output["overall_sentiment"] = "ambivalent"  # not in enum
        valid, errors = validate_output(output)
        assert valid is False

    def test_output_invalid_media_type_fails(self):
        output = _minimal_valid_output()
        output["media_type"] = "VIDEO"  # not in enum
        valid, errors = validate_output(output)
        assert valid is False

    def test_output_missing_comment_analysis_fails(self):
        output = _minimal_valid_output()
        del output["comment_analysis"]
        valid, errors = validate_output(output)
        assert valid is False

    def test_output_returns_tuple(self):
        output = _minimal_valid_output()
        result = validate_output(output)
        assert isinstance(result, tuple)
        assert len(result) == 2

    def test_output_missing_engagement_fails(self):
        output = _minimal_valid_output()
        del output["engagement"]
        valid, errors = validate_output(output)
        assert valid is False

    def test_output_missing_created_at_fails(self):
        output = _minimal_valid_output()
        del output["created_at"]
        valid, errors = validate_output(output)
        assert valid is False

"""Regression tests: a reused near-duplicate must describe THE NEW POST.

PROJECT_ASSESSMENT §13.3. Near-duplicate reuse copies a prior analysis onto a new
post to skip Stage 1 and Stage 2. It was implemented as a verbatim SQL row copy,
which carried three defects at once:

  1. `embedding_is_stub` was not in the INSERT column list, so the copy took the
     column DEFAULT `FALSE` — an honestly-flagged stub row produced a copy
     claiming to be a real semantic vector;
  2. the copied `result` kept the SOURCE's `post_id`, `post_text`, `engagement`
     and `reaction_breakdown`. `/v1/search` returns that document verbatim, so a
     hit on the new post described a different one;
  3. only Postgres was written — no ClickHouse row, no object-storage blob — so
     reused posts were invisible to every analytics aggregate while still
     counting in Postgres-backed reports.

None of it was caught because `tests/test_near_dup.py` drove the functions with a
`FakeSession` returning canned rows, so the SQL never executed and the *content*
of the copy was never inspected. These tests assert the composed document
instead: what a consumer would actually read.

The path is on by default (`NEAR_DUP_DEDUP=true`), and identical captions hash to
identical stub vectors — cosine 1.0, comfortably over the 0.97 threshold — so any
repost takes it.
"""

import sys

sys.path.insert(0, '/home/bk/code/defense/src/defense')
sys.path.insert(0, '/home/bk/code/defense/src/defense/libs')
sys.path.insert(0, '/home/bk/code/defense/src/defense/services/workers/assembler')

import pytest

from services.ingestion.normalizer import normalize_post
from services.workers.assembler.builder import build_reused_result

_SHARED_CAPTION = "একই ক্যাপশন, ভিন্ন পোস্ট"


def _raw_post(post_id: str, *, reactions: int, comments: int) -> dict:
    """An upstream post: same caption, everything else its own."""
    return {
        "id": post_id,
        "campaignId": "camp-1",
        "platformPostId": f"pp-{post_id}",
        "url": f"https://www.facebook.com/{post_id}",
        "postType": "TEXT",
        "caption": _SHARED_CAPTION,
        "postedAt": "2026-08-01T10:00:00Z",
        "scrapedAt": "2026-08-02T10:00:00Z",
        "engagement": {
            "totalReactions": reactions,
            "commentCount": comments,
            "shareCount": 0,
            "storedCommentRows": comments,
        },
        "reactionBreakdown": {"LIKE": reactions, "ANGRY": 0},
        "comments": [
            {"id": f"{post_id}-c{i}", "text": f"comment {i} on {post_id}", "likes": 0}
            for i in range(comments)
        ],
    }


def _source_analysis() -> dict:
    """A canonical result for the SOURCE post — note every field names src-1."""
    return {
        "post_id": "src-1",
        "campaign_id": "camp-OLD",
        "platform": "facebook",
        "platform_post_id": "src-1",
        "media_type": "TEXT",
        "language": "bn",
        "post_text": _SHARED_CAPTION,
        "overall_sentiment": "negative",
        "sentiment_score": -0.7,
        "baseline_sentiment": "neutral",
        "topics": ["politics"],
        "keywords": ["নির্বাচন"],
        "post_summary": "A summary produced for src-1.",
        "engagement": {"total_reactions": 9999, "comment_count": 500, "share_count": 42},
        "reaction_breakdown": {"LIKE": 9000, "ANGRY": 999},
        "comment_analysis": {
            "analyzed": 500,
            "coverage": 1.0,
            "sentiment_breakdown": {"positive": 10, "negative": 480, "neutral": 10},
            "comments": [{"id": "src-1-c0", "sentiment": "negative"}],
            "themes": ["anger at src-1's thread"],
        },
        "confidence": {"overall": 0.8},
        "processing": {"schema_version": "1.3", "stage1_ms": 12.0, "llm_used": True},
        "created_at": "2026-07-01T00:00:00Z",
        "scraped_at": "2026-07-02T00:00:00Z",
    }


@pytest.fixture()
def reused():
    new_post = normalize_post(_raw_post("new-9", reactions=3, comments=2))
    return build_reused_result(new_post, _source_analysis(), "src-1", 0.999)


# ---------------------------------------------------------------------------
# Identity: the document must name the post it belongs to
# ---------------------------------------------------------------------------


def test_the_reused_document_names_the_new_post(reused):
    """`/v1/search` returns this dict verbatim. It used to say `src-1`."""
    assert reused["post_id"] == "new-9"
    assert reused["platform_post_id"] != "src-1"
    assert reused["campaign_id"] == "camp-1"


def test_engagement_and_reactions_belong_to_the_new_post(reused):
    """Reactions and comment counts are per-post FACTS, not analysis. Two posts
    can share a caption and have nothing else in common — the normal case for a
    repost, which is exactly what this path is for."""
    assert reused["engagement"]["total_reactions"] == 3
    assert reused["engagement"]["comment_count"] == 2
    assert reused["engagement"]["total_reactions"] != 9999
    assert reused["engagement"]["comment_count"] != 500
    # Case-insensitive: the normalizer lower-cases reaction keys and Stage 1
    # re-emits them upper-cased, which is why `persistence._reaction_columns`
    # matches either (§11.3b). The reuse path takes the normalizer's form.
    rb = {k.lower(): v for k, v in reused["reaction_breakdown"].items()}
    assert rb.get("like") == 3
    assert rb.get("like") != 9000


def test_timestamps_belong_to_the_new_post(reused):
    assert reused["created_at"].startswith("2026-08-01")
    assert not reused["created_at"].startswith("2026-07-01")


# ---------------------------------------------------------------------------
# ...and the analysis is what gets reused
# ---------------------------------------------------------------------------


def test_the_post_level_analysis_is_reused(reused):
    """This is the point of the feature: skip Stage 1 and 2 for a caption that
    has already been analysed."""
    assert reused["overall_sentiment"] == "negative"
    assert reused["sentiment_score"] == -0.7
    assert reused["topics"] == ["politics"]
    assert reused["post_summary"] == "A summary produced for src-1."


# ---------------------------------------------------------------------------
# The comment thread is NOT reused
# ---------------------------------------------------------------------------


def test_the_source_comment_analysis_is_not_carried_over(reused):
    """A near-duplicate is a CAPTION match. The two threads are different people
    saying different things, so inheriting the source's per-comment labels would
    be fabricated data about comments nobody read — §13.3's defect in its worst
    form."""
    ca = reused["comment_analysis"]
    assert ca["analyzed"] == 0
    assert ca["coverage"] == 0.0
    assert ca["sentiment_breakdown"] == {"positive": 0, "negative": 0, "neutral": 0}
    assert "comments" not in ca or not ca.get("comments")
    assert "themes" not in ca or not ca.get("themes")


def test_the_unanalysed_thread_says_why(reused):
    """Absent is not the same as zero. §13.3's sibling rule from
    `aggregate_target_stances`: "nobody talked about X" and "everybody was
    neutral about X" must not look the same."""
    prov = reused["comment_analysis"]["provenance"]
    assert "reused" in prov["note"]
    assert prov["stored_comments"] == 2


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------


def test_a_reused_result_says_it_was_reused(reused):
    """It must not be indistinguishable from a fresh analysis."""
    reuse = reused["processing"]["reused_from"]
    assert reuse["source_post_id"] == "src-1"
    assert reuse["similarity"] == 0.999
    assert reuse["reused"] == "post_level_analysis_only"


def test_a_reused_result_claims_no_stage_time(reused):
    """No stage ran, so reporting the source's stage timings would corrupt any
    latency measurement taken over the corpus."""
    assert reused["processing"]["stage1_ms"] == 0
    assert reused["processing"]["stage2_ms"] == 0


def test_the_composed_document_is_schema_valid(reused):
    """build_reused_result validates before returning; assert the guarantee."""
    from defense.contracts.schemas.validator import assert_valid_output  # noqa: PLC0415

    assert_valid_output(reused)


# ---------------------------------------------------------------------------
# All three stores, not just Postgres
# ---------------------------------------------------------------------------


def test_reuse_goes_through_the_assembler_so_every_store_is_written():
    """The old path INSERTed straight into analysis_results, so reused posts were
    absent from every ClickHouse aggregate while counting in Postgres-backed
    reports — the two stores disagreed by construction."""
    src = open(
        '/home/bk/code/defense/services/ingestion/service.py', encoding='utf-8'
    ).read()
    assert '_enqueue_reuse' in src
    assert 'ASSEMBLER_STREAM' in src
    assert 'INSERT INTO analysis_results' not in src, (
        "ingestion must not write analysis_results directly — the assembler owns "
        "the three-store fan-out"
    )


def test_the_assembler_composes_rather_than_copies():
    asm = open(
        '/home/bk/code/defense/services/workers/assembler/assembler.py', encoding='utf-8'
    ).read()
    assert 'build_reused_result' in asm
    assert 'reused_result' in asm


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-v"]))

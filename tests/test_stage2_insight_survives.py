"""Stage-2's insight task must reach the canonical result.

``stage2_llm/worker._run_insight`` spends a real LLM call — up to
``INSIGHT_MAX_TOKENS`` per routed post — producing ``refined_topics``,
``intents`` and a one-line ``insight``. The assembler used to read ``topics``
and ``intents`` from ``stage1_result`` only and never read ``insight`` at all,
which was also absent from ``output_schema.json``. The whole task's output was
therefore paid for and discarded before it reached the API, Postgres,
ClickHouse or the dashboard, while FEATURES.md listed it as a working feature.

Same shape as tests/test_provenance_survives.py: pin the fields at the boundary
where they were being dropped.
"""

from __future__ import annotations

import pytest

from services.workers.assembler.builder import build_canonical_result


def _normalized_post() -> dict:
    return {
        "post_id": "post-insight-1",
        "campaign_id": "camp-1",
        "platform": "facebook",
        "platform_post_id": "pp-1",
        "media_type": "TEXT",
        "caption": "একটি পরীক্ষামূলক পোস্ট",
        "created_at": "2026-01-01T00:00:00Z",
        "scraped_at": "2026-01-01T00:05:00Z",
        "engagement": {"comment_count": 0, "stored_comments": 0, "total_reactions": 0},
        "baseline_sentiment": 0.1,
    }


def _stage1_result() -> dict:
    return {
        "language": "bn",
        "overall_sentiment": "neutral",
        "sentiment_score": 0.0,
        "topics": ["stage1-topic"],
        "intents": ["stage1-intent"],
        "confidence": 0.9,
        "comment_analysis": {
            "analyzed": 0,
            "coverage": 1.0,
            "comments": [],
            "sentiment_breakdown": {"positive": 0, "negative": 0, "neutral": 0},
        },
        "processing": {"stage1_ms": 12.0},
    }


def test_stage2_insight_reaches_the_canonical_result():
    """The one-line insight is a top-level field, not a dropped intermediate."""
    result = build_canonical_result(
        _normalized_post(),
        _stage1_result(),
        {
            "topics": ["refined-a", "refined-b"],
            "intents": ["refined-intent"],
            "insight": "Commenters are frustrated about the delay.",
            "processing": {"stage2_ms": 900},
        },
    )

    assert result["insight"] == "Commenters are frustrated about the delay."
    # Stage 2's refinements win over Stage 1's, exactly like post_type does.
    assert result["topics"] == ["refined-a", "refined-b"]
    assert result["intents"] == ["refined-intent"]


def test_stage1_labels_stand_when_stage2_is_skipped():
    result = build_canonical_result(_normalized_post(), _stage1_result(), None)

    assert result["insight"] is None
    assert result["topics"] == ["stage1-topic"]
    assert result["intents"] == ["stage1-intent"]


@pytest.mark.parametrize(
    "stage2",
    [
        {"processing": {}},                                     # insight task didn't run
        {"topics": [], "intents": [], "insight": "", "processing": {}},   # LLM declined
        {"topics": None, "intents": None, "insight": None, "processing": {}},
    ],
)
def test_empty_stage2_refinements_do_not_erase_stage1_labels(stage2):
    """An empty refinement means "no opinion", not "delete Stage 1's labels"."""
    result = build_canonical_result(_normalized_post(), _stage1_result(), stage2)

    assert result["topics"] == ["stage1-topic"]
    assert result["intents"] == ["stage1-intent"]
    assert result["insight"] is None


def test_insight_is_declared_in_the_output_schema():
    """A field the schema does not know about is a field consumers cannot rely on."""
    import json
    from pathlib import Path

    schema_path = (
        Path(__file__).resolve().parents[1]
        / "src" / "defense" / "contracts" / "schemas" / "output_schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))

    assert "insight" in schema["properties"], (
        "output_schema.json must declare `insight` — the assembler now emits it"
    )
    assert "null" in schema["properties"]["insight"]["type"]

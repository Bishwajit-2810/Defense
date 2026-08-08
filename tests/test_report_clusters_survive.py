"""Regression tests: the report layer's embedding clusters must reach a consumer.

PROJECT_ASSESSMENT §13.1. `reports._embedding_clusters` is the LLM cost lever
architecture.md §5 is named after: cluster the corpus and spend **one LLM-B call
per cluster** instead of one per post. It ran on every grounded report, wrote its
summaries into `jobs.options`, and then:

  * `ReportResponse` declared no `embedding_clusters` field, so FastAPI's
    `response_model` stripped it from `POST /v1/reports`;
  * `_row_to_report` never read the key, so `GET /v1/reports` and
    `GET /v1/reports/{id}` did not return it either;
  * `dashboard/app.js` never mentioned it.

Real spend, no output — the same defect as §11.1's discarded Stage-2 `insight`,
one layer up. So these tests assert from the CONSUMER end: what a caller of the
API can actually see. A test of `_embedding_clusters` itself would have passed
throughout, which is exactly why the bug survived.
"""

import sys

sys.path.insert(0, '/home/bk/code/defense/src/defense')
sys.path.insert(0, '/home/bk/code/defense/src/defense/services/api')
sys.path.insert(0, '/home/bk/code/defense/src/defense/services/api/routers')

import pytest

from models import ReportResponse
from routers.reports import _row_to_report

_CLUSTERS = [
    {"cluster_id": "emb-0", "size": 12, "top_sentiment": "negative",
     "representative_post_id": "p-1", "summary": "Anger about fuel prices."},
    {"cluster_id": "emb-1", "size": 4, "top_sentiment": "neutral",
     "representative_post_id": "p-9", "summary": "Routine traffic updates."},
]


def _row(content: dict) -> dict:
    return {
        "id": "r-1",
        "selector": {"campaign_id": "c1"},
        "options": {"type": "trend", "title": "T", "result": content},
        "status": "done",
        "created_at": None,
        "updated_at": None,
        "error": None,
    }


# ---------------------------------------------------------------------------
# The response model must be able to carry them at all
# ---------------------------------------------------------------------------


def test_report_response_declares_embedding_clusters():
    """The absence of this field WAS the bug: response_model drops what it does
    not declare, silently and with no error anywhere."""
    assert "embedding_clusters" in ReportResponse.model_fields
    assert "embedding_clusters_are_stub" in ReportResponse.model_fields


def test_embedding_clusters_survive_a_response_model_round_trip():
    resp = ReportResponse(
        id="r-1", campaign_id="c1", status="done",
        embedding_clusters=_CLUSTERS,
    )
    dumped = resp.model_dump()
    assert dumped["embedding_clusters"] == _CLUSTERS
    assert len(dumped["embedding_clusters"]) == 2


# ---------------------------------------------------------------------------
# ...and the reader must actually read them back
# ---------------------------------------------------------------------------


def test_row_to_report_reads_the_clusters_back_out():
    """`GET /v1/reports/{id}` and `GET /v1/reports` both go through this."""
    out = _row_to_report(_row({"embedding_clusters": _CLUSTERS}))
    assert out.embedding_clusters == _CLUSTERS


def test_row_to_report_is_unbothered_by_a_report_without_them():
    """Reports created before this landed, or with grounded=false."""
    out = _row_to_report(_row({"summary": "x", "clusters": []}))
    assert out.embedding_clusters is None
    assert out.embedding_clusters_are_stub is False


def test_the_cheap_and_expensive_cluster_lists_stay_distinct():
    """`clusters` is the zero-LLM SQL topic aggregate; `embedding_clusters` is
    the one that costs a call per cluster. Rendering one as the other would hide
    the spend all over again."""
    content = {
        "clusters": [{"cluster_id": "topic-0", "label": "fuel", "size": 3}],
        "embedding_clusters": _CLUSTERS,
    }
    out = _row_to_report(_row(content))
    assert out.clusters is not None and out.embedding_clusters is not None
    assert out.clusters != out.embedding_clusters
    assert out.clusters[0]["label"] == "fuel"
    assert out.embedding_clusters[0]["cluster_id"] == "emb-0"


# ---------------------------------------------------------------------------
# Honesty about what was clustered
# ---------------------------------------------------------------------------


def test_stub_backed_clusters_are_disclosed():
    """A hash vector is not semantic, so these clusters group posts arbitrarily.
    Surfacing the summaries without saying so would present noise as a finding
    (§13.2)."""
    out = _row_to_report(_row({
        "embedding_clusters": _CLUSTERS,
        "embedding_clusters_are_stub": True,
    }))
    assert out.embedding_clusters_are_stub is True


def test_the_dashboard_renders_the_expensive_clusters():
    """The §11.1/§12.4d lesson: reaching the API is not reaching a reader.

    `insight` was carried to the API by §11.1 and still sat unlabelled in a
    bottom metadata dump until §12.4d. Assert the last hop here rather than
    discovering it in a later pass.
    """
    app_js = open('/home/bk/code/defense/dashboard/app.js', encoding='utf-8').read()
    assert 'rep.embedding_clusters' in app_js
    assert 'embedding_clusters_are_stub' in app_js


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-v"]))

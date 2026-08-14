"""Tests for analysis report generation, HTML/PDF rendering, and export endpoints."""

import sys
import pytest

sys.path.insert(0, '/home/bk/code/defense/src/defense')
sys.path.insert(0, '/home/bk/code/defense/src/defense/services/api')
sys.path.insert(0, '/home/bk/code/defense/src/defense/services/api/routers')

from routers.reports import _report_to_html


def test_report_to_html_renders_executive_summary_and_metrics():
    report_data = {
        "id": "rep-12345",
        "title": "Mass Reaction Analysis",
        "campaign_id": "campaign_alpha",
        "summary": "Mass reaction is overall neutral with 60% neutral sentiment.",
        "summary_source": "llm",
        "period": "2026-08-01 → 2026-08-14",
        "metrics": {
            "total_posts": 50,
            "sentiment_breakdown": {
                "positive": 10,
                "negative": 10,
                "neutral": 30,
            },
            "languages": {"bn": 40, "en": 10},
        },
        "clusters": [
            {"cluster_id": "topic-0", "label": "Elections", "size": 25, "top_sentiment": "neutral"},
        ],
        "embedding_clusters": [
            {"cluster_id": "emb-0", "size": 15, "top_sentiment": "positive", "summary": "Public reaction to policy announcement."},
        ],
    }

    html = _report_to_html(report_data)

    assert "Mass Reaction Analysis" in html
    assert "Executive Summary — Mass Reaction Analysis" in html
    assert "Mass reaction is overall neutral" in html
    assert "50" in html
    assert "Elections" in html
    assert "Public reaction to policy announcement" in html


def test_row_to_report_handles_serialized_json_strings_and_missing_keys():
    from routers.reports import _row_to_report

    row = {
        "id": "rep-999",
        "status": "done",
        "selector": '{"campaign_id": "c1"}',
        "options": '{"title": "Test Title", "type": "mass_reaction", "result": {"summary": "Done"}}',
        "created_at": "2026-08-14T00:00:00Z",
    }

    rep = _row_to_report(row)
    assert rep.id == "rep-999"
    assert rep.campaign_id == "c1"
    assert rep.title == "Test Title"
    assert rep.summary == "Done"


def test_export_latest_alias_routes():
    from fastapi.testclient import TestClient
    import defense.services.api.main as main
    from defense.services.api.deps import get_current_user, get_db, get_redis, rate_limit

    class FakeRedis:
        async def get(self, key): return None

    class FakeDb:
        async def execute(self, *args, **kwargs):
            class _Result:
                def first(s): return {"total_posts": 0, "first_at": None, "last_at": None}
                def all(s): return []
                def mappings(s): return s
            return _Result()

    app = main.app
    app.dependency_overrides[get_redis] = lambda: FakeRedis()
    app.dependency_overrides[get_db] = lambda: FakeDb()
    app.dependency_overrides[get_current_user] = lambda: {"sub": "tester", "tenant_id": "default"}
    app.dependency_overrides[rate_limit] = lambda: None

    client = TestClient(app)
    try:
        # Both /v1/reports/export_latest and /v1/reports/export_latest/export must resolve without 404
        r1 = client.get("/v1/reports/export_latest?campaign_id=all&format=html")
        assert r1.status_code == 200

        r2 = client.get("/v1/reports/export_latest/export?format=html")
        assert r2.status_code == 200
    finally:
        app.dependency_overrides.clear()



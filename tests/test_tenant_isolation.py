"""Regression and contract tests for Pass 9 multi-tenant isolation.

Verifies:
1. Endpoint-level tenant isolation: tenant B cannot read, list, export, or delete tenant A's data.
2. DELETE /v1/posts/{post_id} executes ownership check and delete queries scoped by tenant_id.
3. Static query contract: every function querying `analysis_results` across routers and retrieval MCP includes a `tenant_id` / `tid` predicate.
"""

from __future__ import annotations

import ast
import glob
from pathlib import Path
from typing import AsyncGenerator

import pytest
from httpx import ASGITransport, AsyncClient

from defense.services.api.main import app
from defense.services.api.deps import get_current_user, get_db

REPO = Path(__file__).resolve().parents[1]
PKG = REPO / "src" / "defense"

# ---------------------------------------------------------------------------
# Deliverable 5: Static Query Contract Test
# ---------------------------------------------------------------------------


def test_all_analysis_results_queries_are_tenant_scoped():
    """Scan all API routers and retrieval MCP server files to enforce that every
    function referencing `analysis_results` contains a `tenant_id` or `tid` predicate.
    """
    router_files = glob.glob(str(PKG / "services" / "api" / "routers" / "*.py"))
    mcp_files = glob.glob(str(PKG / "mcp_servers" / "retrieval_mcp" / "*.py"))
    
    files = [Path(f) for f in router_files + mcp_files]
    assert len(files) > 0, "No router or MCP files found to scan"

    unscoped_queries = []

    for file_path in files:
        tree = ast.parse(file_path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                fn_source = ast.get_source_segment(file_path.read_text(encoding="utf-8"), node) or ""
                if "analysis_results" in fn_source:
                    if "tenant_id" not in fn_source and "tid" not in fn_source:
                        unscoped_queries.append(f"{file_path.name}:{node.name}")

    assert not unscoped_queries, (
        f"Found functions referencing analysis_results without tenant_id predicate:\n"
        + "\n".join(unscoped_queries)
    )


# ---------------------------------------------------------------------------
# Deliverable 4: Endpoint Tenant Isolation Tests
# ---------------------------------------------------------------------------


class MockMappingsResult:
    def __init__(self, rows, rowcount=0):
        self._rows = rows
        self._rowcount = rowcount or len(rows)

    def mappings(self):
        return self

    def all(self):
        return self._rows

    def first(self):
        return self._rows[0] if self._rows else None

    @property
    def rowcount(self):
        return self._rowcount


class FakeTenantDbSession:
    """In-memory mock session enforcing tenant filtering matching API router logic."""

    async def execute(self, statement, params=None):
        sql = str(statement)
        params = params or {}
        tid = params.get("tid") or params.get("tenant_id")

        # Handle ownership check / select queries
        if "SELECT 1 FROM posts" in sql:
            if tid == "tenant_a" and params.get("pid") == "post_a":
                return MockMappingsResult([{"1": 1}])
            return MockMappingsResult([])

        if "FROM jobs" in sql:
            if params.get("id") == "job_a":
                if tid == "tenant_a":
                    return MockMappingsResult([{
                        "id": "job_a", "type": "analysis_run", "status": "done",
                        "selector": {"tenant_id": "tenant_a", "campaign_id": "camp_a"},
                        "created_at": None, "updated_at": None, "error": None,
                    }])
                return MockMappingsResult([])
            if params.get("id") == "rep_a":
                if tid == "tenant_a":
                    return MockMappingsResult([{
                        "id": "rep_a", "type": "report", "status": "done",
                        "selector": {"tenant_id": "tenant_a", "campaign_id": "camp_a"},
                        "options": {"title": "Test Report", "type": "trend"},
                        "created_at": None, "updated_at": None, "error": None,
                    }])
                return MockMappingsResult([])

            # List jobs / reports
            if tid == "tenant_a":
                return MockMappingsResult([{
                    "id": "job_a", "type": "analysis_run", "status": "done",
                    "selector": {"tenant_id": "tenant_a"}, "created_at": None, "updated_at": None,
                }])
            return MockMappingsResult([])

        if "FROM analysis_results" in sql:
            if params.get("pid") == "post_a" or params.get("post_id") == "post_a":
                if tid == "tenant_a":
                    return MockMappingsResult([{
                        "id": "res_a", "post_id": "post_a", "campaign_id": "camp_a",
                        "result": {"overall_sentiment": "positive", "topics": ["tech"], "comment_analysis": {"comments": []}},
                        "created_at": None, "scraped_at": None,
                    }])
                return MockMappingsResult([])

            if tid == "tenant_a":
                if "GROUP BY" in sql:
                    return MockMappingsResult([{"k": "positive", "c": 1, "topic": "tech", "key": "joy", "value": "1"}])
                return MockMappingsResult([{
                    "id": "res_a", "post_id": "post_a", "campaign_id": "camp_a",
                    "result": {"overall_sentiment": "positive", "topics": ["tech"]},
                    "snippet": "positive post",
                    "created_at": None, "scraped_at": None, "total": 1, "llm_used": 0, "with_summary": 0,
                    "analyzed": 1, "reported": 1, "anomalies": 0,
                    "posts_analyzed": 1, "llm_calls": 0,
                }])

            # Tenant B: empty data
            if "GROUP BY" in sql or "latest" in sql or "ar.id, ar.post_id" in sql or "WHERE (LOWER" in sql or "WHERE ar.embedding" in sql or "LIKE :pattern" in sql:
                return MockMappingsResult([])
            if "COUNT(*)" in sql or "count(*)" in sql:
                return MockMappingsResult([{
                    "total": 0, "llm_used": 0, "with_summary": 0, "analyzed": 0, "reported": 0, "anomalies": 0,
                    "posts_analyzed": 0, "llm_calls": 0, "n": 0,
                }])
            return MockMappingsResult([])

        if "DELETE FROM" in sql:
            if tid == "tenant_a":
                return MockMappingsResult([], rowcount=1)
            return MockMappingsResult([], rowcount=0)

        return MockMappingsResult([])

    async def commit(self):
        pass

    async def rollback(self):
        pass


@pytest.mark.asyncio
async def test_tenant_b_cannot_access_tenant_a_data():
    """Integrity test verifying cross-tenant isolation on all scoped endpoints."""
    fake_session = FakeTenantDbSession()

    async def override_get_db() -> AsyncGenerator[FakeTenantDbSession, None]:
        yield fake_session

    current_tenant = {"tenant_id": "tenant_b", "sub": "user_b"}

    async def override_get_current_user() -> dict:
        return current_tenant

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_get_current_user

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            # 1. Tenant B tries to GET tenant A's analysis job -> 404
            res = await client.get("/v1/analysis/job_a")
            assert res.status_code == 404

            # 2. Tenant B tries to GET tenant A's post comments -> 404
            res = await client.get("/v1/analysis/post/post_a/comments")
            assert res.status_code == 404

            # 3. Tenant B tries to GET tenant A's report -> 404
            res = await client.get("/v1/reports/rep_a")
            assert res.status_code == 404

            # 4. Tenant B lists analysis jobs -> 0 jobs returned
            res = await client.get("/v1/analysis")
            assert res.status_code == 200
            assert res.json()["total"] == 0

            # 5. Tenant B gets latest analysis -> 0 results
            res = await client.get("/v1/analysis/latest")
            assert res.status_code == 200
            assert len(res.json()["results"]) == 0

            # 6. Tenant B gets overview -> 0 total posts
            res = await client.get("/v1/analysis/overview")
            assert res.status_code == 200
            assert res.json()["total_posts"] == 0

            # 7. Tenant B lists reports -> 0 reports returned
            res = await client.get("/v1/reports")
            assert res.status_code == 200
            assert len(res.json()) == 0

            # 8. Tenant B runs keyword search -> 0 results
            res = await client.get("/v1/search?q=positive")
            assert res.status_code == 200
            assert res.json()["total"] == 0

            # 9. Tenant B gets usage -> 0 posts analyzed
            res = await client.get("/v1/usage")
            assert res.status_code == 200
            assert res.json()["posts_analyzed"] == 0

            # 10. Tenant B attempts DELETE post_a -> 404 (does not touch tenant A's data)
            res = await client.delete("/v1/posts/post_a")
            assert res.status_code == 404

            # Now switch user to Tenant A to verify DELETE works against schema
            current_tenant["tenant_id"] = "tenant_a"
            res = await client.delete("/v1/posts/post_a")
            assert res.status_code == 200
            assert res.json()["post_id"] == "post_a"
            assert res.json()["deleted"]["posts"] == 1

    finally:
        app.dependency_overrides.clear()

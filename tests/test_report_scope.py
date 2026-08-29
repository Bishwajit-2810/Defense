"""A report must cover the posts it claims to cover.

The Jobs tab's per-row "Download Report" passed only the job's `campaign_id`.
Every job created by `POST /v1/analysis/run` with explicit `post_ids` has no
campaign, so the request fell through to `campaign_id=all` and the endpoint
aggregated the whole tenant corpus: a job that analysed **one** post downloaded
a report on fifty, under a filename that said otherwise. Nothing in the PDF
contradicted it either — the scope was never printed.

These tests pin the scope end to end: the SQL filter, the label, the rendered
header, and the job -> post_ids resolution the endpoint now does.
"""

import sys
import pytest

sys.path.insert(0, '/home/bk/code/defense/src/defense')
sys.path.insert(0, '/home/bk/code/defense/src/defense/services/api')
sys.path.insert(0, '/home/bk/code/defense/src/defense/services/api/routers')

from routers.reports import _generate_report_content, _report_to_html, export_latest_report


class Row(dict):
    """A mappings() row: a missing column reads as None, as psycopg's does not."""
    def __missing__(self, key):
        return None


class FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self

    def first(self):
        return self._rows[0] if self._rows else None

    def all(self):
        return self._rows


class RecordingDB:
    """Captures every (sql, params) the report aggregation issues."""

    def __init__(self, head=None, rows=None):
        self.calls = []
        self._head = head if head is not None else Row(total_posts=3, first_at=None, last_at=None)
        self._rows = rows or []

    async def execute(self, statement, params=None):
        sql = str(statement)
        self.calls.append((sql, params or {}))
        # The two aggregate-shaped queries read .first(); the rest read .all().
        if "total_posts" in sql or "avg_tox" in sql:
            return FakeResult([self._head])
        return FakeResult(self._rows)

    @property
    def sqls(self):
        return [sql for sql, _ in self.calls]


POSTS = ["cmor32gy000000000000000a", "cmor32gy000000000000000b"]


@pytest.mark.asyncio
async def test_post_scoped_report_filters_every_aggregate():
    db = RecordingDB()

    await _generate_report_content(db, "all", tenant_id="default", post_ids=POSTS)

    assert db.calls, "the aggregation issued no queries at all"
    for sql, params in db.calls:
        # One unfiltered aggregate is enough to report the whole corpus in a
        # report that claims to be about two posts.
        assert "ar.post_id = ANY(:pids)" in sql, f"unscoped aggregate:\n{sql}"
        assert params.get("pids") == POSTS


@pytest.mark.asyncio
async def test_unscoped_report_is_unchanged():
    db = RecordingDB()

    await _generate_report_content(db, "all", tenant_id="default")

    for sql, params in db.calls:
        assert "ar.post_id" not in sql.split("WHERE")[-1] or "ANY(:pids)" not in sql
        assert "pids" not in params


@pytest.mark.asyncio
async def test_campaign_and_post_scope_compose():
    db = RecordingDB()

    await _generate_report_content(db, "camp-1", tenant_id="default", post_ids=POSTS)

    for sql, params in db.calls:
        assert "ar.campaign_id = :cid" in sql
        assert "ar.post_id = ANY(:pids)" in sql
        assert params.get("cid") == "camp-1"


@pytest.mark.asyncio
async def test_scope_label_names_the_posts_not_all_campaigns():
    db = RecordingDB()

    content = await _generate_report_content(db, "all", tenant_id="default", post_ids=POSTS)

    assert content["scope_label"] == "2 selected post(s)"
    assert content["scope_post_ids"] == POSTS
    # The narrative the LLM is handed, and the aggregate fallback, must not
    # describe a two-post report as covering everything.
    assert "all campaigns" not in content["summary"]
    assert "2 selected post(s)" in content["summary"]


@pytest.mark.asyncio
async def test_unscoped_report_still_says_all_campaigns():
    db = RecordingDB()

    content = await _generate_report_content(db, "all", tenant_id="default")

    assert content["scope_label"] == "all campaigns"
    assert content["scope_post_ids"] is None


def test_rendered_report_states_its_scope():
    html = _report_to_html({
        "id": "rep-1",
        "title": "Mass Reaction Analysis",
        "campaign_id": "all",
        "scope_label": "1 selected post(s)",
        "summary": "One post.",
        "metrics": {"total_posts": 1},
    })

    # A one-post report and a whole-corpus report used to be indistinguishable
    # once the PDF was open.
    assert "Scope: 1 selected post(s)" in html


def test_rendered_report_falls_back_for_older_stored_reports():
    html = _report_to_html({
        "id": "rep-0",
        "title": "Mass Reaction Analysis",
        "campaign_id": "campaign_alpha",
        "summary": "Older report with no scope_label stored.",
        "metrics": {"total_posts": 50},
    })

    assert "Scope: campaign campaign_alpha" in html


# ---------------------------------------------------------------------------
# export_latest_report: job -> post_ids
# ---------------------------------------------------------------------------


class JobDB:
    def __init__(self, selector, found=True):
        self._selector = selector
        self._found = found
        self.queries = []

    async def execute(self, statement, params=None):
        sql = str(statement)
        self.queries.append((sql, params or {}))
        if "FROM jobs" in sql and "type = 'report'" not in sql:
            return FakeResult([Row(selector=self._selector)] if self._found else [])
        return FakeResult([])       # no cached report


@pytest.fixture
def capture_report(monkeypatch):
    """Stop before the LLM/PDF and record the ReportRequest that was built."""
    import routers.reports as reports

    captured = {}

    async def fake_create_report(body, request, db=None, redis=None, current_user=None):
        captured["request"] = body
        return type("R", (), {"report_id": "rep-x"})()

    async def fake_export_report(report_id, format="pdf", db=None, current_user=None):
        captured["exported"] = report_id
        return "PDF"

    monkeypatch.setattr(reports, "create_report", fake_create_report)
    monkeypatch.setattr(reports, "export_report", fake_export_report)
    return captured


@pytest.mark.asyncio
async def test_job_id_scopes_the_report_to_that_jobs_posts(capture_report):
    db = JobDB({"tenant_id": "default", "post_ids": POSTS})

    await export_latest_report(
        campaign_id="all", job_id="1be7e085-0000-0000-0000-000000000000",
        type="mass_reaction", format="pdf", db=db, redis=None,
        current_user={"tenant_id": "default"},
    )

    req = capture_report["request"]
    assert req.post_ids == POSTS, "the job's posts never reached the report request"
    assert "post(s) from job 1be7e085" in req.title


@pytest.mark.asyncio
async def test_job_id_accepts_a_selector_stored_as_json_text(capture_report):
    db = JobDB('{"tenant_id": "default", "post_ids": ["cmor32gy000000000000000a"]}')

    await export_latest_report(
        campaign_id="all", job_id="job-1", type="mass_reaction", format="pdf",
        db=db, redis=None, current_user={"tenant_id": "default"},
    )

    assert capture_report["request"].post_ids == ["cmor32gy000000000000000a"]


@pytest.mark.asyncio
async def test_a_campaign_job_still_scopes_by_campaign(capture_report):
    db = JobDB({"tenant_id": "default", "campaign_id": "camp-7"})

    await export_latest_report(
        campaign_id="all", job_id="job-2", type="mass_reaction", format="pdf",
        db=db, redis=None, current_user={"tenant_id": "default"},
    )

    req = capture_report["request"]
    assert req.post_ids is None
    assert req.campaign_id == "camp-7"


@pytest.mark.asyncio
async def test_the_cached_report_lookup_is_keyed_on_the_post_scope(capture_report):
    """Reusing a 5-minute-old corpus report for a one-post request is the same
    bug wearing a different hat — the right filename over the wrong PDF."""
    db = JobDB({"tenant_id": "default", "post_ids": POSTS})

    await export_latest_report(
        campaign_id="all", job_id="job-3", type="mass_reaction", format="pdf",
        db=db, redis=None, current_user={"tenant_id": "default"},
    )

    cache_query = next(sql for sql, _ in db.queries if "type = 'report'" in sql)
    assert "post_ids" in cache_query
    params = next(p for sql, p in db.queries if "type = 'report'" in sql)
    assert params["pids"] == '["cmor32gy000000000000000a", "cmor32gy000000000000000b"]'


@pytest.mark.asyncio
async def test_an_unknown_job_is_a_404_not_a_corpus_report(capture_report):
    from fastapi import HTTPException

    db = JobDB(None, found=False)

    with pytest.raises(HTTPException) as exc:
        await export_latest_report(
            campaign_id="all", job_id="nope", type="mass_reaction", format="pdf",
            db=db, redis=None, current_user={"tenant_id": "default"},
        )
    assert exc.value.status_code == 404
    assert "request" not in capture_report

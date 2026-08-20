"""Stop / resume / delete for analysis jobs, and the places in the pipeline that read them.

A job is not a process: it is N envelopes spread across four Redis streams, so
"Stop" can only be cooperative. These tests pin the two properties that make the
feature real rather than cosmetic:

* the flag is checked BEFORE the expensive work in each stage, and a cancelled
  envelope produces no downstream message;
* a stopped job stays stopped — nothing in the pipeline or the API can write the
  jobs row back to running/done behind the operator's back;
* resume re-enqueues ONLY what an interrupted job never finished, derived from
  Postgres alone, because the Redis counters do not survive a power cut.
"""

import asyncio
import json
import pathlib
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src" / "defense"))

from defense.libs.jobs import (  # noqa: E402
    CANCEL_TTL,
    PURGEABLE_KEY_TEMPLATES,
    cancel_key,
    is_cancelled,
    mark_cancelled,
    purge_job_keys,
)

SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "defense"


class FakeRedis:
    """Keys, streams and pub/sub — enough for the cancellation paths."""

    def __init__(self):
        self.keys: dict[str, str] = {}
        self.ttls: dict[str, int] = {}
        self.streams: dict[str, list] = {}
        self.published: list[tuple[str, str]] = []
        self.acked: list = []
        self.fail = False
        self._seq = 0

    def _check(self):
        if self.fail:
            raise ConnectionError("redis is down")

    async def set(self, key, value, ex=None):
        self._check()
        self.keys[key] = value
        if ex:
            self.ttls[key] = ex
        return True

    async def get(self, key):
        self._check()
        return self.keys.get(key)

    async def exists(self, key):
        self._check()
        return 1 if key in self.keys else 0

    async def delete(self, *keys):
        self._check()
        n = 0
        for k in keys:
            if self.keys.pop(k, None) is not None:
                n += 1
        return n

    async def incr(self, key):
        self._check()
        self.keys[key] = str(int(self.keys.get(key, 0)) + 1)
        return int(self.keys[key])

    async def expire(self, key, ttl):
        self.ttls[key] = ttl
        return True

    async def rpush(self, key, value):
        self.keys.setdefault(key, [])
        return 1

    async def ltrim(self, key, a, b):
        return True

    async def publish(self, channel, message):
        self.published.append((channel, message))
        return 1

    async def xadd(self, stream, fields):
        self._seq += 1
        self.streams.setdefault(stream, []).append((f"{self._seq}-0", dict(fields)))
        return f"{self._seq}-0"

    async def xack(self, stream, group, msg_id):
        self.acked.append((stream, group, msg_id))


# ---------------------------------------------------------------------------
# libs/jobs.py
# ---------------------------------------------------------------------------


def test_flag_round_trip_and_ttl():
    r = FakeRedis()

    assert asyncio.run(is_cancelled(r, "job-1")) is False
    assert asyncio.run(mark_cancelled(r, "job-1", reason="stopped via API")) is True
    assert asyncio.run(is_cancelled(r, "job-1")) is True

    # Bounded lifetime: the flag has to outlive the job row but not linger forever.
    assert r.ttls[cancel_key("job-1")] == CANCEL_TTL
    assert r.keys[cancel_key("job-1")] == "stopped via API"

    # One job's stop must not touch its neighbours.
    assert asyncio.run(is_cancelled(r, "job-2")) is False


def test_no_job_id_is_not_cancelled():
    """Posts XADDed by hand or replayed carry no job — they must still run."""
    r = FakeRedis()
    assert asyncio.run(is_cancelled(r, None)) is False
    assert asyncio.run(is_cancelled(r, "")) is False
    assert asyncio.run(mark_cancelled(r, "")) is False


def test_redis_failure_fails_open_for_reads_and_closed_for_writes():
    r = FakeRedis()
    r.fail = True

    # A read failure must mean "process this post", never "silently drop it".
    assert asyncio.run(is_cancelled(r, "job-1")) is False
    # A write failure must be reported, so the API cannot claim a stop the
    # workers were never told about.
    assert asyncio.run(mark_cancelled(r, "job-1")) is False


def test_purge_clears_progress_but_keeps_the_stop_flag():
    """Deleting a job must not un-stop its in-flight envelopes."""
    r = FakeRedis()
    asyncio.run(mark_cancelled(r, "job-1"))
    for tmpl in PURGEABLE_KEY_TEMPLATES:
        r.keys[tmpl.format(job_id="job-1")] = "x"

    removed = asyncio.run(purge_job_keys(r, "job-1"))

    assert removed == len(PURGEABLE_KEY_TEMPLATES)
    assert cancel_key("job-1") not in PURGEABLE_KEY_TEMPLATES
    assert asyncio.run(is_cancelled(r, "job-1")) is True


# ---------------------------------------------------------------------------
# The router: a cancelled envelope produces no Stage-2 message
# ---------------------------------------------------------------------------


def _router_envelope(job_id):
    return {
        b"data": json.dumps(
            {
                "post_id": "p1",
                "job_id": job_id,
                "normalized_post": {"post_id": "p1"},
                "stage1_result": {"post_id": "p1", "caption": "hello", "language": "en"},
                "options": {},
            }
        ).encode()
    }


def test_router_drops_a_cancelled_job_before_stage2():
    import defense.services.workers.router.router as router

    r = FakeRedis()
    asyncio.run(mark_cancelled(r, "job-1"))

    asyncio.run(router._process_message(r, b"1-0", _router_envelope("job-1")))

    # Nothing enqueued for Stage 2 — that is where the LLM spend is.
    assert router.STAGE2_QUEUE not in r.streams
    # …and no progress frame claiming the router did work.
    assert r.published == []


def test_router_still_routes_an_uncancelled_job():
    """Guard against the check being written so broadly it stops everything."""
    import defense.services.workers.router.router as router

    r = FakeRedis()
    asyncio.run(router._process_message(r, b"1-0", _router_envelope("job-2")))

    assert len(r.streams.get(router.STAGE2_QUEUE, [])) == 1


# ---------------------------------------------------------------------------
# Placement: the check has to sit before the work in every stage
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "rel,check,work",
    [
        # Ingestion must bail before it upserts the post and enqueues it — a stop
        # that lets a job keep writing posts is not what the button says.
        ("services/ingestion/service.py", "is_cancelled(redis, job_id)", "await _upsert_post("),
        # Stage 1 must bail before it runs the models on the post.
        ("services/workers/stage1_nlp/worker.py", "is_cancelled(redis, job_id)", "await _process_message("),
        # The router must bail before it enqueues to Stage 2.
        ("services/workers/router/router.py", "is_cancelled(redis, job_id)", "await redis.xadd(STAGE2_QUEUE"),
        # Stage 2 must bail before the post-level and comment lanes.
        ("services/workers/stage2_llm/worker.py", "is_cancelled(redis, job_id)", "async def _post_level_lane("),
    ],
)
def test_every_stage_checks_before_it_works(rel, check, work):
    text = (SRC / rel).read_text(encoding="utf-8")
    assert check in text, f"{rel} never checks the stop flag"
    assert text.index(check) < text.index(work), (
        f"{rel} checks the stop flag after the work it is supposed to skip"
    )


def test_the_assembler_does_not_drop_work_already_paid_for():
    """It is the one stage with no stop check, and that is deliberate.

    A post that reaches the assembler has had every model run on it; dropping it
    there would throw away the spend without saving any. The assembler's job is
    to persist it and leave the cancelled status alone.
    """
    text = (SRC / "services/workers/assembler/assembler.py").read_text(encoding="utf-8")
    assert "is_cancelled" not in text


def test_assembler_cannot_reopen_a_cancelled_job():
    """The last in-flight post lands after the stop; it must not un-cancel the row."""
    text = (SRC / "services/workers/assembler/assembler.py").read_text(encoding="utf-8")
    update = text.split("UPDATE jobs", 1)[1].split('"""', 1)[0]
    assert "status <> 'cancelled'" in update


def test_api_treats_cancelled_as_terminal():
    """Otherwise the counter reconciliation in GET /v1/analysis/{id} revives it."""
    from defense.services.api.routers import analysis

    assert "cancelled" in analysis._TERMINAL_JOB_STATUSES
    # The SQL guards are rendered from the same set, so they cannot drift.
    assert "'cancelled'" in analysis._TERMINAL_STATUS_SQL

    text = (SRC / "services/api/routers/analysis.py").read_text(encoding="utf-8")
    # A stopped job publishes no further frames, so the SSE loop must break on it.
    assert 'if event_type in ("done", "error", "cancelled"):' in text


# ---------------------------------------------------------------------------
# The endpoints, against the real routes
# ---------------------------------------------------------------------------


class _Result:
    """Just enough of a SQLAlchemy Result for these two handlers."""

    def __init__(self, rows, rowcount=0):
        self._rows = rows
        self.rowcount = rowcount

    def mappings(self):
        return self

    def first(self):
        return self._rows[0] if self._rows else None

    def all(self):
        return list(self._rows)


class FakeDb:
    """Answers the job lookup and records every statement it is handed."""

    def __init__(self, job_row=None, delete_rowcount=1):
        self.job_row = job_row
        self.delete_rowcount = delete_rowcount
        self.statements: list[tuple[str, dict]] = []
        self.commits = 0

    async def execute(self, stmt, params=None):
        sql = " ".join(str(stmt).split())
        self.statements.append((sql, params or {}))
        if sql.startswith("SELECT id, status, selector, options, created_at, updated_at FROM jobs"):
            return _Result([self.job_row] if self.job_row else [])
        if sql.startswith("DELETE FROM jobs"):
            return _Result([], rowcount=self.delete_rowcount)
        return _Result([])

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        pass


def _client(db, redis):
    from fastapi.testclient import TestClient

    import defense.services.api.main as main
    from defense.services.api.deps import get_current_user, get_db, get_redis, rate_limit

    async def _db():
        return db

    async def _redis():
        return redis

    async def _user():
        return {"sub": "tester", "tenant_id": "default", "auth_method": "test"}

    main.app.dependency_overrides[get_db] = _db
    main.app.dependency_overrides[get_redis] = _redis
    main.app.dependency_overrides[get_current_user] = _user
    main.app.dependency_overrides[rate_limit] = lambda: None
    return TestClient(main.app)


@pytest.fixture
def _no_overrides():
    yield
    import defense.services.api.main as main

    main.app.dependency_overrides.clear()


def test_cancel_endpoint_raises_the_flag_and_closes_the_stream(_no_overrides):
    db = FakeDb({"id": "job-1", "status": "running", "selector": {"tenant_id": "default"}})
    r = FakeRedis()
    r.keys["job:job-1:total"] = "10"
    r.keys["job:job-1:completed"] = "3"

    res = _client(db, r).post("/v1/analysis/job-1/cancel")

    assert res.status_code == 200, res.text
    assert res.json()["status"] == "cancelled"
    assert res.json()["previous_status"] == "running"
    assert res.json()["progress"] == {"total": 10, "completed": 3, "failed": 0}

    # The workers are told.
    assert asyncio.run(is_cancelled(r, "job-1")) is True
    # The row is written, and only while it is non-terminal.
    update = [s for s, _ in db.statements if s.startswith("UPDATE jobs")]
    assert len(update) == 1 and "status NOT IN ('cancelled', 'done', 'failed')" in update[0]
    assert db.commits == 1
    # A watching dashboard gets one terminating frame instead of a 5-min wait.
    assert len(r.published) == 1
    channel, body = r.published[0]
    assert channel == "analysis:progress:job-1"
    assert json.loads(body)["event"] == "cancelled"


def test_cancel_is_a_conflict_on_a_finished_job(_no_overrides):
    db = FakeDb({"id": "job-1", "status": "done", "selector": {"tenant_id": "default"}})
    r = FakeRedis()

    res = _client(db, r).post("/v1/analysis/job-1/cancel")

    assert res.status_code == 409
    assert asyncio.run(is_cancelled(r, "job-1")) is False
    assert not [s for s, _ in db.statements if s.startswith("UPDATE jobs")]


def test_cancel_reports_failure_rather_than_lying_about_the_stop(_no_overrides):
    """The flag IS the mechanism — no flag, no stop, so no 200."""
    db = FakeDb({"id": "job-1", "status": "running", "selector": {"tenant_id": "default"}})
    r = FakeRedis()
    r.fail = True

    res = _client(db, r).post("/v1/analysis/job-1/cancel")

    assert res.status_code == 503
    assert not [s for s, _ in db.statements if s.startswith("UPDATE jobs")]


def test_cancel_and_delete_are_tenant_scoped(_no_overrides):
    """A job belonging to nobody the caller can see is a 404, not a stop."""
    r = FakeRedis()
    client = _client(FakeDb(None), r)

    assert client.post("/v1/analysis/job-1/cancel").status_code == 404
    assert client.delete("/v1/analysis/job-1").status_code == 404
    assert asyncio.run(is_cancelled(r, "job-1")) is False


def test_delete_stops_a_running_job_and_purges_its_redis_state(_no_overrides):
    db = FakeDb({"id": "job-1", "status": "running", "selector": {"tenant_id": "default"}})
    r = FakeRedis()
    for tmpl in PURGEABLE_KEY_TEMPLATES:
        r.keys[tmpl.format(job_id="job-1")] = "x"

    res = _client(db, r).delete("/v1/analysis/job-1")

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["stopped"] is True
    assert body["deleted"] == {"jobs": 1, "redis_keys": len(PURGEABLE_KEY_TEMPLATES)}

    # Deleting the record must not leave the batch churning against a job id
    # that no longer exists — the flag stays behind on purpose.
    assert asyncio.run(is_cancelled(r, "job-1")) is True
    assert [s for s, _ in db.statements if s.startswith("DELETE FROM jobs")]

    # The posts' analyses are NOT job-scoped and must survive.
    assert not [s for s, _ in db.statements if "analysis_results" in s]
    assert not [s for s, _ in db.statements if "FROM posts" in s or "FROM comments" in s]


def test_delete_of_a_finished_job_does_not_raise_the_flag(_no_overrides):
    db = FakeDb({"id": "job-1", "status": "done", "selector": {"tenant_id": "default"}})
    r = FakeRedis()

    res = _client(db, r).delete("/v1/analysis/job-1")

    assert res.status_code == 200, res.text
    assert res.json()["stopped"] is False
    assert asyncio.run(is_cancelled(r, "job-1")) is False


# ---------------------------------------------------------------------------
# POST /v1/analysis/{id}/resume — the power-loss case
# ---------------------------------------------------------------------------
#
# The scenario these pin down: 30 of 300 posts analysed, the machine loses
# power, and the operator taps Resume. Nothing marks that job as dead — its row
# still reads "running" and its Redis counters are gone with the power — so what
# is left has to come out of Postgres, and only the remaining posts may be
# re-enqueued. Re-running all 300 is the failure this endpoint exists to avoid.


NOW = datetime.now(tz=timezone.utc)
LONG_AGO = NOW - timedelta(days=1)


class ResumeDb:
    """A job over `total` posts of which the first `done` have fresh results."""

    def __init__(self, *, total=300, done=30, status="running", updated_at=LONG_AGO,
                 created_at=LONG_AGO, options=None, selector=None, locked=False):
        self.post_ids = [f"p{i:04d}" for i in range(total)]
        self.done_ids = self.post_ids[:done]
        self.row = {
            "id": "job-1",
            "status": status,
            "selector": selector if selector is not None else {
                "tenant_id": "default", "post_ids": self.post_ids,
            },
            "options": options if options is not None else {"tasks": ["all"], "want_summary": True},
            "created_at": created_at,
            "updated_at": updated_at,
        }
        self.locked = locked
        self.statements: list[tuple[str, dict]] = []
        self.commits = 0

    async def execute(self, stmt, params=None):
        sql = " ".join(str(stmt).split())
        self.statements.append((sql, params or {}))
        if sql.startswith("SELECT id, status, selector, options, created_at, updated_at FROM jobs"):
            return _Result([self.row])
        if sql.startswith("SELECT id, raw_payload FROM posts"):
            return _Result([{"id": p, "raw_payload": {"id": p}} for p in self.post_ids])
        if sql.startswith("SELECT post_id FROM analysis_results"):
            return _Result([(p,) for p in self.done_ids])
        if sql.startswith("SELECT privacy_locked FROM tenant_policies"):
            return _Result([(True,)] if self.locked else [])
        return _Result([], rowcount=1)

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        pass


@pytest.fixture(autouse=True)
def _normalize_post_is_identity(monkeypatch):
    """The enqueue path re-normalizes each post; stub it, it is not under test."""
    import defense.services.ingestion.normalizer as normalizer

    monkeypatch.setattr(
        normalizer, "normalize_post",
        lambda raw: {"post_id": raw["id"], "engagement": {}, "comments": []},
    )


def _stage1_entries(redis):
    return [json.loads(f["data"]) for _id, f in redis.streams.get("nlp:stage1:queue", [])]


def test_resume_re_enqueues_only_the_unfinished_posts(_no_overrides):
    db = ResumeDb(total=300, done=30)
    r = FakeRedis()

    res = _client(db, r).post("/v1/analysis/job-1/resume")

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["resumed"] is True
    assert body["progress"] == {"total": 300, "completed": 30, "remaining": 270}

    enqueued = {e["post_id"] for e in _stage1_entries(r)}
    assert len(enqueued) == 270
    assert enqueued.isdisjoint(set(db.done_ids)), "a finished post was paid for twice"


def test_resume_rebuilds_the_counters_the_power_cut_took(_no_overrides):
    """`completed` must carry the work already done.

    With no `total` at all the assembler falls back to "first landing finishes
    the job"; with `completed` reset to 0 the job would need 300 more posts to
    reach its own total and never finish. Both make the resumed job's progress
    a lie.
    """
    db = ResumeDb(total=300, done=30)
    r = FakeRedis()
    assert "job:job-1:total" not in r.keys  # gone with the power

    _client(db, r).post("/v1/analysis/job-1/resume")

    assert r.keys["job:job-1:total"] == 300
    assert r.keys["job:job-1:completed"] == 30
    # Pre-crash failures are being retried in the remaining set, so keeping the
    # old count would push completed+failed past total and finish early.
    assert "job:job-1:failed" not in r.keys


def test_resume_clears_the_stop_flag_before_enqueueing(_no_overrides):
    """Resuming a job that was stopped must not feed it back into the drop check."""
    db = ResumeDb(total=10, done=4)
    r = FakeRedis()
    asyncio.run(mark_cancelled(r, "job-1"))

    res = _client(db, r).post("/v1/analysis/job-1/resume")

    assert res.status_code == 200, res.text
    assert asyncio.run(is_cancelled(r, "job-1")) is False
    assert len(_stage1_entries(r)) == 6


def test_resume_reuses_the_jobs_stored_options(_no_overrides):
    """A job run with summaries must not resume without them."""
    db = ResumeDb(total=10, done=4, options={"tasks": ["all"], "want_summary": True})
    r = FakeRedis()

    _client(db, r).post("/v1/analysis/job-1/resume")

    opts = _stage1_entries(r)[0]["options"]
    assert opts["want_summary"] is True
    assert opts["tasks"] == ["all"]
    assert opts["tenant_id"] == "default"


def test_resume_re_resolves_the_backend_against_the_current_policy(_no_overrides):
    """A stored 'groq' must not outlive the tenant being privacy-locked (§13.5)."""
    db = ResumeDb(total=10, done=4, options={"llm_backend": "groq"}, locked=True)
    r = FakeRedis()

    res = _client(db, r).post("/v1/analysis/job-1/resume")

    assert res.status_code == 403, res.text
    assert "privacy-locked" in res.json()["detail"]
    assert "nlp:stage1:queue" not in r.streams


def test_resume_refuses_a_job_that_is_still_moving(_no_overrides):
    """A live job is busy, not interrupted — re-enqueueing would duplicate work."""
    db = ResumeDb(total=300, done=30, updated_at=NOW)
    r = FakeRedis()

    res = _client(db, r).post("/v1/analysis/job-1/resume")

    assert res.status_code == 409
    assert "stop it first" in res.json()["detail"]
    assert "nlp:stage1:queue" not in r.streams


def test_resume_accepts_a_job_that_went_quiet(_no_overrides):
    from defense.services.api.routers import analysis

    db = ResumeDb(total=300, done=30,
                  updated_at=NOW - timedelta(seconds=analysis._STALE_JOB_SECONDS + 60))
    r = FakeRedis()

    res = _client(db, r).post("/v1/analysis/job-1/resume")

    assert res.status_code == 200, res.text
    assert res.json()["resumed"] is True


def test_resume_of_an_all_done_job_just_finishes_the_row(_no_overrides):
    """The assembler died before the last post flipped the status."""
    db = ResumeDb(total=30, done=30)
    r = FakeRedis()

    res = _client(db, r).post("/v1/analysis/job-1/resume")

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["resumed"] is False and body["status"] == "done"
    assert "nlp:stage1:queue" not in r.streams
    assert [s for s, _ in db.statements if "SET status = 'done'" in s]


def test_resume_is_a_conflict_on_a_completed_job(_no_overrides):
    db = ResumeDb(total=30, done=30, status="done")
    r = FakeRedis()

    res = _client(db, r).post("/v1/analysis/job-1/resume")

    assert res.status_code == 409
    assert "nothing to resume" in res.json()["detail"]


def test_resume_needs_a_selector_it_can_resolve(_no_overrides):
    """An ingest_sync job with only a date window names no post set."""
    db = ResumeDb(total=10, done=0, selector={"tenant_id": "default", "posted_from": "x"})
    r = FakeRedis()

    res = _client(db, r).post("/v1/analysis/job-1/resume")

    assert res.status_code == 422
    assert "no campaign_id or post_ids" in res.json()["detail"]


def test_resume_counts_only_results_written_after_the_job_started(_no_overrides):
    """Otherwise an older run's results would let a job skip posts it never did."""
    db = ResumeDb(total=10, done=4)
    r = FakeRedis()

    _client(db, r).post("/v1/analysis/job-1/resume")

    since = [p for s, p in db.statements if s.startswith("SELECT post_id FROM analysis_results")]
    assert since and since[0]["since"] == db.row["created_at"]
    assert "updated_at >= :since" in [
        s for s, _ in db.statements if s.startswith("SELECT post_id FROM analysis_results")
    ][0]

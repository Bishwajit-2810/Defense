Fix tenant isolation in this repo. It is currently designed and documented but **not
implemented at the data layer** — four related defects, all verified against the live dev
Postgres, listed below with the evidence.

## Before you start: what is ALREADY fixed — do not redo it

The working tree on `new_updates` contains an uncommitted Pass 8 remediation. Four of the
five Pass 8 findings are already fixed and regression-tested; see `AUDIT_PASS8.md`. Leave
them alone:

1. `_merge_ensemble` counting the hash stub's label as a measurement (now `uncertain`).
2. `tests/test_pipeline_e2e.py` running `run_all.py --reset` by default (now guarded by
   `RUN_DESTRUCTIVE_E2E`).
4. The watchlist rule existing as two copies (now `libs/stance_targets.watchlist_verdict`).
5. One voter counted as unanimity (now requires two, with `single_voter` / `unread`).

Baseline before you change anything: `.venv/bin/python -m pytest -q` → **830 passed, 1
skipped, ~40 s**. Do not regress it.

## Hard constraints

- **A live dev stack is running** (`deploy-postgres-1`, `deploy-redis-1`,
  `deploy-clickhouse-1`, `deploy-minio-1`) and it holds real data. Any probe you run against
  it must be **read-only**. Never `FLUSHALL`, never `TRUNCATE`, never `DROP`.
- **Do not run `tests/test_pipeline_e2e.py` and do not set `RUN_DESTRUCTIVE_E2E`.** It wipes
  Redis, Postgres and ClickHouse. That guard exists because the stack above is live.
- **Do not run `run_all.py --reset`** for the same reason.
- Schema changes go in `deploy/init-db.sql` (Postgres) and
  `src/defense/services/workers/assembler/clickhouse_init.sql` (ClickHouse). Both are applied
  idempotently via `CREATE TABLE IF NOT EXISTS` / `ALTER TABLE … ADD COLUMN IF NOT EXISTS`;
  match that style so existing deployments migrate without manual steps.

## FIRST: a scope question you must resolve with the user before writing code

`OPEN_ISSUES.md` notes the pipeline "is documented as 'single-tenant' in several places, but
the auth system issues per-tenant tokens". Both readings are defensible, and they lead to
very different work:

- **(a) It is genuinely multi-tenant.** Then build the data layer out: the work below.
- **(b) It is single-tenant and the tenant plumbing is aspirational.** Then the honest fix is
  much smaller and partly *subtractive*: make the boundary fail closed where it exists,
  delete or clearly mark the checks that only look like they enforce something, and state the
  single-tenant assumption where the docs currently imply otherwise. A half-built boundary
  that reads as enforced is worse than a documented absence — that is this project's own
  standard.

Ask the user which it is, with a recommendation, before implementing. Everything below
assumes (a); under (b), report which parts still apply.

## The four defects

### 1. `analysis_results` is not tenant-scoped anywhere — AUDIT_PASS8 §3

`analysis_results` has **no `tenant_id` column** and no `job_id` to join back to `jobs`, so
every read of it crosses tenants. Readers to fix (all of them):

- `services/api/routers/analysis.py` — `export_analysis`, `get_post_comments`, and the
  overview/aggregate queries
- `services/api/routers/reports.py`
- `services/api/routers/search.py`
- `services/api/routers/usage.py`
- `services/api/routers/pipeline.py`
- `mcp_servers/retrieval_mcp/server.py`

Pass 7 listed this as issue 9 sub-item 3, fixed the other four sub-items, and marked the
issue ✅. The `campaign_id` query parameter that was added is an optional caller-supplied
filter, not an isolation boundary.

The ClickHouse tables (`analysis_events`, `comment_sentiments`) have the same gap and cannot
join to Postgres, so they need a real tenant column of their own.

### 2. The `jobs` tenant filter is vacuous

`analysis.py` scopes job reads with:

```sql
WHERE id = :id AND (selector->>'tenant_id' = :tid OR selector->>'tenant_id' IS NULL)
```

But **nothing ever writes `tenant_id` into a selector.** `_create_analysis_job` builds it
from `campaign_id` / `post_ids` / `filter` only. So the `IS NULL` branch always wins and
every job matches every tenant. Verified read-only against the live DB:

```
$ docker exec deploy-postgres-1 psql -U defense -d defense \
    -c "SELECT count(*) AS jobs, count(selector->>'tenant_id') AS with_tenant FROM jobs;"
 jobs | with_tenant
------+-------------
    1 |           0
```

Note the shape: this is verbatim the failure `router/rules.py`'s docstring warns about — a
check written against a key nothing populates, which reads as enforcement in code review and
enforces nothing. Whatever you do here, the fix must not be another filter on a field no
writer sets.

### 3. `DELETE /v1/posts/{post_id}` is dead — it joins a table that does not exist

`services/api/routers/ingest.py:239` (`delete_post`) verifies ownership with:

```sql
SELECT 1 FROM posts p JOIN campaigns c ON p.campaign_id = c.id
WHERE p.id = :pid AND c.tenant_id = :tid
```

There is **no `campaigns` table** — not in `deploy/init-db.sql`, not in any migration, not
anywhere in the repo. So the query raises `UndefinedTable` and the endpoint 500s on every
call. Verified read-only against the live DB:

```
$ docker exec deploy-postgres-1 psql -U defense -d defense -c "\dt"
 analysis_results | api_keys | comments | jobs | posts | tenant_policies | users
   (7 rows — no campaigns)

$ docker exec deploy-postgres-1 psql -U defense -d defense \
    -c "SELECT 1 FROM posts p JOIN campaigns c ON p.campaign_id = c.id WHERE p.id='x' AND c.tenant_id='default'"
ERROR:  relation "campaigns" does not exist
```

The tenant check added to fix Pass 6 issue 8 is what broke the endpoint. Pass 6's own
recommended mitigation — *"a `test_tenant_isolation.py` that, for every entity-by-ID
endpoint, calls it as a different tenant and asserts 404"* — was never written, which is why
nothing caught it.

### 4. The `campaigns` table is assumed by code and docs but never created

Beyond defect 3, `OPEN_ISSUES.md` states "the database stores per-tenant campaigns", and
`campaign_id` is threaded through posts, analysis results, both ClickHouse tables and the
reports API. The table that would give `campaign_id` a tenant never existed. Decide
deliberately whether it should, because it is the natural home for the boundary.

## Design decision, and the trap in it

Two ways to give a result row a tenant. Weigh them and say why you chose:

- **Create `campaigns` (id, tenant_id, …) and derive tenancy by join.** Matches what the code
  and docs already assume, fixes defects 3 and 4 outright, and keeps one source of truth.
- **Denormalise `tenant_id` onto `analysis_results` directly.** No join on read, but needs a
  writer that knows the tenant.

**The trap:** `tenant_id` appears **nowhere** in `services/workers/` or
`services/ingestion/` — the pipeline envelope does not carry it, so the assembler cannot
stamp it today. Either thread it end-to-end (API → ingestion → stage1 → router → stage2 →
assembler) or have the assembler resolve `campaign_id → tenant_id` against Postgres, which it
already connects to. ClickHouse needs a literal column either way, because it cannot join to
Postgres. Whichever you pick, say what happens to rows whose `campaign_id` is NULL — and
prefer **fail closed** (invisible to every tenant, loudly logged) over defaulting them into
`'default'`, matching `tenant_is_privacy_locked`'s `tenant_policy_lookup_failed_denying`.

Backfill matters: existing `analysis_results` rows have no tenant. Say in the migration what
they become and why, rather than letting them silently land in one tenant.

## House conventions to follow

- **Fail closed.** A missing or unresolvable tenant must never match everything. The
  precedent is `deps.tenant_is_privacy_locked`, which refuses rather than guesses.
- **Docstrings and comments explain WHY, naming the failure mode being prevented.** Read
  `libs/ensemble.py` or `router/rules.py` for the register. Do not narrate what the code does.
- **Tests are named as sentences that state the property** —
  `test_opposing_an_undeclared_target_does_not_alert`. Each new test should fail on the old
  code; check that it does.
- **A scan that finds nothing must never read as a finding.** If you write a grep-the-source
  contract test, it must assert its own file list is non-empty — this exact bug made four
  contract tests silently vacuous before Pass 7 (`AUDIT_PASS7.md` §0).

## Deliverables

1. The scope decision from the user, recorded in the audit doc.
2. Migrations in `deploy/init-db.sql` + `clickhouse_init.sql`, idempotent, with the backfill
   policy stated in a comment.
3. Every read path listed in defect 1 tenant-scoped; defects 2, 3 and 4 fixed or explicitly
   retired with reasoning.
4. **`tests/test_tenant_isolation.py`** — the file Pass 6 asked for. For every entity-by-ID
   and list endpoint over `posts` / `analysis_results` / `jobs`, call it as tenant B against
   tenant A's data and assert it is not returned (404 or absent, per endpoint). Include a
   regression test that `delete_post`'s ownership query executes against the real schema, so
   defect 3's class cannot return.
5. A contract test asserting no `FROM analysis_results` read path lacks a tenant predicate
   (with the non-empty-file-list assertion above).
6. `AUDIT_PASS9.md` in the house style of `AUDIT_PASS8.md`: where, what's wrong, reproduce,
   fix, test left behind. Update `AUDIT_PASS8.md` §3's status and the ⚠️ note in
   `AUDIT_PASS7.md` issue 9 sub-item 3.

## Acceptance criteria

- `.venv/bin/python -m pytest -q` → at least 830 passed + your new tests, 1 skipped, no new
  failures.
- `cd dashboard && npx vitest run` → 11 passed (or more), and `npx vite build` succeeds if you
  touch the dashboard.
- Each new test demonstrably fails on the pre-fix code — state how you checked.
- The live dev stack is intact: `docker exec deploy-postgres-1 psql -U defense -d defense -c
  "SELECT count(*) FROM analysis_results;"` returns what it did before you started. Record
  that count first.
- Anything you could not verify is listed in a "What was NOT verified" section, with the
  reason — do not report unverified work as done.

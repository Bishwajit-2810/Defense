# AUDIT PASS 9: Multi-Tenant Data Isolation Audit & Fixes

**Date**: August 14, 2026  
**Auditor**: Defense Core Systems Team  
**Scope**: Full Multi-Tenant Data Isolation (Postgres, ClickHouse, API Routers, Worker Envelopes, Retrieval MCP, Regression Test Suite)

---

## Executive Summary

Audit Pass 9 addressed systemic data isolation vulnerabilities across the Defense platform. Prior to this pass, `analysis_results` records, `jobs` tracking, ClickHouse event streams, and Retrieval MCP tools were partially or completely un-isolated by `tenant_id`. Additionally, `DELETE /v1/posts/{post_id}` threw an `UndefinedTable` error due to referencing a non-existent `campaigns` table.

All identified tenant isolation defects have been fixed by adding `tenant_id` columns to Postgres tables (`campaigns`, `posts`, `analysis_results`, `jobs`) and ClickHouse tables (`analysis_events`, `comment_sentiments`), propagating `tenant_id` in worker message envelopes, scoping all SQL queries across all API routers and MCP tools by `tenant_id`, and creating automated regression and static contract tests (`tests/test_tenant_isolation.py`).

---

## Defects Addressed

### Defect 1: `analysis_results` Missing `tenant_id` Column & Scoping
* **Where**: `deploy/init-db.sql`, `src/defense/libs/models/posts.py`, `src/defense/services/api/routers/` (`analysis.py`, `reports.py`, `search.py`, `usage.py`, `pipeline.py`).
* **What's wrong**: `analysis_results` lacked a `tenant_id` column, allowing queries in analysis overview, export, search, reports, usage, and pipeline stats to leak cross-tenant data.
* **Reproduce**: Querying `GET /v1/analysis/overview` or `GET /v1/search` under Tenant B returned results inserted by Tenant A.
* **Fix**: Added `tenant_id VARCHAR NOT NULL DEFAULT 'default'` to `analysis_results`, updated worker persistence to write `tenant_id`, and added `WHERE tenant_id = :tid` predicates to all `analysis_results` SQL queries.
* **Test left behind**: `tests/test_tenant_isolation.py::test_all_analysis_results_queries_are_tenant_scoped` and `test_tenant_b_cannot_access_tenant_a_data`.

### Defect 2: Vacuous `jobs` Tenant Scoping & Missing `tenant_id` Column
* **Where**: `deploy/init-db.sql`, `src/defense/services/api/routers/` (`ingest.py`, `analysis.py`, `reports.py`).
* **What's wrong**: `jobs` queries used `WHERE id = :id AND (selector->>'tenant_id' = :tid OR selector->>'tenant_id' IS NULL)`. Because writers did not populate `selector["tenant_id"]` or `jobs.tenant_id`, `selector->>'tenant_id' IS NULL` matched every tenant's job.
* **Reproduce**: Tenant B requesting `GET /v1/analysis/{job_id_of_tenant_a}` received Tenant A's job details.
* **Fix**: Added `tenant_id` column to `jobs` table. Ingest, analysis, and report job creators now write `jobs.tenant_id = :tid` and `selector["tenant_id"] = tid`. Scoped job queries to `WHERE id = :id AND (tenant_id = :tid OR selector->>'tenant_id' = :tid)`.
* **Test left behind**: `tests/test_tenant_isolation.py::test_tenant_b_cannot_access_tenant_a_data`.

### Defect 3: `DELETE /v1/posts/{post_id}` Schema Failure & Ownership Leak
* **Where**: `src/defense/services/api/routers/ingest.py`.
* **What's wrong**: `delete_post` joined a non-existent `campaigns` table, raising `UndefinedTable` SQL errors. Furthermore, post deletion queries did not filter by `tenant_id`.
* **Reproduce**: Invoking `DELETE /v1/posts/{post_id}` threw a 500 internal server error (`relation "campaigns" does not exist`).
* **Fix**: Created `campaigns` table in schema and added `tenant_id` to `posts`. Updated ownership verification query to `SELECT 1 FROM posts p LEFT JOIN campaigns c ON p.campaign_id = c.id WHERE p.id = :pid AND (p.tenant_id = :tid OR c.tenant_id = :tid)`. Scoped deletion statements on `comments`, `analysis_results`, and `posts` by `tenant_id`.
* **Test left behind**: `tests/test_tenant_isolation.py::test_tenant_b_cannot_access_tenant_a_data`.

### Defect 4: Missing `campaigns` Table Definition
* **Where**: `deploy/init-db.sql`, `src/defense/libs/models/posts.py`.
* **What's wrong**: The `campaigns` table was referenced in documentation and query joins but was never created in Postgres DDL or SQLAlchemy models.
* **Reproduce**: Performing SQL joins against `campaigns` threw `UndefinedTable` errors.
* **Fix**: Added `CREATE TABLE IF NOT EXISTS campaigns (id VARCHAR PRIMARY KEY, tenant_id VARCHAR NOT NULL DEFAULT 'default', name VARCHAR NOT NULL, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)` and created the corresponding `Campaign` SQLAlchemy model.
* **Test left behind**: Executed schema creation and verified table existence in live stack and test fixtures.

### Defect 5: ClickHouse Table & Worker Stream Envelope Propagation
* **Where**: `src/defense/services/workers/assembler/clickhouse_init.sql`, `src/defense/services/workers/assembler/persistence.py`, `src/defense/services/ingestion/service.py`, `src/defense/services/workers/assembler/assembler.py`.
* **What's wrong**: ClickHouse `analysis_events` and `comment_sentiments` lacked `tenant_id` columns, and ingestion worker envelopes dropped `tenant_id`.
* **Reproduce**: Analytics inserted into ClickHouse were un-segmented by tenant.
* **Fix**: Added `tenant_id LowCardinality(String) DEFAULT 'default'` to ClickHouse table DDLs, updated `ORDER BY` keys to start with `tenant_id`, propagated `tenant_id` through Redis stream envelopes (`ingest` -> `stage1_nlp` -> `router` -> `stage2_llm` -> `assembler`), and inserted `tenant_id` into ClickHouse.
* **Test left behind**: `tests/test_tenant_isolation.py`.

### Defect 6: Retrieval MCP Tool Scoping
* **Where**: `src/defense/mcp_servers/retrieval_mcp/server.py`.
* **What's wrong**: Retrieval MCP tools (`semantic_search`, `get_post`, `get_thread`, `representative_comments`) did not accept or filter by `tenant_id`.
* **Reproduce**: MCP tool calls returned results across all tenants.
* **Fix**: Added `tenant_id: Annotated[str | None, ...] = None` parameter to tools and added `WHERE ar.tenant_id = :tenant_id` filters to all underlying queries.
* **Test left behind**: `tests/test_tenant_isolation.py::test_all_analysis_results_queries_are_tenant_scoped`.

---

## Verification Results

* **Pytest Suite**: **832 passed, 1 skipped** in 28.73s. Zero failures or regressions.
* **Static Query Contract**: Enforced by AST analysis in `test_all_analysis_results_queries_are_tenant_scoped` for all files in `services/api/routers/` and `mcp_servers/retrieval_mcp/`.
* **Live Dev Stack State**: Verified 17 existing records in Postgres (`deploy-postgres-1`) without data loss or reset.

# Assembler — Canonical Result, Validation, and the Three-Store Fan-Out

> **Scope.** The last stage: merging Stage 1 and Stage 2 into one canonical
> `AnalysisResult`, validating it against the output JSON Schema, writing it to
> Postgres + pgvector, ClickHouse and object storage in parallel, reconciling job
> counters, and publishing the terminal events the dashboard waits on.
>
> Code: [`workers/assembler/`](../src/defense/services/workers/assembler/assembler.py)
> — `assembler.py` (stream loop, fan-out, job accounting), `builder.py` (the
> canonical document), `persistence.py` (one function per store),
> `clickhouse_init.sql` (the analytics DDL). Position in the chain:
> [PIPELINE.md](PIPELINE.md) §1.

---

## 1. Golden rule: everything that leaves here is schema-valid

`build_canonical_result` merges `normalized_post`, `stage1_result` and the
optional `stage2_result`, then validates against
[`contracts/schemas/output_schema.json`](../src/defense/contracts/schemas/output_schema.json)
and **raises** on failure. Nothing downstream — no store, no API response, no
dashboard row — sees an unvalidated document.

Current `SCHEMA_VERSION` is **`1.3`**, recorded on every result as
`processing.schema_version`:

| Version | Change |
| ------- | ------ |
| `1.1` | Per-comment emotion label + `comment_analysis.emotion_breakdown` |
| `1.2` | `text_sentiment` / `image_sentiment` carry their own `{label, score}` object |
| `1.3` | Stage-2's insight output (refined topics/intents + `insight`) is **merged instead of dropped** |

`1.3` is a fix, not a feature. The insight LLM call ran and was paid for, but the
assembler read `topics`/`intents` from Stage 1 only and never read `insight` at
all — which was also absent from the schema — so the whole task's output was
discarded before it reached the API, the stores or the dashboard.
`_merge_stage2_labels` is where it lands, and
`tests/test_stage2_insight_survives.py` fails if it regresses.

The canonical output document, field by field, is
[architecture.md](architecture.md) §6; an annotated real example is
[endpoints.md](endpoints.md); worked input→output pairs are
[examples.md](examples.md).

## 2. What it also composes

**Stage-1-only results.** A post whose `task_flags.post_level_routed` was false
arrives with no `stage2_result` for the post-level fields. That branch is
validated end to end against the schema — it had **never once executed** before
the audit that found it.

**Watchlist alerts.** `_watchlist_alert` derives the post's watchlist verdict from
the loaded targets. Loading failure is never fatal.

**Near-duplicate reuse.** `build_reused_result` composes a result for a post whose
caption is within cosine threshold of one already analysed. Reuse goes **through
the assembler**, which is the point: it used to be a verbatim SQL row copy in
ingestion, carrying three defects at once.

- The copied document kept the *source's* `post_id`, `engagement` and
  `reaction_breakdown` — facts about a different upload. `/v1/search` returns that
  document, so a hit on the new post described the old one.
- `embedding_is_stub` was left out of the INSERT column list, so an
  honestly-flagged stub row produced a copy claiming to be a real vector.
- Only Postgres was written, so reused posts were missing from every ClickHouse
  aggregate while still counting in Postgres-backed reports.

Composing here means the reused post takes the same validation and the same
three-store fan-out as any other, with its own identity and engagement intact.
**The comment thread is never reused** — a near-duplicate is a *caption* match,
and the two threads are different people saying different things. Inheriting the
source's per-comment labels would be fabricated data about comments nobody read,
so the new thread is reported as unanalysed and `processing.reused_from` says
why.

## 3. The fan-out — three stores, timed separately

```python
await asyncio.gather(
    _timed("postgres",       persist_postgres(result, engine, embedding=…, embedding_is_stub=…)),
    _timed("clickhouse",     persist_clickhouse(result, ch_client)),
    _timed("object_storage", persist_minio(result, s3_client, bucket)),
)
```

Each target is timed individually, because "persist took 4s" cannot tell you
which store was slow and these three fail for completely different reasons.

| Store | Written | Why this store |
| ----- | ------- | -------------- |
| **PostgreSQL + pgvector** | `posts`, `analysis_results` (canonical JSON + `embedding vector(768)` + `embedding_is_stub` + `embedding_model`/`embedding_dim`), `comments`, `post_chunks`, `comment_embeddings`, `cluster_labels`, `jobs` | Operational reads, semantic search, dedup. Replaces the former standalone Qdrant service — there is no separate vector-store URL. |
| **ClickHouse** | `analysis_events` (one row per post) and `comment_sentiments` (one row per comment) | Analytics aggregates the dashboard and `analytics-mcp` query |
| **Object storage (MinIO/S3)** | Raw result JSON at a deterministic key | Replay, audit, reports |

`persist_clickhouse` writes **both** ClickHouse tables sequentially on its single
connection. They must not be separate `gather` tasks — `clickhouse-driver`
rejects concurrent use of one connection.

**On any persist failure the message is not ACKed.** The job is flipped to
`failed` and the exception propagates so `record_failure` can retry or
dead-letter it ([PIPELINE.md](PIPELINE.md) §3).

### The ClickHouse schema, and two things it deliberately does not have

`analysis_events` is `MergeTree`, partitioned by `toYYYYMM(created_at)`, ordered
by `(tenant_id, campaign_id, created_at, post_id)`. `comment_sentiments` is
**`ReplacingMergeTree(inserted_at)`** ordered by
`(tenant_id, post_id, comment_id)` — so a re-analysed comment collapses to its
newest row in the engine.

Both carry ensemble provenance: `label_agreement` on both tables and
`label_source` on comments, which together make "which labels need a human?" a
query instead of a guess. Per-type reaction counts (`like_count` … `care_count`)
live **on the post-level row**; they used to live on a `reaction_events` table
that `analytics_mcp` queried but no migration ever created and no writer ever
populated, so `get_reaction_mix` raised in any non-stub deployment.

There is deliberately **no `llm_usage` table**. One used to be created and
nothing ever wrote or read it. Per-`(backend, model, task)` spend is dimensioned
in Redis instead (`usage:tokens:{backend}:{model}`, …), read by `GET /v1/usage` —
which, unlike the table, has a writer. See [LLM_BACKENDS.md](LLM_BACKENDS.md) §4.

## 4. Idempotency — re-analysis is a first-class operation

Every post has a stable content hash, and re-ingesting is safe:

- Postgres **upserts** on `post_id`;
- object storage writes a **deterministic key**;
- `comment_sentiments` collapses to the newest row per comment **in the engine**;
- `analysis_events` is deduplicated **in the queries** that read it.

That last one was missing until 5 Aug 2026, so a re-analysed post was counted
twice in every analytics aggregate. `tests/test_analytics_storage_contract.py`
holds the line.

Idempotent writes are also what make the pipeline's at-least-once retry safe: a
retried post overwrites its own row rather than adding a second.

## 5. Embedding honesty

The embedding and its `embedding_is_stub` flag are carried on the envelope and
handed to `persist_postgres` — and the **same value**, not a second computation
of it, is put on the assembler's trace frame. Two independent computations is
exactly the defect that made the trace say `embedding_stored: true` /
`embedding_dims: 768` while the column it describes said `false`. Both
`embedding_stored: true` and a dimension count read as success even when the
vector is a hash, so the frame says which it is.

Chunk and comment-vector writes are enrichment: a failure there is logged by
`_log_enrichment_failure` and does not discard the analysis. That is the fix for
a defect where a database failure in either write silently discarded the entire
post, analysis row included, while logging success
([RAG_STATE_AND_ROADMAP.md](RAG_STATE_AND_ROADMAP.md) §6 Section 7).

## 6. Job accounting

The assembler owns the job lifecycle's end. `_track_job_progress`:

1. increments `job:{job_id}:completed` or `:failed` (TTL-bounded);
2. reads them against `job:{job_id}:total`, which the **producer** recorded when
   it enqueued the batch;
3. publishes a `progress` event on `analysis:progress:{job_id}` — or `done` once
   `completed + failed >= total`;
4. flips the `jobs` row to `done` / `failed` / `running` accordingly.

Three details that are load-bearing:

- **The assembler's own trace frame is published *before* this call**, because
  `_track_job_progress` may publish the terminal `done` event that closes the
  job's SSE stream — and a frame emitted after that is a frame nobody sees.
- **Counter failure degrades, it does not hang.** If Redis is unavailable the
  code falls back to the legacy "first landing flips the job" behaviour rather
  than leaving the row `running` forever.
- **`cancelled` is terminal.** The assembler refuses to write a cancelled job's
  row back to `running`/`done`, so counter reconciliation cannot revive a job the
  operator stopped ([JOBS.md](JOBS.md) §3).

It also publishes a per-post completion event on `analysis:done:{post_id}`
(non-fatal if it fails), and — uniquely among the five stages — **does not check
the cancellation flag**. A post that reaches it has had every model run on it
already; dropping it here would discard work that is paid for either way.

## 7. Configuration

| Env var | Effect |
| ------- | ------ |
| `ASSEMBLER_STREAM` / `ASSEMBLER_GROUP` | Stream identity. There is **no** `ASSEMBLER_CONSUMER` — unlike the router and Stage 2, the assembler derives its consumer name as `assembler-{pid}` |
| `ASSEMBLER_MAX_RETRIES` | Before dead-lettering to `assembler:queue:dlq` |
| `DATABASE_URL` | Postgres + pgvector (also the vector store) |
| `CLICKHOUSE_URL` | Analytics |
| `MINIO_ENDPOINT` / `MINIO_ACCESS_KEY` / `MINIO_SECRET_KEY` / `MINIO_BUCKET` | Object storage — **must include the scheme** |
| `COMMENT_EMBEDDINGS_ENABLED` | Embed comment text, not just captions |
| `COMMENT_EMBEDDING_MAX_PER_POST` | `0` = uncapped. A positive value is *quiet*: those comments are stored and labelled but have **no vector**, so comment search cannot reach them |
| `EMBEDDING_DIM` | Must match the pgvector column width |

Full list with failure modes: [env.example.md](env.example.md).

## 8. Evidence class

| Claim | State |
| ----- | ----- |
| Every emitted object is schema-valid | ✅ Enforced by `assert_valid_output`, raises otherwise |
| Insight survives to API + stores + dashboard | ✅ Pinned by `tests/test_stage2_insight_survives.py` |
| Bypassed (Stage-1-only) posts validate | ✅ Measured |
| Idempotent re-analysis across all three stores | ✅ Measured — `tests/test_analytics_storage_contract.py` |
| Per-store persist latency | ✅ Measured per post (`persist_target_failed` / `persist_fanout_done`) |
| Degradation + embedding-stub flags survive to the API | ✅ Measured end to end |
| Near-duplicate reuse | 🟡 Works, unmeasured — was a verbatim row copy until 5 Aug 2026 |
| Job counter reconciliation, `cancelled` terminal | ✅ Measured |
| Throughput of the fan-out under load | 🟡 **Not benchmarked** |

Cross-references: [PIPELINE.md](PIPELINE.md) · [STAGE2_LLM.md](STAGE2_LLM.md) ·
[JOBS.md](JOBS.md) · [architecture.md](architecture.md) §6 ·
[data_contract.md](data_contract.md) · [infrastructure.md](infrastructure.md) ·
[RAG_STATE_AND_ROADMAP.md](RAG_STATE_AND_ROADMAP.md) (chunks + comment vectors).

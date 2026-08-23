# Ingestion — Pull, Normalize, Dedup, Enqueue

> **Scope.** How a post enters the system: the two entry points (upload and
> upstream pull), normalization and the golden rules it enforces, the two kinds
> of deduplication, and where a job's expected total is recorded.
>
> Code: [`services/ingestion/service.py`](../src/defense/services/ingestion/service.py)
> (the stream consumer), [`normalizer.py`](../src/defense/services/ingestion/normalizer.py)
> (the transform), [`api/routers/ingest.py`](../src/defense/services/api/routers/ingest.py)
> (the entry points). The upstream payload contract is
> [data_contract.md](data_contract.md) — the source of truth for field
> semantics; this document is the mechanism.

---

## 1. Two ways in

| Entry point | What it does |
| ----------- | ------------ |
| `POST /v1/posts/upload` | Push a batch of post-with-details payloads directly. Creates a job, `XADD`s one entry per post to `ingestion:queue`, records `job:{id}:total`. This is the path `run_all.py` uses and the one the corpus is loaded through. |
| `POST /v1/ingest/sync` | Ask the service to **pull** from the upstream platform API for a campaign / date range. Enqueues one *control* message; the ingestion worker does the HTTP work. |
| `DELETE /v1/posts/{post_id}` | Remove a post and its analysis. |

**`ingest_sync` is a control message, not a post.** The worker recognises
`job_type == "ingest_sync"`, `GET`s `<UPSTREAM_API_URL>/posts-with-details` with
`campaignId` / `postedFrom` / `postedTo` and an
`Authorization: Bearer <UPSTREAM_API_KEY>` header when set, then feeds each
returned post **back through the same queue** wrapped with the `job_id` — so the
normal dedup → normalize → enqueue path applies to pulled posts exactly as it
does to uploaded ones. No second code path, no second set of bugs.

It fails **fast and actionably** rather than silently: with no
`UPSTREAM_API_URL` the job is set `failed` with the message *"UPSTREAM_API_URL is
not configured — set it (and UPSTREAM_API_KEY) to enable upstream pulls, or use
POST /v1/posts/upload instead."* Pull failure and a response that is not a post
list each fail the job with their own reason.

The corpus in this repo has no live upstream, so **`/v1/posts/upload` is the
exercised path** and `/v1/ingest/sync` is implemented but unexercised against a
real platform.

## 2. The golden rules, enforced in the normalizer

`normalize_post` is where the integration contract becomes code. Five rules,
each of which exists because getting it wrong is invisible downstream:

1. **Platform is derived from the URL host, never hardcoded.** Facebook in the
   current sample; others supported.
2. **`baseline_sentiment` / `baseline_viral_potential` are preserved exactly as
   received** and never overwritten, so our recomputation can be compared against
   the upstream's.
3. **`comment_sentiment` is intentionally left null** — computing it is our job,
   and seeding it would make the comparison circular.
4. **No write-back to the upstream system.** Read-only consumer, own database.
5. **The content hash is computed here** and used by the service layer for dedup.

It also translates the upstream's camelCase engagement keys to the output
schema's snake_case (`commentCount → comment_count`,
`storedCommentRows → stored_comments`, `totalReactions → total_reactions`, …),
folds `reactionBreakdown` into a dict, and computes `coverage` from
`stored_comments / comment_count`.

Every payload is validated against
[`input_schema.json`](../src/defense/contracts/schemas/input_schema.json) by
`assert_valid_input` before any of this runs.

## 3. Two kinds of deduplication

### Exact — content hash, Redis `SET NX`

```
SET dedup:{content_hash} 1 NX EX 604800
```

Returns the key already existed ⇒ duplicate ⇒ skip. The TTL bounds the dedup
window at 7 days, so a legitimate re-ingest later is not blocked forever.
Re-analysis of a *known* post is a first-class operation and goes through the
upsert path instead ([ASSEMBLER.md](ASSEMBLER.md) §4).

### Near-duplicate — cosine over caption embeddings

`_find_near_duplicate` embeds the caption and runs a pgvector nearest-neighbour
query over `analysis_results.embedding`, **scoped to the tenant and (when given)
the campaign**. Above `NEAR_DUP_THRESHOLD` the prior analysis is reused: Stage 1
and Stage 2 are skipped entirely and the result is composed by the assembler
([ASSEMBLER.md](ASSEMBLER.md) §2).

> **Both defaults are worth reading twice.** `NEAR_DUP_DEDUP` ships **`false`**
> (Settings default) — near-duplicate reuse is **off** unless you turn it on —
> and `NEAR_DUP_THRESHOLD` is **`0.95`**, not the `0.97` older docs quote.
> Nothing in `.env` or `.env.example` sets either, so the Settings defaults are
> what runs.

Two consequences to keep in mind if you do enable it:

- **In stub mode only *identical* captions match**, because the stub embedding is
  a hash of the text: identical text yields an identical vector and cosine is
  exactly 1.0. That makes it a repost detector, not a similarity detector.
- **Turn it off for any run whose per-post latency you intend to quote**
  (`NEAR_DUP_DEDUP=false`), because a reused post skips both stages and its
  timing is not comparable to an analysed one.

## 4. Job bookkeeping starts here

The producer — whichever entry point created the batch — records
`job:{job_id}:total` with a 24-hour TTL, and that number is what the assembler
reconciles its counters against to decide a job is finished
([ASSEMBLER.md](ASSEMBLER.md) §6). Posts skipped as duplicates are counted so a
job whose every post was a duplicate still terminates rather than hanging at
0/N.

Ingestion also publishes the **first** stage event (`ingest`), during the POST
that creates the job — which is why `libs/progress.py` replays buffered events on
SSE connect: no browser can be subscribed yet ([PIPELINE.md](PIPELINE.md) §3).

Cancellation is honoured here as at every other pre-assembler stage: a
cancelled job's messages are dropped as they are picked up
([JOBS.md](JOBS.md) §2).

## 5. Failure policy

**A single bad message never crashes the consumer.** Validation errors are logged
and the original message is **ACKed** — a permanently-bad payload must not block
the stream behind it. Operational failures (a database that is down, not a
payload that is wrong) go through `record_failure`: bounded retry, then
`ingestion:queue:dlq` ([PIPELINE.md](PIPELINE.md) §3).

## 6. What lands in Postgres

`_upsert_post` writes the canonical normalized record via
[`libs/repos/posts.py`](../src/defense/libs/repos/posts.py), upserting on
`post_id`. The caption embedding is computed here for the near-duplicate lookup;
the *authoritative* result-row embedding is written later by the assembler with
its own `embedding_is_stub` provenance.

## 7. Configuration

| Env var | Default | Effect |
| ------- | ------- | ------ |
| `UPSTREAM_API_URL` | `http://upstream-api/v1` | Source for `/v1/ingest/sync`. Empty disables upstream pulls entirely |
| `UPSTREAM_API_KEY` | — | Sent as `Authorization: Bearer <key>` when set |
| `NEAR_DUP_DEDUP` | **`false`** | Enable near-duplicate reuse |
| `NEAR_DUP_THRESHOLD` | **`0.95`** | Cosine similarity required to count as a near-duplicate |
| `INGESTION_STREAM` / `INGESTION_GROUP` | `ingestion:queue` / `ingestion-workers` | Stream identity |
| `INGESTION_MAX_RETRIES` | `3` | Before dead-lettering |
| `DATABASE_URL` · `REDIS_URL` | see [env.example.md](env.example.md) | Stores |

The dedup TTL (7 days) is a module constant, not an env var.

## 8. Evidence class

| Claim | State |
| ----- | ----- |
| Post-with-details pull → own database, no write-back | ✅ Measured (upload path) |
| Platform detection from the URL host | ✅ Measured |
| Baseline preservation | ✅ Measured |
| Idempotent upsert + deterministic object key | ✅ Measured |
| Exact content-hash dedup | ✅ Measured |
| Input-schema validation on every payload | ✅ Measured |
| A bad payload does not block the stream | ✅ Measured |
| `/v1/ingest/sync` against a live upstream platform | ⚠️ **Unexercised** — no upstream is reachable from this repo |
| Near-duplicate reuse | 🟡 Works, unmeasured — and **ships disabled** (§3) |

Cross-references: [data_contract.md](data_contract.md) (the payload contract) ·
[PIPELINE.md](PIPELINE.md) · [ASSEMBLER.md](ASSEMBLER.md) · [JOBS.md](JOBS.md) ·
[api_design.md](api_design.md) · [endpoints.md](endpoints.md) ·
[examples.md](examples.md).

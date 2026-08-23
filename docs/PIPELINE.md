# Pipeline — The Five Stages, End to End

> **Scope.** The per-post processing pipeline: the five stages, the Redis
> streams between them, the one envelope that threads through all of them, and
> the properties (cancellation, retry, dead-lettering, progress) that hold at
> every hop.
>
> This is the **hub document** for the stage docs. Each stage has its own:
> [INGESTION.md](INGESTION.md) · [STAGE1_NLP.md](STAGE1_NLP.md) ·
> [ROUTER.md](ROUTER.md) · [STAGE2_LLM.md](STAGE2_LLM.md) ·
> [ASSEMBLER.md](ASSEMBLER.md). Job control across all five is
> [JOBS.md](JOBS.md). The architectural rationale is
> [architecture.md](architecture.md) §3 and §5; this document is what the code
> does.

---

## 1. The chain

Five stages, four hops, one stream per hop. Every name comes from
[`libs/streams.py`](../src/defense/libs/streams.py), which exists because four
separate bugs in this system had the identical shape — one component writes a
string, another reads a different one, and the mismatch degrades to a silent
no-op rather than an error.

| # | Stage | Reads stream | Consumer group | Writes to | Module |
| - | ----- | ------------ | -------------- | --------- | ------ |
| 1 | **Ingestion** | `ingestion:queue` | `ingestion-workers` | `nlp:stage1:queue` | [`services/ingestion/`](../src/defense/services/ingestion/service.py) |
| 2 | **Stage-1 NLP** | `nlp:stage1:queue` | `stage1-nlp-group` | `router:queue` | [`workers/stage1_nlp/`](../src/defense/services/workers/stage1_nlp/worker.py) |
| 3 | **Router** | `router:queue` | `router-workers` | `llm:stage2:queue` | [`workers/router/`](../src/defense/services/workers/router/router.py) |
| 4 | **Stage-2 LLM** | `llm:stage2:queue` | `stage2-llm-workers` | `assembler:queue` | [`workers/stage2_llm/`](../src/defense/services/workers/stage2_llm/worker.py) |
| 5 | **Assembler** | `assembler:queue` | `assembler-group` | Postgres · ClickHouse · object storage | [`workers/assembler/`](../src/defense/services/workers/assembler/assembler.py) |

**The chain is linear. There is no bypass hop.** Read that row 3 carefully,
because it is the single most-misdocumented fact in this repository: the router
sends **every** post to `llm:stage2:queue`. What the gate decides is
*post-level* LLM work (summary, post-type, insight), carried on the envelope as
`task_flags.post_level_routed` — not whether the post reaches Stage 2 at all.

It used to be a real branch, and that was the bug: the entire comment ensemble
lives in Stage 2, so a bypassed post's comments were left with their Stage-1
label alone — three empty columns in the per-comment comparison, on the majority
of posts, with nothing saying why. `stats:llm_routed` still counts only the posts
that got post-level tasks, so `estimated_llm_share` keeps meaning what it always
meant. See [ROUTER.md](ROUTER.md) §4 and the routing-rate reading in
[evaluation.md](evaluation.md) §4.

> `router.py` imports `ASSEMBLER_QUEUE` and its module docstring still describes
> the old two-way dispatch. The constant is unused on the dispatch path — the
> only `XADD` in `_process_message` targets `STAGE2_QUEUE`.

Every stream and group name stays env-overridable (`INGESTION_STREAM`,
`ROUTER_GROUP`, …) because deployments legitimately shard streams. The defaults
live in exactly one place, and `tests/test_streams.py` asserts the KEDA
manifests under [`deploy/k8s/`](../deploy/k8s) agree with them — a rename now
breaks a test instead of silently disabling an autoscaler. That was bug #2: the
manifests named `stage1_nlp:queue` and `stage2-llm-group`, which no worker
creates, and a `redis-streams` KEDA trigger pointed at a nonexistent group
reports **no backlog** — so Stage 2, the one stage where scaling changes cost and
latency, never scaled while the manifests looked correct.

## 2. One envelope, mutated at each hop

Every stream entry is a single field, `data`, holding one JSON envelope. Stages
do not build fresh messages; they add to the one they were handed and pass it on.
That is why the assembler can validate a complete result without querying
anything back.

```text
ingestion  creates  { post_id, raw_post, normalized_post, options, job_id, tenant_id }
stage1     drops    raw_post                      (the bulky field; nothing downstream reads it)
           adds     stage1_result
router     adds     task_flags                    incl. post_level_routed
           mutates  stage1_result.comment_analysis in place — stage2_selection + per-comment
                                                   stage2_selected / stage2_skip_reason
stage2     adds     stage2_result                  (incl. processing.role_models)
           drops    task_flags                     (consumed; the decision is recorded in
                                                    stage2_result.processing)
assembler  reads    post_id, normalized_post, stage1_result, stage2_result
```

Payloads are serialised with `ensure_ascii=False` from Stage 1 onward. That is
not cosmetic: `\uXXXX`-escaping Bengali was *half* the token bill on the agent
path, and it triples the bytes on every stream entry here.

The **field names the router reads out of `stage1_result` are load-bearing** and
have broken twice. Every gate now reads through a named reader that accepts all
three shapes the pipeline emits, so a rename breaks a test rather than silently
disabling a gate ([ROUTER.md](ROUTER.md) §2).

## 3. What every stage does the same way

The four stream consumers share one base class,
[`libs/worker.py`](../src/defense/libs/worker.py) (`StreamWorker`), and four
cross-cutting behaviours.

**Consumer groups, created on start.** Each worker `XGROUP CREATE … MKSTREAM`s
its own input, tolerating `BUSYGROUP`. Workers scale horizontally per stage: N
replicas in one group split the stream.

**Cooperative cancellation.** The four stages ahead of the assembler call
`is_cancelled(redis, job_id)` as they pick a message up, and drop it instead of
doing the work. The assembler deliberately does *not* check — a post that
reaches it has had every model run on it already, so dropping it there would
discard work already paid for. What the assembler does instead is refuse to
write a cancelled job's row back to `running`/`done`. See [JOBS.md](JOBS.md) §2.

**Bounded retry, then dead-letter.** On failure,
[`libs/dlq.py`](../src/defense/libs/dlq.py) re-enqueues up to `max_retries`,
then writes the payload plus error context to `<stream>:dlq` — ACKing the
original either way, so the pending-entries list cannot grow without bound. The
attempt counter rides *inside* the payload (`_dlq_attempts`), because a
re-enqueued message gets a fresh stream id and a per-id counter would reset and
loop forever. At-least-once retry is safe because the assembler's writes are
idempotent ([ASSEMBLER.md](ASSEMBLER.md) §4).

**Progress events.** Each stage publishes one event as it picks a post up and
another as it hands it on, via
[`libs/progress.py`](../src/defense/libs/progress.py), onto
`analysis:progress:{job_id}`. `GET /v1/analysis/{job_id}/stream` relays any
event whose `event` field is not `done`/`error` verbatim, which is what the
dashboard's **Trace** tab renders — stage boundaries are otherwise invisible from
outside, because a post's only trace is a message sitting in a stream.

Three constraints shaped it, and each one is a rule for anyone adding an event:

- **Never fail the pipeline.** Publishing is best-effort and every call is
  wrapped. A Redis hiccup or a missing `job_id` degrades to no event, never to a
  dropped post. Progress reporting is not worth a retry.
- **Stay small.** `detail` is display-ready scalars, not a stage's full output.
  The canonical result is already fetchable from `GET /v1/analysis/{post_id}`;
  big payloads on a pub/sub channel would cost real throughput on a 100k batch.
- **Replayable.** Pub/sub has no backlog, and the first event is published by
  ingestion *during* the POST that creates the job — before the browser can
  possibly hold the job id and subscribe. So every event is also appended to a
  capped list, `job:{job_id}:stage_events`, which the SSE endpoint replays on
  connect. Each event carries a monotonic `seq` so a client can drop the
  duplicate when a frame arrives both replayed and live.

The stage rail the dashboard draws comes from `progress.STAGES` —
`("ingest", "stage1", "router", "stage2", "assembler")`.

## 4. Running the stages

`uv run run_all.py` starts all five plus the API, agents service and dashboard.
Individually:

```bash
uv run python -m defense.services.ingestion
uv run python -m defense.services.workers.stage1_nlp
uv run python -m defense.services.workers.router
uv run python -m defense.services.workers.stage2_llm
uv run python -m defense.services.workers.assembler
```

Queue depth per stage: `GET /v1/pipeline/stats`; live: `GET /v1/pipeline/stream`
(SSE) — the dashboard's **Pipeline** tab. DLQ depth per stream is on
`GET /v1/system/stats` ([SYSTEM_MONITOR.md](SYSTEM_MONITOR.md)). Full
operational reference: [run.md](run.md).

## 5. Ordering, and what is *not* guaranteed

- **Per-post ordering is guaranteed by construction** — a post moves through the
  chain as one envelope, and each stage writes the next entry only after
  finishing.
- **Across posts, nothing is ordered.** N replicas per stage means post 30 can
  reach the assembler before post 12. Consumers key on `post_id`, and job
  progress is a counter, not a cursor.
- **A stop is bounded, not instant.** The cost of a stop is the single post
  already mid-flight in each of four stages, not the rest of the batch.
- **Exactly-once is not attempted.** At-least-once plus idempotent writes is the
  contract: a retried post overwrites its own row rather than adding a second.

## 6. Evidence class

| Claim | State |
| ----- | ----- |
| Stream/group names agree with the KEDA manifests | ✅ Asserted by `tests/test_streams.py` |
| Stage boundaries are observable per post | ✅ Measured — Trace tab, `stage_events` replay |
| Cancellation bounds cost at one post per stage | ✅ Measured — [JOBS.md](JOBS.md) |
| Retry → DLQ policy | ✅ Tested; DLQ depth surfaced on `/v1/system/stats` |
| Per-stage latency (`stage1_ms`, `stage2_ms`, persist fan-out) | ✅ Measured per post, recorded on the result |
| Horizontal scaling changes throughput | 🟡 Code complete, **not benchmarked** — no multi-replica run has been timed |

Cross-references: [architecture.md](architecture.md) §3 (data flow), §8
(reliability) · [FEATURES.md](FEATURES.md) · [SYSTEM_MONITOR.md](SYSTEM_MONITOR.md) ·
[testing.md](testing.md).

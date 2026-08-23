# Jobs — Run, Stop, Resume, Delete

> **Scope.** The lifecycle of an analysis job: what a job *is* in a queue-based
> system, why "stop" is a flag rather than a kill, why "resume" has to be
> reconstructed from Postgres, and how progress reaches the browser.
>
> Code: [`api/routers/analysis.py`](../src/defense/services/api/routers/analysis.py)
> (the endpoints), [`libs/jobs.py`](../src/defense/libs/jobs.py) (the
> cancellation flag), [`libs/progress.py`](../src/defense/libs/progress.py) (stage
> events), [`workers/assembler/`](../src/defense/services/workers/assembler/assembler.py)
> (counter reconciliation). Rationale: [api_design.md](api_design.md) §3a,
> [architecture.md](architecture.md) §3 step 3.

---

## 1. A job is not a process

A job is **N envelopes sitting in the stage streams**, each picked up by whichever
worker replica is free. There is no PID, no task handle, and no way to pull a
message back out of a stream once `XADD` has accepted it. Every design decision
below follows from that one fact.

| Endpoint | Does |
| -------- | ---- |
| `POST /v1/analysis/run` | Create a `jobs` row, resolve the selector to posts, `XADD` one envelope per post to `nlp:stage1:queue`, record `job:{id}:total` |
| `GET /v1/analysis/{id}` | The job row + progress |
| `GET /v1/analysis/{id}/stream` | SSE: replayed + live stage events, progress, terminal `done`/`error` |
| `POST /v1/analysis/{id}/cancel` | Set the cancellation flag |
| `POST /v1/analysis/{id}/resume` | Re-enqueue only the posts that never finished |
| `DELETE /v1/analysis/{id}` | Delete the job (and stop it) |
| `GET /v1/analysis` · `/latest` · `/overview` · `/export` | Listing and result access |

A re-analysis run enters at **Stage 1**, not ingestion — there is no separate
analysis worker. The normal pipeline does the work and the assembler flips the
job to `done` via `job_id`.

**`options` is persisted on the job row**, because resume has to re-enqueue the
remaining posts with the *same* per-request options the run started with. A job
run with `want_summary` and resumed without it would finish with half its posts
summarised and nothing saying why. That column used to be written as a literal
`{}`, throwing the request detail away at enqueue time.

`llm_backend` is stored with the rest but deliberately **not trusted on resume**:
it is re-resolved against the tenant's policy, which may have been
privacy-locked in the meantime ([LLM_BACKENDS.md](LLM_BACKENDS.md) §3).

## 2. Stop is cooperative

`POST /v1/analysis/{id}/cancel` sets one Redis key —
`job:{job_id}:cancelled`, TTL 24h — and returns. The four stages ahead of the
assembler check it as they pick a message up and drop the message instead of
doing the work.

**The cost of a stop is therefore bounded by the single post already mid-flight
in each stage**, not by the rest of the batch. A 300-post job stops after the
handful currently being worked on.

Three deliberate choices:

- **The assembler does not check.** A post that reaches it has had every model
  run on it already, so dropping it there would discard work paid for either way.
  What the assembler does instead is refuse to write a cancelled job's row back to
  `running`/`done` — so `cancelled` is genuinely terminal and counter
  reconciliation cannot revive a job the operator stopped.
- **A flag, not a stream drain.** A consumer group's messages are not addressable
  by content: finding a job's envelopes would mean `XRANGE`-ing four streams and
  rewriting them, racing every worker reading at the same time.
- **The flag outlives the job row.** Deleting a job has to stop it too, and the
  in-flight envelopes still name a `job_id` that no longer exists in Postgres. So
  the flag carries its own TTL and is deliberately *not* deleted along with the
  job's other Redis keys (`purge_job_keys`).

Checks are best-effort in the same sense as progress publishing: a Redis failure
means the message gets **processed**, never that the pipeline breaks. A stop that
misses is recoverable; a stop that silently drops a post on a Redis hiccup is not.

## 3. Resume reconstructs from Postgres

Resume exists for the one failure the pipeline **cannot** detect on its own: the
machine lost power (or the workers were killed) with 30 of 300 posts analysed.
Nothing marks that job as dead — its row still reads `running`, its Redis counters
may be gone entirely, and the envelopes it had in flight were lost with the
streams. A plain re-run would re-analyse all 300 and pay for the 30 again.

So "what is left" is derived from **Postgres alone**, the only thing that survives
a power cut:

1. The job's **selector** gives the full post set.
2. A post counts as **done** when its `analysis_results` row was written at or
   after the job started.
3. The remaining posts are re-enqueued at Stage 1 with the job's original
   `options`.
4. The Redis counters are **rebuilt to match** — `total` = the whole set,
   `completed` = what is already done, `failed` reset — which is what lets the
   assembler finish the job when the remainder lands instead of flipping it done
   on the first one. Progress continues at **30/300** rather than restarting at 0.

`analysis_results` is keyed `UNIQUE (post_id)` with no job column, so that
timestamp is the only available discriminator. It means a post another job
re-analysed in the meantime also counts as done — which is correct: a fresh
result exists either way.

### What resume refuses to do

| Condition | Response |
| --------- | -------- |
| Job status `done` | **409** — nothing to resume |
| Job is non-terminal and its row was touched < **300 s** ago | **409** — *"still making progress … stop it first, then resume"* |
| Selector has neither `campaign_id` nor `post_ids` | **422** — no post set to resume; start a new run |

`jobs.updated_at` is the heartbeat: the assembler writes `running` to the row as
every post lands, so a job that is genuinely moving touches it continuously.
Nothing else in the system can tell a power-cut job from a slow one — a killed
process leaves no marker — so the threshold has to clear the **slowest single
post** (a full comment ensemble plus summary on a long thread), not the average
one. Below it a resume is refused, which is unambiguous.

**There is no per-post partial resume.** A post that was mid-flight is redone
from Stage 1.

> **Known, deliberate limitation.** The selector's optional `filter` block
> (platform / posted_from / posted_to / sentiment bounds) is stored on the job but
> has never been applied when resolving posts — a filtered run analyses the whole
> campaign. That is pre-existing behaviour, left alone so **resume selects exactly
> the set its original run did**. Fixing it changes which posts a run covers,
> which is a separate decision.

## 4. Progress and live streams

Two channels, one endpoint.

```
analysis:progress:{job_id}      pub/sub — stage events + progress + terminal done
job:{job_id}:stage_events       capped list — replayed on SSE connect
job:{job_id}:total|completed|failed   counters, TTL-bounded
```

`GET /v1/analysis/{id}/stream` replays the buffered stage events, then relays
live ones. Any event whose `event` field is not `done`/`error` passes through
verbatim, which is why a worker can add a stage frame with no API change. Each
event carries a monotonic `seq` so a client can drop the duplicate when a frame
arrives both replayed and live.

The assembler publishes `progress` per post and `done` once
`completed + failed >= total`. Its own trace frame goes out **before** that call,
because the terminal event closes the stream and a frame emitted after it is a
frame nobody sees.

Counter failure degrades rather than hangs: if Redis is unavailable the assembler
falls back to "first landing flips the job" instead of leaving the row `running`
forever.

The dashboard renders this as the **Analysis Jobs** tab (progress, stop/resume/
delete) and the **Trace** tab (the per-post stage rail) —
[DASHBOARD_UI.md](DASHBOARD_UI.md).

## 5. Statuses

| Status | Meaning |
| ------ | ------- |
| `queued` | Row created, envelopes enqueued |
| `running` | At least one post has landed; written by the assembler as each does |
| `done` | `completed + failed >= total`, with `completed > 0` |
| `failed` | Terminal with no completions, or a control-message failure (e.g. unconfigured upstream) |
| `cancelled` | Operator stopped it. **Terminal** — the assembler will not write over it |

## 6. Evidence class

| Claim | State |
| ----- | ----- |
| Stop bounds cost at one post per stage | ✅ Measured |
| `cancelled` is terminal against counter reconciliation | ✅ Measured |
| Resume re-enqueues only unfinished posts, progress continues | ✅ Measured |
| Resume refuses a live job (409) and a selector-less job (422) | ✅ Measured |
| `options` persisted and reused on resume | ✅ Measured |
| Backend re-resolved against tenant policy on resume | ✅ Measured |
| Stage events replay correctly for a late subscriber | ✅ Measured |
| Behaviour under a genuine mid-batch power cut | 🟡 Reasoned + tested, **not staged on real hardware** |

Cross-references: [PIPELINE.md](PIPELINE.md) · [INGESTION.md](INGESTION.md) ·
[ASSEMBLER.md](ASSEMBLER.md) · [api_design.md](api_design.md) §3a ·
[endpoints.md](endpoints.md) · [DASHBOARD_UI.md](DASHBOARD_UI.md).

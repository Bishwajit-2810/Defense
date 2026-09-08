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
   after the job started **and carries this job's id** (`processing.job_id`).
3. The remaining posts are re-enqueued at Stage 1 with the job's original
   `options`.
4. The Redis counters are **rebuilt to match** — `total` = the whole set,
   `completed` = what is already done, `failed` reset — which is what lets the
   assembler finish the job when the remainder lands instead of flipping it done
   on the first one. Progress continues at **30/300** rather than restarting at 0.

### Why step 2 needs provenance, not a timestamp

`analysis_results` is keyed `UNIQUE (post_id)` and upserted `ON CONFLICT
(post_id)`. A post analysed twice has **one** row, and the second run overwrites
the first; the only thing that moves is `updated_at`.

Until 29 Aug 2026 step 2 was the timestamp alone. That does not ask "did this job
finish this post" — it asks *"has anything touched this post since this job
started"*, and any concurrent re-run of the same post answers yes. This was
documented here as correct on the grounds that "a fresh result exists either
way". It is not correct, and the failure is not subtle:

> Job `e7ec04a6` analysed one post. A **different** run of the same post finished
> at 16:31:36 and upserted the row — seven seconds *before* this job's own post
> reached the router. Resume read that row, concluded the job was complete,
> flipped it to `done`, and returned `resumed: false`. The job's actual post was
> still inside Stage 2, and remained there.

The assembler now stamps `processing.job_id` onto every result, so the question
is answerable exactly. Three cases, and the API distinguishes all three:

| Row | Read as |
| --- | ------- |
| `processing.job_id` == this job | done, confirmed |
| `processing.job_id` == another job | **not** done — positive evidence this job's own post has not landed |
| `processing.job_id` key **absent** (written before stamping) | counts as done, matched by timestamp only — and the response's `reason` says so rather than presenting the guess as a fact |

The shortcut that flips a job to `done` also **writes the Redis counters** it
just asserted. It did not, and a `done` row with no `completed` counter is what
the Jobs tab drew as a confident **0%** next to it — see §4.2.

### What resume refuses to do

| Condition | Response |
| --------- | -------- |
| Job status `done` | **409** — nothing to resume |
| Job is non-terminal and **either** its row **or its pipeline** reported < **300 s** ago | **409** — *"still making progress … stop it first, then resume"* |
| Selector has neither `campaign_id` nor `post_ids` | **422** — no post set to resume; start a new run |

Liveness has **two** sources, and it needs both.

`jobs.updated_at` is written by the **assembler and by nobody else**, once per
post landing. So it is a heartbeat only for a job with many posts landing
steadily; for the minutes a single post spends in Stage 1 and Stage 2 it does not
move at all, and a one-post job's row is untouched from creation until it
finishes. Judged on that alone, a perfectly healthy job on a local LLM reads as
stalled after five minutes — and resume then re-enqueues a post that is still
being worked on, which is how two runs of one post end up racing.

The stage-event buffer is the per-post signal the row cannot be: every frame
carries wall-clock `ts` (§4), so the newest frame says when the pipeline last did
something for this job. Resume takes the **more recent** of the two. Absence of
frames is treated as *no evidence* — the buffer has a 1 h TTL — never as proof
the job is dead.

A killed process still leaves no marker, so the threshold must clear the
**slowest single post** (a full comment ensemble plus summary on a long thread).
Below it a resume is refused, which is unambiguous.

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
[DASHBOARD_UI.md](DASHBOARD_UI.md). The Trace tab's client holds that stream
**outside React** (`utils/traceSession.js`), because tabs are rendered
conditionally and leaving the tab would otherwise abandon a post mid-pipeline.

## 4.1 A job's report covers that job

The Jobs tab's per-row **Download Report** sends
`GET /v1/reports/export_latest?job_id={id}`, and the API resolves the job's own
`selector` — its `post_ids` when it has them, otherwise its campaign.

Until 29 Aug 2026 it sent only `job.campaign_id`. **A job created from explicit
`post_ids` has no campaign**, so the request fell through to `campaign_id=all`
and a one-post job downloaded a report over the whole corpus, named
`analysis_report_all.pdf`. The scope is now part of the request, part of the
filename, and printed on the PDF — [REPORTS.md](REPORTS.md) §1.1.

This is worth remembering as a *class* of bug rather than one incident: the
job selector is the only record of what a job was asked to do, and any feature
that answers "about this job" has to read it rather than infer it from a column
that happens to be populated for some jobs.

## 4.2 Progress has two sources, and the durable one is the status

`job:{id}:total` and `job:{id}:completed` are **two separate Redis keys with a
24 h TTL, and they expire independently**. That is the fact everything here turns
on. A finished job can be left holding either one alone, and on 29 Aug 2026 two
live jobs showed both halves at once:

| Job | Counters present | Rendered |
| --- | --- | --- |
| a 1-post `analysis_run` | `:total` only | **0%** |
| a 50-post `posts_upload` | `:completed` only | **—** |

Both had finished. Two different wrong answers, from the same missing data.

Three separate defects produced that, and each needed its own fix:

1. **The API fabricated the zero.** `list_analysis_jobs` coerced a missing
   `:completed` to `0` while reporting a missing `:total` as `null`. It now
   reports **both** as `null` — "the counter is gone" and "the counter says zero"
   are different facts, and a caller cannot distinguish them if the API will not.
2. **The tab rendered a number it did not have.** `total > 0 ? completed/total :
   (isFinished ? 100 : 0)` coerces `null` to `0` first, which is what erased the
   difference in the first place.
3. **Neither consulted the durable record.** The counters are ephemeral; the job
   *status* is a Postgres column that does not expire — and `done` **means** every
   post landed, because the assembler writes it only once
   `completed + failed >= total`.

So the rule is now: a real ratio when both counters are present; otherwise a
terminal `done` status is read as **100%**, marked with a `*` and a tooltip
saying it is derived from the status rather than counted. `failed` and
`cancelled` are *not* complete and never get the full bar — a stopped job's
progress is genuinely lost, and it says **—**. A running job with no counters yet
says **—**. A counter that exists and reads zero is a measurement and still
renders 0%, and real counters win over the status even when they contradict it:
a measurement beats an inference, and the contradiction is worth seeing.

`post_count`, derived from the job's selector rather than from Redis, is the
durable answer to "how many posts was this job for" and is what the Posts column
falls back to.

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
| A job's report is scoped to that job's posts | ✅ Measured — [`tests/test_report_scope.py`](../tests/test_report_scope.py) |
| Resume attributes a result to the job that produced it, not to a timestamp | ✅ Measured — `tests/test_job_lifecycle.py` |
| A completion recorded outside the assembler still writes the counters | ✅ Measured |
| Liveness reads the pipeline's frames, not only the jobs row | ✅ Measured |
| A missing counter is reported as null, never as 0 | ✅ Measured — `tests/test_job_lifecycle.py` |
| A finished job reads 100% from its status when the counters cannot answer | ✅ Measured — `dashboard/src/pages/AnalysisJobs.test.jsx` |
| Cancelled and failed jobs never render as complete | ✅ Measured |
| Behaviour under a genuine mid-batch power cut | 🟡 Reasoned + tested, **not staged on real hardware** |

Cross-references: [PIPELINE.md](PIPELINE.md) · [INGESTION.md](INGESTION.md) ·
[ASSEMBLER.md](ASSEMBLER.md) · [api_design.md](api_design.md) §3a ·
[endpoints.md](endpoints.md) · [DASHBOARD_UI.md](DASHBOARD_UI.md).

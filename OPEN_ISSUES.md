# Open Issues — Pass 6 remediation list

> **Superseded in part.** A fourth pass (12–13 August 2026) found eleven more, two of them
> regressions of items fixed here: the routing gate (below) and issue 4 (report cluster
> summaries computed, paid for and discarded). It also found that the contract guards this
> pass left behind had _all_ stopped running. See **[AUDIT_PASS7.md](AUDIT_PASS7.md)**,
> then [AUDIT_PASS8.md](AUDIT_PASS8.md) and [AUDIT_PASS9.md](AUDIT_PASS9.md)
> (14 August — multi-tenant data isolation across Postgres, ClickHouse, the routers,
> the worker envelopes and the retrieval MCP).
>
> **Two later changes touch items in this file (17 August 2026), neither of them a
> regression:**
>
> - **The Stage-2 comment set is chosen by the router**, not by the stance pass:
>   `ROUTER_COMMENT_TOP_N` (default **0 = every comment with text**) is applied once
>   and *every* voter reads that set. The cap this file discusses
>   (`COMMENT_STANCE_MAX_PER_POST`) also stays at 0 — a positive value gives the LLM
>   fewer comments than the seven cheap heads got, which is the hole the
>   router-side selection exists to close. If a positive cap is set for speed, the
>   comments below the cut are kept, persisted, and reported as
>   `ensemble.not_analysed` with `uncertain` labels.
> - **Stage 1's label is no longer a voter, and `_seed_heuristic_vote` is gone.**
>   It was dead code for a while (defined, unit-tested, never called); it was
>   briefly wired up for the comments the router left out; it is now deleted and
>   `heuristic` is out of `ensemble.CHEAP_SOURCES`. **Only a model may label a
>   comment.** The rule is that a voter which answers on every comment — and whose
>   answers are 71.3% deterministic stub or emoji rule (§6.2) — can never abstain,
>   which makes `abstained` / `unread` / `single_voter` unfalsifiable. **Consequence
>   to watch:** every comment outside the router's top-N now reports `uncertain` at
>   zero voters, so on a thread much larger than `ROUTER_COMMENT_TOP_N` the
>   post-level `sentiment_breakdown` is mostly `uncertain`. That is accurate, and it
>   is a reporting change worth knowing before showing a chart.

**Found:** 5 August 2026, fresh-eyes audit of the whole tree (the third such pass).
**Status: ALL TEN ARE FIXED** and regression-tested, 5 August 2026.
**Full write-ups:** [PROJECT_ASSESSMENT.md](PROJECT_ASSESSMENT.md) §13. This file is
the actionable version — what changed, how it was proved, what test now guards it.

Test suite: **597 → 673 passing, 34 → 40 files.** Each fix was mutation-tested by
reverting it and confirming the new tests fail; the one that initially survived
its mutation (issue 2 — structural tests pass if `chat()` imports the counter and
never calls it) gained a behavioural test that drives the real method.

The decisions this list left open were resolved as: **issue 1 → enforce**, not
merely restate; **issue 5 → route reuse through the assembler** so all three
stores are written.

**The dashboard was then checked against the changed API and had three gaps of
its own** — see [UI sync](#ui-sync) and PROJECT_ASSESSMENT §13.9. One of them was
opened by issue 5's own fix, which is §13.8's pattern recurring inside its own
remediation.

---

## At a glance

| #                                                                        | Issue                                            | Impact                                            | Status                                          | Blocked the defense? |
| ------------------------------------------------------------------------ | ------------------------------------------------ | ------------------------------------------------- | ----------------------------------------------- | -------------------- |
| [1](#1--the-privacy-lock-does-not-cover-the-analysis-pipeline)           | Privacy lock doesn't cover the analysis pipeline | **Credibility** — the strongest claim in the deck | ✅ **Fixed** — enforced via the envelope        | —                    |
| [2](#2--v1usage-counts-stage-2-only)                                     | `/v1/usage` counts Stage 2 only, says otherwise  | **Thesis** — undercounts the cost number          | ✅ **Fixed** — tracking moved into `LLMClient`  | —                    |
| [3](#3--embedding_is_stub-is-false-on-every-row-the-pipeline-writes)     | `embedding_is_stub` is `FALSE` on every row      | Honesty flag reports the inverse                  | ✅ **Fixed** — flag carried, not inferred       | —                    |
| [4](#4--report-cluster-summaries-are-computed-paid-for-and-discarded)    | Report cluster summaries computed then discarded | Real LLM spend, zero output                       | ✅ **Fixed** — surfaced through API + dashboard | —                    |
| [5](#5--near-duplicate-reuse-copies-the-analysis-but-not-its-provenance) | Near-dup reuse copies the wrong things           | Wrong data in a store, on by default              | ✅ **Fixed** — composed via the assembler       | —                    |
| [6](#6--the-agent-runner-bypasses-every-llmclient-resilience-path)       | Agent runner bypasses breaker/failover/usage     | Agents hard-fail where everything else degrades   | ✅ **Fixed** — `tools` passthrough on `chat()`  | —                    |
| [7a–d](#7--smaller-issues)                                               | Four smaller ones                                | Bounded                                           | ✅ **All fixed**                                | —                    |

Fixed in the order below; see [What changed](#what-changed) for the per-issue landing notes.

---

## 1 — The privacy lock does not cover the analysis pipeline

> ✅ **FIXED** — enforced, not merely restated. See [what landed](#what-landed-1--privacy-lock-enforced).
>
> §13.5 · **[read]** · the local⇄Groq policy is what §3.3 calls _"the best design
> decision in the project"_, so this is the one item here that is a credibility
> risk rather than a correctness one.

### Where

- [deps.py:413](src/defense/services/api/deps.py#L413) — `check_llm_backend_policy`
- [config.py:87](src/defense/services/api/routers/config.py#L87) — `PUT /v1/config/llm`
- [stage2_llm/worker.py:991](src/defense/services/workers/stage2_llm/worker.py#L991) — reads `config:llm_backend`
- [stage1_nlp/worker.py:89](src/defense/services/workers/stage1_nlp/worker.py#L89) — same key

### What's wrong

Two halves that do not meet.

**The guarded knob is dead.** `check_llm_backend_policy` returns immediately
unless `options["llm_backend"] == "groq"`. `POST /v1/analysis` and `POST /v1/ingest`
both call it with the request options — but grepping `src/defense/services/workers/` for
`llm_backend` shows **no consumer**. Stage 1 and Stage 2 each resolve their backend
from the global Redis key `config:llm_backend`, falling back to the `LLM_BACKEND`
env var. The per-request option is accepted, policy-checked, written into the job
envelope, and never read. Its only observable effect is the 403.

**The deciding knob is unguarded.** `PUT /v1/config/llm` sets `config:llm_backend`
with no `check_llm_backend_policy` call and no role check. Any authenticated
principal can flip it, and it applies **process-wide to every tenant's posts**.
Nothing in the pipeline carries a tenant id, so there is no point at which a
locked tenant's post could be excluded even in principle.

Net: a privacy-locked tenant's post content goes to Groq whenever the global
toggle points there, while `POST /v1/analysis` with `llm_backend:"groq"` still
returns a 403 that reads as the guarantee working.

### Reproduce

```bash
grep -rn "llm_backend" src/defense/services/workers/ | grep -v '_llm_backend"'
# → only config:llm_backend reads and processing-provenance writes.
#   No worker reads options["llm_backend"].
```

### Fix — pick one, and say which

**(a) Restate it honestly (small, do this before the defense regardless).**
The pipeline is single-tenant; the backend policy is enforced on the interactive
surfaces (`/v1/chat`, agents) and not on batch analysis. This is the same move
§12.5 already made for `analysis_run`'s missing tenant scoping. Already applied to
[FEATURES.md](FEATURES.md) §6, [architecture.md](architecture.md) §9 and
[endpoints.md](endpoints.md) by this pass — but the _code_ should say it too, in a
comment on `check_llm_backend_policy` naming what it does and does not cover.

**(b) Enforce it (larger).**

1. Guard the toggle: call `check_llm_backend_policy` in `put_llm_config`, and gate
   it on an admin role. A global switch should not be settable by a tenant user.
2. Carry the tenant through the pipeline: add `tenant_id` to the ingestion/analysis
   envelope, and have the Stage-2 worker resolve `enforce_policy(tenant_policy,
config:llm_backend)` per message via [src/defense/libs/llm/policy.py](src/defense/libs/llm/policy.py)
   — which already exists and is already used by the agent runner.
3. Then either delete `options["llm_backend"]` or make Stage 1/2 honour it.

Do **not** ship (b) half-done: a per-request option that is checked but not read
is exactly the shape of the bug.

### Test to leave behind

Assert the _contract_, per §11.6: for every knob `check_llm_backend_policy`
inspects, some worker must read it. A test that greps `src/defense/services/workers/` for the
option key and fails when nothing consumes it would have caught this on the day it
was introduced.

---

## 2 — `/v1/usage` counts Stage 2 only

> ✅ **FIXED** — see [what landed](#what-landed-2--usage-counters-moved-down-a-layer).
>
> §13.4 · **[measured]** · `estimated_cost_usd` is the number the cost-efficiency
> thesis rests on.

### Where

- [stage2_llm/worker.py:210](src/defense/services/workers/stage2_llm/worker.py#L210) — `_track_usage`, the only writer
- [usage.py](src/defense/services/api/routers/usage.py) — the reader and its `scope_note`

### What's wrong

`_track_usage` writes `usage:tokens:total`, `usage:llm_calls`, `usage:cache_hits`,
`usage:tokens:{backend}:{model}` and the lane counters. It exists in exactly one
file. Five other LLM call sites never touch a counter:

| Call site                                                                                                                                                 | When it runs                                    | Volume                 |
| --------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------- | ---------------------- |
| [chat.py:202](src/defense/services/api/routers/chat.py#L202), [:260](src/defense/services/api/routers/chat.py#L260)                                       | every chatbot turn                              | unbounded, user-driven |
| [reports.py:216](src/defense/services/api/routers/reports.py#L216) `_llm_narrative`                                                                       | every grounded report (the default)             | 1 per report           |
| [reports.py:354](src/defense/services/api/routers/reports.py#L354) `_summarize_cluster`                                                                   | every grounded report                           | up to 8 per report     |
| [llm_analyzer.py:225](src/defense/services/workers/stage1_nlp/llm_analyzer.py#L225), [:292](src/defense/services/workers/stage1_nlp/llm_analyzer.py#L292) | when `STAGE1_LLM=true` — **the shipped config** | per post               |
| [runner.py:409](src/defense/services/agents/runner.py#L409) `_llm_chat_with_tools`                                                                        | every agent turn                                | per tool-calling turn  |

Meanwhile `UsageResponse.total_tokens` says _"Total tokens spent"_, the endpoint
docstring says _"Every token/cost/cache figure therefore comes from Redis"_, and
`scope_note` — the field §5.8 added **specifically** to state what the numbers
cover — returns the flat string `"All figures are system-wide."`

It matters most in the shipped configuration: with `STAGE1_LLM=true`, Stage-1
comment labelling is uncounted, and §6.8 measured comment-level calls at **96%** of
the total there.

### Reproduce

```bash
grep -rn "_track_usage" --include=*.py src/defense/services/ src/defense/libs/ | grep -v tests/
# → src/defense/services/workers/stage2_llm/worker.py only.
```

### Fix

Move tracking **into `LLMClient.chat` / `chat_stream`** rather than adding five
call sites — that is the layer every caller already goes through, and it makes the
next caller correct by default. `_track_usage` needs a Redis handle and a `lane`/
`task`; pass them as optional kwargs so the Stage-2 worker keeps its existing
dimensions and other callers get a sensible default (`lane="interactive"`,
`task="chat"` / `"report"` / `"agent"` / `"stage1"`).

Two things to preserve while doing it:

- `usage:llm_calls` must stay **fresh-only** (it is `cache_hit_rate`'s
  denominator); `usage:calls:task:{task}` counts cached and fresh alike. §12.4b
  fixed this distinction — don't undo it.
- `agents/runner.py` bypasses `LLMClient.chat` entirely (issue 6), so it needs its
  own call or needs fixing first.

**Cheapest honest interim, if the above is too large right now:** make `scope_note`
say `"Token, cost and cache figures cover Stage-2 pipeline work only; chat, report
and agent LLM calls are not counted."` A wrong number that says so is survivable;
one that claims to be system-wide is not.

### Test to leave behind

Same shape as §11.3's AST test: assert via the AST that every module calling
`LLMClient().chat(` either calls `_track_usage` or appears on an explicit
opt-out allowlist. Then a new uncounted caller fails a test instead of quietly
shrinking the cost figure.

---

## 3 — `embedding_is_stub` is `FALSE` on every row the pipeline writes

> ✅ **FIXED** — re-probed against Postgres 16: the column now reads `True`.
>
> §13.2 · **[probed]** against real Postgres 16 + pgvector · this is §9.11
> consequence 1 reappearing one layer down.

### Where

- [persistence.py:55](src/defense/services/workers/assembler/persistence.py#L55) — `_resolve_embedding`, writes the DB column
- [assembler.py:295](src/defense/services/workers/assembler/assembler.py#L295) — the trace frame, computes the _same flag differently_

### What's wrong

Two independent computations of one flag, and they disagree:

| Site                                   | Rule                                                            | Value in stub mode |
| -------------------------------------- | --------------------------------------------------------------- | ------------------ |
| `assembler.py:295` (trace frame / log) | `processing.stub_mode or not stage1_embedding`                  | **True** ✓         |
| `_resolve_embedding` (the DB column)   | dimension check: `len(embedding) == EMBEDDING_DIM → not a stub` | **False** ✗        |

With `MODEL_STUB_MODE=true` — the default — Stage 1 emits `stub_embedding(text)`,
a 768-dim hash vector. Its dimension is correct, so persistence classifies it as a
real Stage-1 vector. The `is_stub=True` branch fires **only** when Stage 1 supplied
no embedding or a wrong-dimension one — i.e. the one case the vector is
persistence's own `post_id`-seeded fallback rather than a Stage-1 stub. The column
is close to the inverse of its documented meaning.

**Why it has stayed invisible:** `_semantic_search` ORs the row flag with
`query_is_stub`, and in a fully-stubbed deployment the query is a stub too — so the
response is right for the wrong reason. It breaks in the exact sequence a demo
follows: analyse the corpus in stub mode (fast), then install the `ml` extra and
set `MODEL_STUB_MODE=false` to show semantic search. The query is then real,
`stub_rows` counts 0, the `semantic_search_over_stub_vectors` warning never fires,
and every hit reports `embedding_is_stub: false` over a corpus of hash noise.

This also silently governs issue 4 — `_embedding_clusters` clusters those same
vectors, and nothing in the report says the clusters are noise.

### Reproduce

Needs a real Postgres (see the throwaway-container recipe in §12's method note):

```
stage1 embedding dims: 768
stage1 vector IS the hash stub? True
assembler trace frame says embedding_is_stub = True
DB column (what /v1/search returns) says      = False
```

### Fix

Stop deriving provenance from shape. `embed_text_with_provenance` **already
returns the honest boolean** — Stage 1 should carry it on its result
(`embedding_is_stub`, beside `embedding`), the assembler should pass it to
`persist_postgres`, and `_resolve_embedding` should use it, falling back to the
dimension heuristic only when the producer didn't say. Then delete the second
computation in `assembler.py:295` and read the same value the column gets, so the
trace and the column cannot drift apart again.

### Test to leave behind

End-to-end, in the shape [tests/test_provenance_survives.py](tests/test_provenance_survives.py)
already uses for the `processing` block: with `MODEL_STUB_MODE=true`, run Stage 1 →
assembler → `persist_postgres` and assert the persisted column is `True`. A per-hop
test would catch only half of this — which is precisely how it survived §9.11.

---

## 4 — Report cluster summaries are computed, paid for, and discarded

> ✅ **FIXED** — see [what landed](#what-landed-4--the-expensive-artefact-reaches-a-reader).
>
> §13.1 · **[measured]** · this is §11.1's defect (Stage-2 `insight`) one layer up,
> in the feature architecture.md §5 names **the LLM cost lever**.

### Where

- [reports.py:240](src/defense/services/api/routers/reports.py#L240) — `_embedding_clusters` (the producer)
- [reports.py:472](src/defense/services/api/routers/reports.py#L472) — writes `content["embedding_clusters"]`
- [models.py:367](src/defense/services/api/models.py#L367) — `ReportResponse` (no such field)
- [reports.py:53](src/defense/services/api/routers/reports.py#L53) — `_row_to_report` (never reads it)

### What's wrong

`_embedding_clusters` pulls up to `REPORT_CLUSTER_SAMPLE_CAP` (1500) embeddings,
k-means them (`MAX_CLUSTERS = 8`), and spends **one LLM-B call per cluster**. Then:

- `ReportResponse` declares no `embedding_clusters` field, and FastAPI's
  `response_model` strips undeclared keys → `POST /v1/reports` does not return it;
- `_row_to_report` never reads the key → `GET /v1/reports` and
  `GET /v1/reports/{id}` do not return it;
- `embedding_clusters` appears **nowhere** in `dashboard/app.js`.

Its only existence is the raw `jobs.options` JSONB. The `clusters` field the API
_does_ return is `_generate_report_content`'s topic-count aggregate — pure SQL,
zero LLM calls. **The report renders the cheap artefact and discards the expensive
one.**

### Reproduce

```python
from models import ReportResponse
"embedding_clusters" in ReportResponse.model_fields   # → False
```

### Fix

Add `embedding_clusters: Optional[List[Dict[str, Any]]] = None` to
`ReportResponse`, populate it in `_row_to_report` from `content`, return it from
`create_report`, and render it in the dashboard's Reports tab next to `clusters` —
labelled as the embedding clusters, so the two are not confused.

While there: the summaries are only meaningful once issue 3 is fixed. Have the
report state `embedding_is_stub` for the corpus it clustered, or suppress the
cluster block entirely when the vectors are stubs. Paying for a summary of noise
and _labelling_ it is honest; paying for it silently is not.

### Test to leave behind

Assert the consumer, not the producer: build a report `content` dict containing
`embedding_clusters` and assert it survives `_row_to_report` **and** the
`ReportResponse` round-trip. `model_fields` membership alone is enough to pin it.

---

## 5 — Near-duplicate reuse copies the analysis but not its provenance

> ✅ **FIXED** — reuse is now composed by the assembler, not copied by SQL.
>
> §13.3 · **[probed]** against real Postgres · on by default.

### Where

[service.py:263](src/defense/services/ingestion/service.py#L263) — `_reuse_analysis`

### What's wrong

Three problems in one function:

1. **The stub flag is dropped.** The INSERT column list is `(post_id, campaign_id,
result, embedding, schema_version, created_at, updated_at)` — `embedding_is_stub`
   is not in it, so the copy takes the column `DEFAULT FALSE`, and the
   `ON CONFLICT DO UPDATE` branch doesn't set it either. A source row honestly
   flagged `TRUE` produces a copy claiming `FALSE`:

   ```
    post_id | embedding_is_stub | result_post_id
   ---------+-------------------+----------------
    new     | f                 | src
    src     | t                 | src
   ```

2. **The copied document names the wrong post.** `result` is copied verbatim, so
   the new row's canonical JSON keeps the **source's** `post_id`, `post_text`,
   `engagement` and `reaction_breakdown`. `/v1/search` returns that raw `result`
   dict, so a hit on the new post carries a document describing a different one.
   Reactions and comment counts are per-post _facts_, not analysis — two posts can
   share a caption and have nothing else in common, which is the normal case for a
   repost.

3. **Only Postgres is written.** No ClickHouse row, no object-storage blob. A
   reused post is invisible to every analytics aggregate (trend, top posts,
   reaction mix, sentiment-over-time) while still counting in the Postgres-backed
   `/v1/reports` and `/v1/usage` numbers. The two stores disagree by construction.

**Not a rare path.** `NEAR_DUP_DEDUP` defaults to `true` and identical text yields
an identical stub vector, so cosine is exactly 1.0 ≥ the 0.97 threshold. Any repost
with the same caption takes it.

### Fix

1. Add `embedding_is_stub` to both the INSERT column list and the `DO UPDATE` set.
2. Rewrite the identity and engagement fields in the copied `result` before
   insert — `post_id`, `post_text`, `engagement`, `reaction_breakdown`,
   `created_at`, `platform_post_id`, `url` all belong to the _new_ post; only the
   analysis (sentiment, topics, summary, comment analysis) is legitimately reused.
   Add a `reused_from: {source_post_id, score}` provenance block, so the result
   says it was reused rather than looking like a fresh analysis.
3. Decide explicitly whether reused posts should reach ClickHouse/object storage.
   They should, or the analytics numbers are wrong by however many reposts the
   corpus contains — and say so in [FEATURES.md](FEATURES.md) either way.

### Test to leave behind

`tests/test_near_dup.py` currently uses a `FakeSession` returning canned rows, so
this SQL has **never been executed by the suite** — which is why all three
survived. Either add a Postgres-backed test (the throwaway container takes under a
minute) or, at minimum, assert on the generated SQL text that the column list
includes `embedding_is_stub`, in the style of
[tests/test_analytics_storage_contract.py](tests/test_analytics_storage_contract.py).

---

## 6 — The agent runner bypasses every LLMClient resilience path

> ✅ **FIXED** — `LLMClient.chat` forwards `tools`; the bypass is gone.
>
> §13.6 · **[read]**

### Where

[runner.py:409](src/defense/services/agents/runner.py#L409) — `_llm_chat_with_tools`

### What's wrong

It reaches past `LLMClient.chat` into `self.llm._get_client(...)` and calls
`oai_client.chat.completions.create` directly, because `chat()` does not forward
tool definitions. The docstring is accurate about what that preserves — _"policy
enforcement and model resolution"_ — and silent about what it drops:

- **the circuit breaker** — no `record_success` / `record_failure`, so agent
  traffic can neither open Groq's breaker nor respect it. §11.4b and §12.4a were
  both fixes for exactly this class of gap in `chat` / `chat_stream`;
- **the Groq→local failover** — every other caller degrades; agents hard-fail;
- **truncation continuation** (§6.1) and the **JSON-mode degeneracy retry** — an
  agent turn that hits the token ceiling is silently cut;
- **usage tracking** — issue 2's fifth row.

### Fix

Add a `tools` / `tool_choice` passthrough to `LLMClient.chat` and return
`tool_calls` on the response dict, then delete `_llm_chat_with_tools` and call
`chat()`. That is a smaller change than it looks — the tool-call normalisation
already in the runner moves into the client — and it removes the reason anyone
would reach for the private helpers again.

### Test to leave behind

Assert no module outside `src/defense/libs/llm/` references `_get_client`, `_resolve_model` or
`_breakers`. A private-helper firewall is the general form of this bug.

---

## 7 — Smaller issues

> ✅ **All four FIXED.**

### 7a — `config.py`'s model table is missing the `summary` role

[config.py:36](src/defense/services/api/routers/config.py#L36) `_MODEL_ENVS` is a
hand-maintained mirror of `client.py`'s role→model tables (its comment says so).
It carries `stage1`/`stage2`/`llm_a`/`llm_b`/`vlm` and **omits `summary`** — the
role §6.5 added so summaries get a stronger model. So `GET /v1/config/llm` cannot
show which model writes summaries, which is the headline of that requirement. The
ten values it does carry all agree with `client.py` today (verified by diff).

**Fix:** delete the mirror and build the response from
`LLMClient.default_model(role, backend)` over `VALID_ROLES` — the public accessor
that already exists. **Test:** assert the endpoint's role set equals
`client.VALID_ROLES`.

### 7b — `pipeline.py` hardcodes stream and consumer-group names

[pipeline.py:34](src/defense/services/api/routers/pipeline.py#L34) `_STAGES` hardcodes all five
stream **and** group names as string literals instead of importing
[src/defense/libs/streams.py](src/defense/libs/streams.py). They match the defaults today, but every name
is env-overridable _by design_ ("deployments legitimately shard streams"), and
under any override `_stage_stats` swallows the `xinfo_groups` error and returns
zeros — so the dashboard's Pipeline tab renders an **idle, healthy** pipeline while
work is queued. That is the §5.5 KEDA failure mode reproduced in the monitoring
view.

**Fix:** build `_STAGES` from `streams.ALL`. **Test:**
`test_workers_import_their_names_from_libs_streams` covers the five workers and not
this file — extend it. It is the last un-pinned copy of those identifiers.

### 7c — An ingestion docstring describes the opposite of its code

[service.py:657](src/defense/services/ingestion/service.py#L657) `_ensure_consumer_group`'s
docstring says it uses `"$"` _"so we only process messages that arrive after the
service starts"_, and offers `"0"` as the change to make for reprocessing. The code
passes `id="0"`. The comment describes the opposite of the behaviour, and its
remediation advice describes the state it is already in.

**Fix:** correct the docstring (the `id="0"` behaviour is the right one — it is
what lets a restarted service pick up a backlog).

### 7d — `lane_split` serializes token counts as floats

[usage.py:109](src/defense/services/api/routers/usage.py#L109) `UsageResponse.lane_split` is
typed `Dict[str, Dict[str, float]]`, so the integer call and token counts inside it
serialize as `{"calls": 123.0, "tokens": 45678.0}`.

**Fix:** widen to `Dict[str, Dict[str, float | int]]`, or model the entry as a
small `BaseModel` with `calls: int`, `tokens: int`, `token_share: float`,
`call_share: float`.

---

## Not a bug — recorded so it is not re-raised

`_reuse_analysis` issues `INSERT … SELECT … ORDER BY … LIMIT 1 ON CONFLICT …`,
which looks like it should need a CTE or subquery wrapper. **It does not** —
PostgreSQL 16 parses and executes it correctly (`INSERT 0 1`). Ten seconds against
a throwaway container settled what would otherwise have been an argument.

---

## What changed

### What landed (1) — privacy lock, enforced

`deps.resolve_llm_backend` resolves the backend the job will **actually** run on
(request > toggle > env) at enqueue time, where the tenant is known, applies the
lock, and the resolved value is stamped into the job envelope. Stage 1 and
Stage 2 now read `options["llm_backend"]` first and fall back to the global
toggle only for envelopes written before this. `PUT /v1/config/llm` requires an
admin role to select `groq` and refuses it outright for a locked tenant.

Two asymmetric outcomes, deliberately: an **explicit** request for `groq` is
refused (403 — it is the caller's own request), while a **global toggle** set to
`groq` is silently downgraded to `local` for a locked tenant. 403-ing them would
take a locked tenant offline whenever an operator flipped a switch they do not
control, and the guarantee is "their content never leaves the local backend",
not "they get an error".

`tests/test_privacy_lock_covers_pipeline.py` (14 tests), including that both
enqueue paths stamp the decision and both workers prefer it.

### What landed (2) — usage counters, moved down a layer

`src/defense/libs/llm/usage.py` is new and `LLMClient.chat` / `chat_stream` record every call
themselves, so a **new call site is counted without its author knowing a counter
exists** — which is the only version of this that stays true, since §13.4 happened
because five call sites accumulated and nothing made the omission visible. Lanes
gained `stage1`, `interactive` and `agent`; `pipeline_tokens` isolates the
per-post model from per-question interactive spend so §6.8's figure cannot be
inflated by a chatbot session. §12.4b's deliberate asymmetry is preserved and
pinned: `usage:llm_calls` stays fresh-only, `usage:calls:task:{task}` counts both.

`tests/test_usage_covers_every_caller.py` (20 tests): an AST check that nothing
outside `src/defense/libs/llm/` calls the OpenAI SDK or touches the client's private helpers,
plus behavioural tests that drive the real `chat()` and watch the counters fire.

### What landed (3) — provenance carried, not inferred

`text_analyzer._embed_with_provenance` returns `(vector, is_stub)`, Stage 1 puts
it on its result, and the assembler computes **one** value used for both the
trace frame and the database column — the two independent computations that
disagreed are gone. `_resolve_embedding` takes the producer's answer and keeps
the dimension heuristic only as a fallback. A failed real model now returns a
stub _and says so_, where it used to return an empty vector that silently became
a different, `post_id`-seeded stub; the prototype topic classifier is gated on
the flag rather than on that emptiness, so it still never runs on hash noise.

Re-probed against a real Postgres 16 + pgvector: `DB embedding_is_stub = True`.

### What landed (4) — the expensive artefact reaches a reader

`embedding_clusters` and `embedding_clusters_are_stub` are declared on
`ReportResponse`, read back by `_row_to_report`, returned by all three report
endpoints, and rendered in the dashboard's Reports tab as their own section,
labelled `llm` and separate from the zero-cost SQL topic aggregate. Clustering
over stub vectors is disclosed both in the response and above the summaries in
the UI, so a summary of noise is never presented as a finding.

`tests/test_report_clusters_survive.py` (7 tests) asserts from the consumer end,
including the last hop into `dashboard/app.js` — the hop §11.1 missed and §12.4d
had to add later for `insight`.

### What landed (5) — reuse composes instead of copying

`builder.build_reused_result` composes the document: identity, engagement,
reactions and timestamps from **this** post, post-level analysis from the source,
`processing.reused_from` provenance, and stage timings zeroed so no reused post
contributes fake latency. Ingestion hands it to the **assembler**
(`_enqueue_reuse`) rather than INSERTing a row, so all three stores are written
and reused posts stop being invisible to every ClickHouse aggregate. The stub
flag is read explicitly from the source row and carried.

**The comment thread is not reused.** A near-duplicate is a _caption_ match; the
two threads are different people saying different things, so inheriting the
source's per-comment labels would be fabricated data about comments nobody read.
The new post's thread is reported unanalysed, with a provenance note saying why.

`tests/test_near_dup_reuse_identity.py` (11 tests), and `tests/test_near_dup.py`
no longer drives dead SQL through a `FakeSession`.

### What landed (6) — one door into the LLM

`LLMClient.chat` forwards `tools`/`tool_choice` and returns normalised
`tool_calls`; continuation is skipped for a reply carrying tool calls, since the
model stopped to call a tool rather than running out of room. The runner's
`_llm_chat_with_tools` is now a thin adapter over `chat()`, so agents get the
circuit breaker, the Groq→local failover, truncation recovery, the degeneracy
retry and usage tracking — all four things the bypass cost them.

### What landed (7)

- **7a** — `config.py`'s mirrored model table is deleted; `_models()` derives
  from `LLMClient.default_model` over `VALID_ROLES`, so `summary` appears and any
  future role does too, automatically.
- **7b** — `pipeline.py::_STAGES` is built from `streams.ALL`, and
  `tests/test_streams.py` now covers it — the last un-pinned copy of those names.
- **7c** — the `_ensure_consumer_group` docstring describes `"0"`, which is what
  the code does, and says why.
- **7d** — `lane_split` entries are a `LaneUsage` model: `calls` and `tokens` are
  `int`, shares are `float`.

## UI sync

The fixes changed the API surface, so `dashboard/app.js` was audited against it
rather than assumed correct. Three gaps, all §13.8's rule:

| Gap                                                                                                                                                                | Fixed by                                                                                                                                                                               |
| ------------------------------------------------------------------------------------------------------------------------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| The "Cost split" card read 2 of 5 lanes, and its comment-share denominator silently grew to span chat and agent traffic while still being labelled post-vs-comment | Share computed over pipeline lanes only; a "Spend by lane" breakdown names every lane with a hint table mirroring `src/defense/libs/llm/usage.py`                                      |
| `pipeline_tokens` unrendered — the token card showed a total that now includes per-question chat/agent spend                                                       | Its own stat card, with a subtitle stating what the total covers that it does not                                                                                                      |
| `processing.reused_from` rendered nowhere — a reused post was indistinguishable from a cheap analysis, and its `0 analyzed` comment section gave no reason         | `near-dup` tag in the post list, a "Reused Analysis" block in the modal, a `reused` row in the Trace tab, and a "thread was not analysed" note from `comment_analysis.provenance.note` |

Pinned by `tests/test_dashboard_renders_api_fields.py`. Mutation-testing that
file exposed a flaw in **the test**: it scanned the raw source, and this codebase
comments heavily using the same field names, so deleting every real read of
`reused_from` still passed. It now strips comment lines and asserts property
reads rather than string presence.

Already in sync, no change needed: the LLM settings panel iterates
`Object.keys(models)` so the new `summary` role appeared automatically (7a); the
403 from the guarded toggle surfaces its `detail` through `apiCall`; and the
search stub-warning banner was already built — it simply **never fired**, because
`embedding_is_stub` was false on every row until issue 3. Expect it to start
appearing in stub-mode deployments; that is the disclosure working, not a
regression.

## The pattern worth naming

Four of the six main issues are one thing:

> **A fix applied at the layer where the defect was noticed, and not at the layer
> where the value is consumed.**

§9.11 fixed `embedding_is_stub` in the assembler's log frame and left the database
column users read (issue 3). §5.9 marked the row and left the reuse path that
copies rows (issue 5). §11.1 carried Stage 2's `insight` to the API and §12.4d had
to carry it the last hop to the dashboard — the report layer's cluster summaries
never got that second pass (issue 4). §5.6 hardened how a tenant is _identified_
and never checked whether anything downstream _uses_ the identity (issue 1).

The mitigation, and the thing to apply to every fix on this list: **when a fix adds
a signal, follow the signal to the surface a human reads, and assert it there.**
§12.3 already did this once for a document. The same test shape — start at the
consumer, walk back to the producer — would have caught all four.

---

---

# Open Issues — Pass 7 audit

**Found:** 7 August 2026, full-codebase audit across five parallel reviewers
(API, workers/pipeline, src/defense/libs/agents, dashboard/config, test suite) with every
finding verified against the source.

**Status: ALL TEN ARE FIXED**, 7 August 2026.

### Changes applied

| #   | Fix summary                                                                              | Files changed                                               |
| --- | ---------------------------------------------------------------------------------------- | ----------------------------------------------------------- |
| 8   | Tenant-scoping added to `delete_post`, `get_analysis`, `get_report`                      | `ingest.py`, `analysis.py`, `reports.py`                    |
| 9   | PEL drain phase added to `bus.py`; empty ingestion messages ACK'd                        | `bus.py`, `service.py`                                      |
| 10  | Silent ACK-and-drop replaced with `record_failure` → DLQ; DLQ replay `xdel` on null data | `assembler.py`, `worker.py` (stage1), `router.py`, `dlq.py` |
| 11  | Permanent `_API_KEY_TABLE_USABLE = False` replaced with 30s TTL retry                    | `deps.py`                                                   |
| 12  | `stream_options={"include_usage": True}` for non-local backends                          | `client.py`                                                 |
| 13  | JSON-retry wrapped in same try/except failover as initial call                           | `client.py`                                                 |
| 14  | `EngagementResult` fields default to `0`                                                 | `models.py`                                                 |
| 15  | `error` field added to `ReportResponse`; `_row_to_report` reads it                       | `models.py`, `reports.py`                                   |
| 16  | `coverage_anomaly()` used instead of `coverage > 1.0` (always false)                     | `harness.py`                                                |
| 17a | Validator sort key stringified to prevent mixed-type crash                               | `validator.py`                                              |
| 17b | `ch_client.disconnect()` added to assembler finally block                                | `assembler/__main__.py`                                     |
| 17c | Router + Stage 2 cleanup moved into `try/finally`                                        | `router.py`, `stage2_llm/worker.py`                         |
| 17d | Dashboard search reads `res.post_text`                                                   | `app.js`                                                    |
| 17e | Demo API key seeded in `init-db.sql`                                                     | `init-db.sql`                                               |
| 17f | `.env` parser strips inline comments                                                     | `run_all.py`                                                |
| 17g | Rate limiter reads `X-Forwarded-For` / `X-Real-IP`                                       | `deps.py`                                                   |
| 17h | Admin gate applies to all backend changes, not just groq                                 | `config.py`                                                 |

---

## At a glance

| #                                                                               | Issue                                                 | Impact                                                | Layer                           | Status   |
| ------------------------------------------------------------------------------- | ----------------------------------------------------- | ----------------------------------------------------- | ------------------------------- | -------- |
| [8](#8--idor--endpoints-lack-tenant-scoping)                                    | IDOR — endpoints lack tenant scoping                  | **Security** — cross-tenant data access/deletion      | API                             | ✅ Fixed |
| [9](#9--pel-messages-are-never-reclaimed-after-worker-crash)                    | PEL messages never reclaimed after worker crash       | **Data loss** — messages lost on any restart          | Bus / all workers               | ✅ Fixed |
| [10](#10--failedcorrupt-messages-are-silently-dropped-instead-of-dead-lettered) | Failed/corrupt messages silently dropped              | **Data loss** — bypasses the DLQ by design            | Assembler, Stage 1, Router, DLQ | ✅ Fixed |
| [11](#11--api-key-auth-permanently-disabled-on-first-db-failure)                | API key auth permanently disabled on first DB failure | **Auth** — one blip locks out every key user          | API deps                        | ✅ Fixed |
| [12](#12--streaming-usage-tracking-reports-zero-tokens)                         | Streaming usage reports zero tokens                   | **Thesis** — cost figure under-counts streaming       | LLM client                      | ✅ Fixed |
| [13](#13--json-retry-bypasses-failover--crashes-instead-of-degrading)           | JSON retry bypasses failover                          | **Resilience** — hard crash instead of degrade        | LLM client                      | ✅ Fixed |
| [14](#14--engagementresult-required-fields-crash-the-api-on-missing-data)       | EngagementResult crashes the API                      | **Availability** — 500 on any post missing engagement | API models                      | ✅ Fixed |
| [15](#15--reportresponse-omits-error--failed-reports-give-no-reason)            | ReportResponse omits `error`                          | **UX** — failed reports give no reason                | API models                      | ✅ Fixed |
| [16](#16--eval-harness-over-coverage-metric-is-dead-code)                       | Eval over-coverage metric is dead code                | **Eval honesty** — always reports 0                   | Eval harness                    | ✅ Fixed |
| [17](#17--smaller-issues)                                                       | Eight smaller issues                                  | Bounded                                               | Various                         | ✅ Fixed |

---

## 8 — IDOR — endpoints lack tenant scoping

### Where

- [ingest.py:223](src/defense/services/api/routers/ingest.py#L223) — `delete_post`
- [analysis.py:513](src/defense/services/api/routers/analysis.py#L513) — `get_analysis`
- [reports.py:567](src/defense/services/api/routers/reports.py#L567) — `get_report`

### What's wrong

Three endpoints accept an entity ID from the URL path and use it as the sole
lookup/delete key, with **no tenant-scoping filter**:

| Endpoint                  | Operation                                                     | Query shape                              |
| ------------------------- | ------------------------------------------------------------- | ---------------------------------------- |
| `DELETE /posts/{post_id}` | cascade delete across `comments`, `analysis_results`, `posts` | `WHERE post_id = :pid` — no tenant check |
| `GET /v1/analysis/{id}`   | read job + results                                            | `WHERE id = :id` — no tenant check       |
| `GET /v1/reports/{id}`    | read report                                                   | `WHERE id = :id` — no tenant check       |

`delete_post` is the worst: any authenticated user can delete another tenant's
posts, their analysis results, and all their comments in a single call. The
other two are read-only IDOR — still a confidentiality breach, but not
destructive.

### Reproduce

```bash
# As tenant A, create a post and note its post_id.
# As tenant B, call DELETE /posts/{post_id} — it succeeds.
```

### Fix

Add `AND campaign_id IN (SELECT id FROM campaigns WHERE tenant_id = :tid)` (or
the equivalent join) to every query that takes a user-supplied entity ID. For
`get_analysis` and `get_report`, filter on the job's `tenant_id` or the
`campaign_id` that owns the data. Apply the same pattern to any other endpoint
that fetches by ID — audit `routers/*.py` for `{id}` or `{post_id}` path
parameters.

### Test to leave behind

For each endpoint: call it as tenant B with tenant A's entity ID, assert 404
(not 200 or 204). These are the three tests that a `test_tenant_isolation.py`
file should hold.

---

## 9 — PEL messages are never reclaimed after worker crash

### Where

- [bus.py:56](src/defense/libs/bus.py#L56) — `RedisStreamBus.consume`
- Every worker's main loop: [stage1_nlp/worker.py:536](src/defense/services/workers/stage1_nlp/worker.py#L536),
  [stage2_llm/worker.py:1277](src/defense/services/workers/stage2_llm/worker.py#L1277),
  [router/router.py:213](src/defense/services/workers/router/router.py#L213),
  [assembler/assembler.py:505](src/defense/services/workers/assembler/assembler.py#L505),
  [ingestion/service.py:792](src/defense/services/ingestion/service.py#L792)
- [ingestion/service.py:585](src/defense/services/ingestion/service.py#L585) — early return without ACK

### What's wrong

`RedisStreamBus.consume` calls `XREADGROUP` with ID `">"` exclusively — it only
reads **new** messages. If a worker crashes or restarts between reading a message
and ACKing it, that message is stranded in the Redis Pending Entries List (PEL)
forever. No worker ever reads from ID `"0"` to drain its PEL on startup, and
there is no `XAUTOCLAIM` / `XPENDING` background loop anywhere in the codebase.

A second variant of the same bug lives in `ingestion/service.py:585`: when
`raw_json` is missing or empty, `_process_message` logs a warning and returns
early. The outer loop does not ACK the message, and no exception is raised to
trigger `record_failure`. The message sits in the PEL indefinitely.

### Impact

**Any worker restart — including a normal rolling deployment — silently drops
every in-flight message.** In a Kubernetes environment with KEDA-driven scaling,
scale-to-zero followed by scale-up guarantees message loss on every idle cycle.

### Fix

1. On startup, have each consumer read from ID `"0"` to drain its PEL before
   switching to `">"`. This is the standard Redis Streams pattern.
2. Add an `XAUTOCLAIM` background task (or a simpler `XPENDING` + `XCLAIM`
   sweep) that reclaims messages idle for longer than a configurable threshold
   (e.g., 5 minutes).
3. Fix the `_process_message` early return: either ACK the empty message
   explicitly or raise an exception to route it to the DLQ.

### Test to leave behind

Simulate crash-before-ACK: consume a message, do **not** ACK, restart the
consumer, and assert it re-processes the same message. This is the test that
would have caught it on day one.

---

## 10 — Failed/corrupt messages are silently dropped instead of dead-lettered

### Where

- [assembler/assembler.py:299](src/defense/services/workers/assembler/assembler.py#L299) — `ValueError`/`KeyError` → ACK + return
- [stage1_nlp/worker.py:602](src/defense/services/workers/stage1_nlp/worker.py#L602) — `JSONDecodeError` → ACK
- [router/router.py:88](src/defense/services/workers/router/router.py#L88) — `JSONDecodeError` → return (outer loop ACKs)
- [dlq.py:170](src/defense/libs/dlq.py#L170) — `replay_dlq` loops forever on `data is None`

### What's wrong

**Three workers ACK and discard messages that fail processing, instead of routing
them to the DLQ.** The DLQ infrastructure (`src/defense/libs/dlq.py`, `record_failure`) exists
and works — these paths just don't use it:

| Worker    | Failure                                                 | What happens                    | Should happen          |
| --------- | ------------------------------------------------------- | ------------------------------- | ---------------------- |
| Assembler | `build_canonical_result` raises `ValueError`/`KeyError` | logs, ACKs, returns             | `record_failure` → DLQ |
| Stage 1   | `json.loads` raises `JSONDecodeError`                   | logs, ACKs                      | `record_failure` → DLQ |
| Router    | `json.loads` raises `JSONDecodeError`                   | logs, returns (outer loop ACKs) | `record_failure` → DLQ |

**The DLQ itself has a bug.** `replay_dlq` iterates entries and calls `continue`
when `data is None` — but the `xdel` that removes the entry from the DLQ is
**after** the `continue`. So a malformed DLQ entry stays in the queue forever,
and every replay pass re-encounters it, creating an infinite loop that blocks
all subsequent replays.

### Fix

1. Replace the explicit ACK + return in the assembler with a call to
   `record_failure(redis_client, stream, msg_id, fields, error=str(exc))`.
2. Same for Stage 1 and Router JSON failures.
3. In `replay_dlq`, when `data is None`, `xdel` the entry **before**
   `continue` (or move it to a terminal error stream).

### Test to leave behind

Feed a malformed message into each stream and assert it lands in the DLQ with
the correct `orig_stream` and error metadata. For the DLQ bug: replay a DLQ
containing one `data=None` entry and assert it is removed, not retried.

---

## 11 — API key auth permanently disabled on first DB failure

### Where

[deps.py:251](src/defense/services/api/deps.py#L251) — `_principal_from_api_key`

### What's wrong

When the `api_keys` table lookup fails (e.g., a momentary Postgres blip), the
handler sets a module-level global:

```python
_API_KEY_TABLE_USABLE = False
```

This flag is **never reset**. Every subsequent request that presents an API key
skips the database lookup entirely for the rest of the process lifetime. The
only recovery is restarting the API server.

### Impact

A single transient database error permanently locks out every API-key-
authenticated user until an operator notices and restarts the service. JWT-
authenticated users are unaffected, so the failure is partial and easy to miss.

### Fix

Remove the global caching of the failure state entirely, or replace it with a
short-TTL cache (e.g., retry after 30 seconds). A circuit-breaker pattern would
be ideal — the LLM client already uses one (`src/defense/libs/llm/client.py`), so the
pattern is in-tree.

### Test to leave behind

Simulate a DB failure on the first API-key lookup, then simulate recovery, and
assert the second lookup succeeds. The test should fail if the global flag
persists.

---

## 12 — Streaming usage tracking reports zero tokens

### Where

[client.py:581](src/defense/libs/llm/client.py#L581) — `chat_stream`'s `_open` helper

### What's wrong

`chat_stream` does not pass `stream_options={"include_usage": True}` when
creating the streaming completion. The code comment says this is intentional
("some Ollama builds reject unknown params"), but the effect is that **every
streamed response reports zero token usage**. Since `chat_stream` feeds through
the same `src/defense/libs/llm/usage.py` tracking that issue 2 fixed, the counters are
incremented — by zero.

Chat and agent interactions are the primary streaming callers, so interactive
token spend is systematically under-reported.

### Impact

`/v1/usage` under-counts tokens and cost for all streaming callers. The cost
figure the thesis relies on is too low by however much traffic goes through
`chat_stream`.

### Fix

Pass `stream_options={"include_usage": True}` and gate it on the backend: apply
it for `groq` and `openai`-compatible backends, skip it for `local` / Ollama
where it is known to cause errors. The backend is already resolved by this
point.

### Test to leave behind

Drive `chat_stream` through a mock OpenAI server that returns usage in the final
chunk, and assert the tracked token count is non-zero.

---

## 13 — JSON retry bypasses failover — crashes instead of degrading

### Where

[client.py:426](src/defense/libs/llm/client.py#L426) — JSON-mode degeneracy retry

### What's wrong

When `_is_degenerate_json` detects a bad JSON response, the retry calls
`_call_api` **without** a `try/except` block. If this second call fails (e.g.,
Groq rate limit, network error), the exception propagates uncaught, bypassing
the Groq→local failover that protects the initial call.

The initial call is wrapped in the failover logic added by issue 6's fix. The
retry is not — it is the same shape of bug: a second call site that reaches the
API without the resilience wrapper.

### Impact

A JSON-mode request that gets a degenerate response and then hits a transient
error on retry will hard-crash the request, even though the local backend is
available and healthy. Every other LLM call path degrades; this one does not.

### Fix

Wrap the retry in the same `try/except` → failover block as the initial call, or
factor the failover logic into `_call_api` itself so every call gets it
automatically.

### Test to leave behind

Mock the primary backend to return degenerate JSON on the first call and raise
on the second. Assert the client falls back to local rather than crashing.

---

## 14 — EngagementResult required fields crash the API on missing data

### Where

- [models.py:195](src/defense/services/api/models.py#L195) — `EngagementResult`
- [analysis.py:816](src/defense/services/api/routers/analysis.py#L816) — populates from `r.get("engagement", {})`

### What's wrong

`EngagementResult` declares four strictly required `int` fields (`comment_count`,
`stored_comments`, `total_reactions`, `share_count`) with no defaults. The
analysis endpoint populates it from `r.get("engagement", {})` — an empty dict
when the JSONB result has no engagement data. Pydantic validation fails, and the
endpoint returns a 500.

### Impact

Any post whose result JSON lacks engagement data (e.g., an old schema version,
a partial analysis, or a platform that doesn't provide reactions) crashes the
analysis-results endpoint for **every post in that batch**, not just the one
missing data.

### Fix

Either make the fields `Optional[int] = 0` (safe — engagement counts default to
zero), or provide a complete fallback dict in the router instead of `{}`.

### Test to leave behind

Build an `AnalysisResultResponse` with `engagement={}` and assert it
serializes without raising, returning zeros.

---

## 15 — ReportResponse omits `error` — failed reports give no reason

### Where

- [reports.py:37](src/defense/services/api/routers/reports.py#L37) — `_get_report_row` selects the `error` column
- [models.py:427](src/defense/services/api/models.py#L427) — `ReportResponse` has no `error` field

### What's wrong

`_get_report_row` explicitly SELECTs the `error` column from the `jobs` table.
But `ReportResponse` does not declare an `error` field, and `_row_to_report`
never reads it. FastAPI's `response_model` strips undeclared keys.

When a report fails (e.g., clustering over zero posts, LLM timeout), the user
sees `status: "failed"` with no explanation. The error string is in the database
— it is selected, and then discarded.

### Fix

Add `error: Optional[str] = None` to `ReportResponse`, populate it in
`_row_to_report`, and render it in the dashboard's Reports tab when
`status === "failed"`.

### Test to leave behind

Build a report row with `status="failed"` and `error="some message"`, pass it
through `_row_to_report` and the `ReportResponse` model, and assert `error`
survives.

---

## 16 — Eval harness over-coverage metric is dead code

### Where

- [harness.py:169](eval/harness.py#L169) — `if coverage > 1.0: over_one += 1`
- [utils.py:182](src/defense/libs/common/utils.py#L182) — `return min(1.0, max(0.0, analyzed / total_comment_count))`

### What's wrong

`run_coverage_check` counts posts with `coverage > 1.0` to detect
over-counting. But `compute_coverage` **clamps** its return value to
`min(1.0, ...)`. The condition can never be true. `over_one` is always 0.

Over-coverage (more comments marked "analyzed" than actually exist) is a real
signal — it indicates a counting bug in the comment pipeline. This metric was
presumably added to catch it, but the clamp at the source makes it invisible.

### Fix

Read the raw `analyzed / total_comment_count` ratio before clamping, or add a
separate `coverage_anomaly(analyzed, total)` function that returns `True` when
`analyzed > total`. The eval harness should call the anomaly check, not the
clamped coverage.

### Test to leave behind

Call `compute_coverage(15, 10)` and assert it returns `1.0` (existing behavior),
then call the new anomaly check with the same inputs and assert it flags it.

---

## 17 — Smaller issues

### 17a — Schema validator crashes on mixed-type path sorting

[validator.py:48](src/defense/contracts/schemas/validator.py#L48) `_collect_errors` sorts
validation errors by `key=lambda e: list(e.absolute_path)`. JSON Schema paths
contain both strings (object keys) and integers (array indices). Python 3 raises
`TypeError: '<' not supported between instances of 'int' and 'str'` when
comparing them, so the validator crashes on any schema with nested arrays.

**Fix:** `key=lambda e: [str(p) for p in e.absolute_path]`.

### 17b — ClickHouse client never closed on shutdown

[assembler/**main**e client never closed on shutdown

[assembler/**main**.py:159](src/defense/services/workers/assembler/__main__.py#L159) The
`finally` block closes Redis and disposes the SQLAlchemy engine, but never
calls `ch_client.disconnect()`. ClickHouse connections leak on every graceful
shutdown or restart.

**Fix:** Add `ch_client.disconnect()` to the `finally` block.

### 17c — Resource cleanup not in `try/finally`

[router/router.py:277](src/defense/services/workers/router/router.py#L277) and
[stage2_llm/worker.py:1341](src/defense/services/workers/stage2_llm/worker.py#L1341) have
`await redis.aclose()` at the bottom of the function, **outside** any
`try/finally`. An unhandled exception in the consumer loop bypasses cleanup.

**Fix:** Wrap the consumer loop in `try/finally` and move cleanup into `finally`.

### 17d — Dashboard search reads wrong field name

[dashboard/app.js:2660](dashboard/app.js#L2660) Search result rendering tries
`res.caption || res.text` — neither exists on `AnalysisResultResponse`. The
correct field is `res.post_text`. Posts without an LLM summary render a blank
snippet.

**Fix:** Add `res.post_text` to the fallback chain before `res.caption`.

### 17e — Demo login advertised but no seed in `init-db.sql`

[run_all.py:562](run_all.py#L562) tells users to "log in with API key: demo".
[init-db.sql:83](deploy/init-db.sql#L83) creates the `api_keys` table but
**inserts no rows**. First-time users cannot log in.

**Fix:** Add an `INSERT INTO api_keys` with the SHA-256 hash of `"demo"`, or
update `run_all.py`'s instructions to explain how to create a key.

### 17f — `run_all.py` `.env` parser doesn't strip inline comments

[run_all.py:360](run_all.py#L360) `_dotenv_value` splits on `=` and strips
quotes, but does not strip inline comments. `GROQ_API_KEY=sk-1234 # my key`
parses as the literal string `sk-1234 # my key`, silently breaking LLM auth.

**Fix:** `value.split("#", 1)[0].strip()` before stripping quotes.

### 17g — Rate limiter ignores reverse proxy headers

[deps.py:400](src/defense/services/api/deps.py#L400) The anonymous rate limiter uses
`request.client.host`. Behind a reverse proxy or load balancer, all anonymous
traffic appears to come from the proxy's IP, applying a single shared rate
limit to every user.

**Fix:** Read `X-Forwarded-For` or `X-Real-IP` (with a configurable trusted-
proxy allowlist to prevent spoofing).

### 17h — `PUT /v1/config/llm` allows non-admins to set backend to `local`

[config.py:92](src/defense/services/api/routers/config.py#L92) The admin-role gate only
fires when `backend == "groq"`. Any authenticated user can set the global LLM
backend to `"local"` or clear the override entirely, affecting all tenants.

**Fix:** Restrict all modifications of the global LLM backend to admin roles.

---

## The pattern this pass found

Two recurring shapes:

> **1. The happy path is tested; the failure/edge path is ACKed and
> discarded.** Issues 9, 10, and 11 are all cases where the error handler
> "succeeds" — it ACKs the message, sets a flag, returns early — and the
> data is gone. The DLQ exists precisely for these cases and is simply not
> called.

> **2. Tenant boundaries are assumed, not enforced.** Issue 8 is the
> classic IDOR shape: the endpoint trusts the caller's entity ID and never
> checks ownership. The pipeline is documented as "single-tenant" in
> several places, but the auth system issues per-tenant tokens, the
> database stores per-tenant campaigns, and the API happily crosses the
> boundary.

The mitigation for shape 1: every `except` block that ACKs a stream message
should either re-raise (letting the outer DLQ handler catch it) or explicitly
call `record_failure`. A lint rule or AST test — "no `xack` inside an `except`
block without `record_failure`" — would catch future instances.

The mitigation for shape 2: a `test_tenant_isolation.py` that, for every
entity-by-ID endpoint, calls it as a different tenant and asserts 404.

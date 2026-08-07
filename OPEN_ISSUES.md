# Open Issues — Pass 6 remediation list

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

| # | Issue | Impact | Status | Blocked the defense? |
| - | ----- | ------ | ------ | ------------------- |
| [1](#1--the-privacy-lock-does-not-cover-the-analysis-pipeline) | Privacy lock doesn't cover the analysis pipeline | **Credibility** — the strongest claim in the deck | ✅ **Fixed** — enforced via the envelope | — |
| [2](#2--v1usage-counts-stage-2-only) | `/v1/usage` counts Stage 2 only, says otherwise | **Thesis** — undercounts the cost number | ✅ **Fixed** — tracking moved into `LLMClient` | — |
| [3](#3--embedding_is_stub-is-false-on-every-row-the-pipeline-writes) | `embedding_is_stub` is `FALSE` on every row | Honesty flag reports the inverse | ✅ **Fixed** — flag carried, not inferred | — |
| [4](#4--report-cluster-summaries-are-computed-paid-for-and-discarded) | Report cluster summaries computed then discarded | Real LLM spend, zero output | ✅ **Fixed** — surfaced through API + dashboard | — |
| [5](#5--near-duplicate-reuse-copies-the-analysis-but-not-its-provenance) | Near-dup reuse copies the wrong things | Wrong data in a store, on by default | ✅ **Fixed** — composed via the assembler | — |
| [6](#6--the-agent-runner-bypasses-every-llmclient-resilience-path) | Agent runner bypasses breaker/failover/usage | Agents hard-fail where everything else degrades | ✅ **Fixed** — `tools` passthrough on `chat()` | — |
| [7a–d](#7--smaller-issues) | Four smaller ones | Bounded | ✅ **All fixed** | — |

Fixed in the order below; see [What changed](#what-changed) for the per-issue landing notes.

---

## 1 — The privacy lock does not cover the analysis pipeline

> ✅ **FIXED** — enforced, not merely restated. See [what landed](#what-landed-1--privacy-lock-enforced).
>
> §13.5 · **[read]** · the local⇄Groq policy is what §3.3 calls *"the best design
> decision in the project"*, so this is the one item here that is a credibility
> risk rather than a correctness one.

### Where

- [deps.py:413](services/api/deps.py#L413) — `check_llm_backend_policy`
- [config.py:87](services/api/routers/config.py#L87) — `PUT /v1/config/llm`
- [stage2_llm/worker.py:991](services/workers/stage2_llm/worker.py#L991) — reads `config:llm_backend`
- [stage1_nlp/worker.py:89](services/workers/stage1_nlp/worker.py#L89) — same key

### What's wrong

Two halves that do not meet.

**The guarded knob is dead.** `check_llm_backend_policy` returns immediately
unless `options["llm_backend"] == "groq"`. `POST /v1/analysis` and `POST /v1/ingest`
both call it with the request options — but grepping `services/workers/` for
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
grep -rn "llm_backend" services/workers/ | grep -v '_llm_backend"'
# → only config:llm_backend reads and processing-provenance writes.
#   No worker reads options["llm_backend"].
```

### Fix — pick one, and say which

**(a) Restate it honestly (small, do this before the defense regardless).**
The pipeline is single-tenant; the backend policy is enforced on the interactive
surfaces (`/v1/chat`, agents) and not on batch analysis. This is the same move
§12.5 already made for `analysis_run`'s missing tenant scoping. Already applied to
[FEATURES.md](FEATURES.md) §6, [architecture.md](architecture.md) §9 and
[endpoints.md](endpoints.md) by this pass — but the *code* should say it too, in a
comment on `check_llm_backend_policy` naming what it does and does not cover.

**(b) Enforce it (larger).**
1. Guard the toggle: call `check_llm_backend_policy` in `put_llm_config`, and gate
   it on an admin role. A global switch should not be settable by a tenant user.
2. Carry the tenant through the pipeline: add `tenant_id` to the ingestion/analysis
   envelope, and have the Stage-2 worker resolve `enforce_policy(tenant_policy,
   config:llm_backend)` per message via [libs/llm/policy.py](libs/llm/policy.py)
   — which already exists and is already used by the agent runner.
3. Then either delete `options["llm_backend"]` or make Stage 1/2 honour it.

Do **not** ship (b) half-done: a per-request option that is checked but not read
is exactly the shape of the bug.

### Test to leave behind

Assert the *contract*, per §11.6: for every knob `check_llm_backend_policy`
inspects, some worker must read it. A test that greps `services/workers/` for the
option key and fails when nothing consumes it would have caught this on the day it
was introduced.

---

## 2 — `/v1/usage` counts Stage 2 only

> ✅ **FIXED** — see [what landed](#what-landed-2--usage-counters-moved-down-a-layer).
>
> §13.4 · **[measured]** · `estimated_cost_usd` is the number the cost-efficiency
> thesis rests on.

### Where

- [stage2_llm/worker.py:210](services/workers/stage2_llm/worker.py#L210) — `_track_usage`, the only writer
- [usage.py](services/api/routers/usage.py) — the reader and its `scope_note`

### What's wrong

`_track_usage` writes `usage:tokens:total`, `usage:llm_calls`, `usage:cache_hits`,
`usage:tokens:{backend}:{model}` and the lane counters. It exists in exactly one
file. Five other LLM call sites never touch a counter:

| Call site | When it runs | Volume |
| --------- | ------------ | ------ |
| [chat.py:202](services/api/routers/chat.py#L202), [:260](services/api/routers/chat.py#L260) | every chatbot turn | unbounded, user-driven |
| [reports.py:216](services/api/routers/reports.py#L216) `_llm_narrative` | every grounded report (the default) | 1 per report |
| [reports.py:354](services/api/routers/reports.py#L354) `_summarize_cluster` | every grounded report | up to 8 per report |
| [llm_analyzer.py:225](services/workers/stage1_nlp/llm_analyzer.py#L225), [:292](services/workers/stage1_nlp/llm_analyzer.py#L292) | when `STAGE1_LLM=true` — **the shipped config** | per post |
| [runner.py:409](services/agents/runner.py#L409) `_llm_chat_with_tools` | every agent turn | per tool-calling turn |

Meanwhile `UsageResponse.total_tokens` says *"Total tokens spent"*, the endpoint
docstring says *"Every token/cost/cache figure therefore comes from Redis"*, and
`scope_note` — the field §5.8 added **specifically** to state what the numbers
cover — returns the flat string `"All figures are system-wide."`

It matters most in the shipped configuration: with `STAGE1_LLM=true`, Stage-1
comment labelling is uncounted, and §6.8 measured comment-level calls at **96%** of
the total there.

### Reproduce

```bash
grep -rn "_track_usage" --include=*.py services/ libs/ | grep -v tests/
# → services/workers/stage2_llm/worker.py only.
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

- [persistence.py:55](services/workers/assembler/persistence.py#L55) — `_resolve_embedding`, writes the DB column
- [assembler.py:295](services/workers/assembler/assembler.py#L295) — the trace frame, computes the *same flag differently*

### What's wrong

Two independent computations of one flag, and they disagree:

| Site | Rule | Value in stub mode |
| ---- | ---- | ------------------ |
| `assembler.py:295` (trace frame / log) | `processing.stub_mode or not stage1_embedding` | **True** ✓ |
| `_resolve_embedding` (the DB column) | dimension check: `len(embedding) == EMBEDDING_DIM → not a stub` | **False** ✗ |

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

- [reports.py:240](services/api/routers/reports.py#L240) — `_embedding_clusters` (the producer)
- [reports.py:472](services/api/routers/reports.py#L472) — writes `content["embedding_clusters"]`
- [models.py:367](services/api/models.py#L367) — `ReportResponse` (no such field)
- [reports.py:53](services/api/routers/reports.py#L53) — `_row_to_report` (never reads it)

### What's wrong

`_embedding_clusters` pulls up to `REPORT_CLUSTER_SAMPLE_CAP` (1500) embeddings,
k-means them (`MAX_CLUSTERS = 8`), and spends **one LLM-B call per cluster**. Then:

- `ReportResponse` declares no `embedding_clusters` field, and FastAPI's
  `response_model` strips undeclared keys → `POST /v1/reports` does not return it;
- `_row_to_report` never reads the key → `GET /v1/reports` and
  `GET /v1/reports/{id}` do not return it;
- `embedding_clusters` appears **nowhere** in `dashboard/app.js`.

Its only existence is the raw `jobs.options` JSONB. The `clusters` field the API
*does* return is `_generate_report_content`'s topic-count aggregate — pure SQL,
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
and *labelling* it is honest; paying for it silently is not.

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

[service.py:263](services/ingestion/service.py#L263) — `_reuse_analysis`

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
   Reactions and comment counts are per-post *facts*, not analysis — two posts can
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
   `created_at`, `platform_post_id`, `url` all belong to the *new* post; only the
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

[runner.py:409](services/agents/runner.py#L409) — `_llm_chat_with_tools`

### What's wrong

It reaches past `LLMClient.chat` into `self.llm._get_client(...)` and calls
`oai_client.chat.completions.create` directly, because `chat()` does not forward
tool definitions. The docstring is accurate about what that preserves — *"policy
enforcement and model resolution"* — and silent about what it drops:

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

Assert no module outside `libs/llm/` references `_get_client`, `_resolve_model` or
`_breakers`. A private-helper firewall is the general form of this bug.

---

## 7 — Smaller issues

> ✅ **All four FIXED.**

### 7a — `config.py`'s model table is missing the `summary` role

[config.py:36](services/api/routers/config.py#L36) `_MODEL_ENVS` is a
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

[pipeline.py:34](services/api/routers/pipeline.py#L34) `_STAGES` hardcodes all five
stream **and** group names as string literals instead of importing
[libs/streams.py](libs/streams.py). They match the defaults today, but every name
is env-overridable *by design* ("deployments legitimately shard streams"), and
under any override `_stage_stats` swallows the `xinfo_groups` error and returns
zeros — so the dashboard's Pipeline tab renders an **idle, healthy** pipeline while
work is queued. That is the §5.5 KEDA failure mode reproduced in the monitoring
view.

**Fix:** build `_STAGES` from `streams.ALL`. **Test:**
`test_workers_import_their_names_from_libs_streams` covers the five workers and not
this file — extend it. It is the last un-pinned copy of those identifiers.

### 7c — An ingestion docstring describes the opposite of its code

[service.py:657](services/ingestion/service.py#L657) `_ensure_consumer_group`'s
docstring says it uses `"$"` *"so we only process messages that arrive after the
service starts"*, and offers `"0"` as the change to make for reprocessing. The code
passes `id="0"`. The comment describes the opposite of the behaviour, and its
remediation advice describes the state it is already in.

**Fix:** correct the docstring (the `id="0"` behaviour is the right one — it is
what lets a restarted service pick up a backlog).

### 7d — `lane_split` serializes token counts as floats

[usage.py:109](services/api/routers/usage.py#L109) `UsageResponse.lane_split` is
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

`libs/llm/usage.py` is new and `LLMClient.chat` / `chat_stream` record every call
themselves, so a **new call site is counted without its author knowing a counter
exists** — which is the only version of this that stays true, since §13.4 happened
because five call sites accumulated and nothing made the omission visible. Lanes
gained `stage1`, `interactive` and `agent`; `pipeline_tokens` isolates the
per-post model from per-question interactive spend so §6.8's figure cannot be
inflated by a chatbot session. §12.4b's deliberate asymmetry is preserved and
pinned: `usage:llm_calls` stays fresh-only, `usage:calls:task:{task}` counts both.

`tests/test_usage_covers_every_caller.py` (20 tests): an AST check that nothing
outside `libs/llm/` calls the OpenAI SDK or touches the client's private helpers,
plus behavioural tests that drive the real `chat()` and watch the counters fire.

### What landed (3) — provenance carried, not inferred

`text_analyzer._embed_with_provenance` returns `(vector, is_stub)`, Stage 1 puts
it on its result, and the assembler computes **one** value used for both the
trace frame and the database column — the two independent computations that
disagreed are gone. `_resolve_embedding` takes the producer's answer and keeps
the dimension heuristic only as a fallback. A failed real model now returns a
stub *and says so*, where it used to return an empty vector that silently became
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

**The comment thread is not reused.** A near-duplicate is a *caption* match; the
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

* **7a** — `config.py`'s mirrored model table is deleted; `_models()` derives
  from `LLMClient.default_model` over `VALID_ROLES`, so `summary` appears and any
  future role does too, automatically.
* **7b** — `pipeline.py::_STAGES` is built from `streams.ALL`, and
  `tests/test_streams.py` now covers it — the last un-pinned copy of those names.
* **7c** — the `_ensure_consumer_group` docstring describes `"0"`, which is what
  the code does, and says why.
* **7d** — `lane_split` entries are a `LaneUsage` model: `calls` and `tokens` are
  `int`, shares are `float`.

## UI sync

The fixes changed the API surface, so `dashboard/app.js` was audited against it
rather than assumed correct. Three gaps, all §13.8's rule:

| Gap | Fixed by |
| --- | -------- |
| The "Cost split" card read 2 of 5 lanes, and its comment-share denominator silently grew to span chat and agent traffic while still being labelled post-vs-comment | Share computed over pipeline lanes only; a "Spend by lane" breakdown names every lane with a hint table mirroring `libs/llm/usage.py` |
| `pipeline_tokens` unrendered — the token card showed a total that now includes per-question chat/agent spend | Its own stat card, with a subtitle stating what the total covers that it does not |
| `processing.reused_from` rendered nowhere — a reused post was indistinguishable from a cheap analysis, and its `0 analyzed` comment section gave no reason | `near-dup` tag in the post list, a "Reused Analysis" block in the modal, a `reused` row in the Trace tab, and a "thread was not analysed" note from `comment_analysis.provenance.note` |

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
never got that second pass (issue 4). §5.6 hardened how a tenant is *identified*
and never checked whether anything downstream *uses* the identity (issue 1).

The mitigation, and the thing to apply to every fix on this list: **when a fix adds
a signal, follow the signal to the surface a human reads, and assert it there.**
§12.3 already did this once for a document. The same test shape — start at the
consumer, walk back to the producer — would have caught all four.

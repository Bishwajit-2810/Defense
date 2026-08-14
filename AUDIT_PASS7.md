# Open Issues — Pass 7 remediation list

**Found:** 12–13 August 2026, fresh-eyes audit of the whole tree (the fourth such pass),
triggered by the two commits that landed after Pass 6: `49efec3` _"update llm stratagy"_
and `274068b` _"added warning support with batch pdf download"_.

**Status: ALL THIRTEEN FIXED** and regression-tested, 13-14 August 2026. The last two
were found only by running the real models (Ollama `qwen2.5:7b` + both real HF checkpoints)
against real Bangla comments — see [Verified against the real models](#verified-against-the-real-models). This file is the actionable
version — what was wrong, how it was proved, what test now guards it. Pass 6's list is
[OPEN_ISSUES.md](OPEN_ISSUES.md).

**Test suite: 66 failing / 3 uncollectable / 3 hanging → 817 passing in 22 s, 44 → 47 files.**
> **Correction (Pass 8).** That 22 s excluded `test_pipeline_e2e.py`, which was still running
> on a bare `pytest` and spending 129.8 s failing — the real figure for the command as
> written was 826 passing / 1 failing in 153 s. Now guarded and skipped by default:
> [AUDIT_PASS8 §2](AUDIT_PASS8.md#2--a-plain-pytest-wipes-the-developers-datastores).
The suite was the finding that mattered most: the contract guards Pass 6 left behind —
`test_usage_covers_every_caller`, `test_privacy_lock_covers_pipeline`,
`test_integration::TestRouterReadsRealStage1Shape`, `test_analytics_storage_contract` —
were _all_ silently dead, which is why everything below reached `master` unnoticed. Issue 0
is therefore listed first: it is the one that let the rest happen.

Two of the thirteen are **regressions of previously-fixed issues**: #5 re-broke the routing
gate ([OPEN_ISSUES §router](OPEN_ISSUES.md)), and #11 re-broke OPEN_ISSUES §4 (report
cluster summaries computed, paid for and discarded) by losing the fix in the React rewrite.
A third, #9, reintroduces the "absent measurement renders as a neutral verdict" pattern the
README explicitly retracts for image sentiment.

---

## At a glance

| #                                                                     | Issue                                          | Impact                                                      | Status       |
| --------------------------------------------------------------------- | ---------------------------------------------- | ----------------------------------------------------------- | ------------ |
| [0](#0--the-test-suite-was-not-running)                               | The test suite was not running                 | **Everything below** — the guards were dead                 | ✅ Fixed     |
| [1](#1--every-degradation-path-raises-typeerror)                      | Every degradation path raises `TypeError`      | **Correctness** — handlers kill the post they exist to save | ✅ Fixed     |
| [2](#2--the-comment-ensemble-is-computed-paid-for-and-contradicted)   | Comment ensemble computed, then contradicted   | **Thesis** — two disagreeing aggregates in one payload      | ✅ Fixed     |
| [3](#3--three-dead-reads-in-the-watchlist-feature)                    | Three dead reads in the watchlist              | **Feature** — the newest feature cannot fire                | ✅ Fixed     |
| [4](#4--the-shipped-watchlist-names-a-real-person-and-two-adjectives) | Watchlist ships a real person + two adjectives | **Credibility / false alerts**                              | ✅ Fixed     |
| [5](#5--the-routing-gate-stopped-gating-again)                        | The routing gate stopped gating (again)        | **Thesis** — `estimated_llm_share` = 1.0                    | ✅ Fixed     |
| [6](#6--every-post-pays-for-two-summaries)                            | Every post pays for two summaries              | Real LLM spend, discarded                                   | ✅ Fixed     |
| [7](#7--the-router-rewrites-comment-text-after-stage-1-scored-it)     | Router rewrites comment text post-hoc          | Data fidelity + dead counters                               | ✅ Fixed     |
| [8](#8--the-cheap-classifiers-fabricate-verdicts)                     | Cheap classifiers fabricate verdicts           | **Honesty** — a failed model reads as neutral               | ✅ Fixed     |
| [9](#9--v1analysisexport-is-unbounded-unescaped-and-dishonest)        | `/v1/analysis/export` unbounded + unescaped    | Availability + injection + honesty                          | ✅ Fixed     |
| [10](#10--smaller-ones)                                               | Seven smaller ones                             | Bounded                                                     | ✅ All fixed |
| [11](#11--the-react-rewrite-lost-two-shipped-fixes)                   | The React rewrite lost two shipped fixes       | Regression of OPEN_ISSUES §4                                | ✅ Fixed     |
| [12](#12--the-second-classifier-silently-lost-to-the-llm-for-gpu-memory) | 2nd classifier lost the GPU race to the LLM | **Only found by running the real models** — a voter vanished | ✅ Fixed     |
| [13](#13--the-hash-stub-was-voting-in-the-ensemble)                   | The hash stub was voting in the ensemble       | **Honesty** — 11 of 12 votes were hash noise                | ✅ Fixed     |

---

## 0 — The test suite was not running

### Where

[tests/conftest.py](tests/conftest.py) · [pyproject.toml](pyproject.toml) · ~20 test files

### What's wrong

Four independent causes, all invisible because pytest reports a missing module as one
error and moves on:

1. **Stale import roots.** The tree moved under `src/defense/`; tests still did
   `import mcp_servers.analytics_mcp.server` and read `_REPO / 'services' / ...`. The
   grep-the-source contract tests scanned directories that no longer exist, found nothing,
   and _passed_ — `test_every_clickhouse_table_created_has_a_writer` reported "no writer
   exists" for every table in the schema because its file list was empty.
2. **Cached settings.** `get_settings()` is `@lru_cache`d, so the first import froze the
   environment for the whole session and every `monkeypatch.setenv` asserted against the
   snapshot. This is what "broke" `test_sentiment_routing`, `test_llm_cache_key`,
   `test_tracing` and `test_auth_jwt`.
3. **Two module identities for one file.** Both `src/defense` and
   `src/defense/services/api` are importable, so `libs.common.config` and
   `defense.libs.common.config` are _different module objects_ — each with its own
   randomly-generated `JWT_DEV_DEFAULT_SECRET`. Tests minted tokens through one and
   verified through the other (signature failures), and patched
   `"libs.llm.client.LLMClient.chat"` while the router called
   `defense.libs.llm.client` — so `test_chat_endpoint` was making **real LLM calls** with
   retry backoff and hanging.
4. **Tests reaching the network.** `STAGE1_LLM` defaults to `true`, so a bare
   `ModelRegistry()` in a unit test tried to reach Ollama and spent the full
   retry-with-backoff budget failing: 65 seconds for one assertion about a stub embedding.
   `test_degradation_honesty` spent 197 s downloading the very models whose _absence_ it
   asserts.

### Reproduce

```bash
git stash && .venv/bin/python -m pytest -q            # hangs; 66 failures when it doesn't
```

### Fix

`pythonpath = [".", "src", "src/defense"]`; a `pytest_sessionstart` that forces
`HF_HUB_OFFLINE` / `TRANSFORMERS_OFFLINE` / `STAGE1_LLM=false`; autouse fixtures that clear
the settings cache per test and keep the developer's `.env` out of the run; and every
cross-identity import/patch repointed at the module the code under test actually uses.

### Test to leave behind

The suite itself — 817 passing in 22 s. `_python_sources()` in
[test_analytics_storage_contract.py](tests/test_analytics_storage_contract.py) now asserts
its own file list is non-empty: **a scan that finds nothing must never read as a finding.**

---

## 1 — Every degradation path raises `TypeError`

> The single highest-impact defect in this pass.

### Where

Seven modules, ~29 call sites: [libs/llm/usage.py](src/defense/libs/llm/usage.py) ·
[libs/tracing.py](src/defense/libs/tracing.py) ·
[libs/embeddings.py](src/defense/libs/embeddings.py) ·
[assembler/persistence.py](src/defense/services/workers/assembler/persistence.py) ·
[stage1_nlp/text_analyzer.py](src/defense/services/workers/stage1_nlp/text_analyzer.py) ·
[stage1_nlp/vision_analyzer.py](src/defense/services/workers/stage1_nlp/vision_analyzer.py) ·
[stage1_nlp/comment_analyzer.py](src/defense/services/workers/stage1_nlp/comment_analyzer.py) ·
[stage1_nlp/models.py](src/defense/services/workers/stage1_nlp/models.py)

### What's wrong

Each built a **stdlib** logger (`logging.getLogger(__name__)`) and called it
**structlog-style** (`logger.warning("batched sentiment failed", hf_name=…, error=…)`).
`logging.Logger._log()` rejects unexpected keyword arguments, so every one of those calls
raises `TypeError` — and **every one sits inside an `except` block**. The handler that
exists to keep a post alive is the thing that kills it:

- `usage.py:165` — the "usage tracking never raises" guarantee, broken. A Redis hiccup
  during an LLM call now fails the whole post.
- `text_analyzer.py:831` — the batched-sentiment fallback dies, taking the post's comment
  analysis with it.
- `vision_analyzer.py:236` — a failed image fetch raises instead of degrading, which is the
  opposite of what the README says was fixed.
- `models.py:92` — a failed model load raises instead of falling back to the stub.

Note the shape: `structlog.stdlib.LoggerFactory` installs `_FixedFindCallerLogger` as the
logger class, so `type(logger)` _prints as a structlog name_ while remaining a stdlib
Logger. Reading the type at a REPL suggests the code is fine.

### Reproduce

```python
from defense.libs.common.logging import setup_logging; setup_logging("probe")
import defense.services.workers.stage1_nlp.text_analyzer as ta
ta.logger.warning("probe", error="x")
# TypeError: Logger._log() got an unexpected keyword argument 'error'
```

### Fix

`logging.getLogger` → `structlog.get_logger` in all seven. `PositionalArgumentsFormatter`
is already in the processor chain, so the two `%s`-style call sites keep working.

### Test to leave behind

[test_audit_regressions.py](tests/test_audit_regressions.py) —
`test_module_logger_accepts_structured_fields` (parametrised over all seven, and it makes
the call the degradation paths make), plus
`test_no_module_pairs_a_stdlib_logger_with_keyword_calls`, which greps the whole package so
the class of bug cannot return in a module nobody thought to list.

---

## 2 — The comment ensemble is computed, paid for, and contradicted

### Where

[stage2_llm/worker.py](src/defense/services/workers/stage2_llm/worker.py) —
`_run_llm_comment_labeling`, `_comment_lane`

### What's wrong

`49efec3` introduced `parallel_labels` (LLM + XLM-R + DistilBERT per comment) and wired
nothing to consume it:

- the function's docstring still claims it _"overwrites each comment's `sentiment`"_; the
  code writes only `parallel_labels["llm"]`, so **the comment's own label never changed**;
- it then recomputed `sentiment_breakdown` from `c["sentiment"]` — the untouched Stage-1
  labels — and `_comment_lane` immediately **overwrote that** with counts built from LLM
  votes only, while `sentiment_breakdown_substantive`, `method_breakdown`, `provenance` and
  `target_stances` still described Stage 1. One payload, two aggregates, no relationship
  between them: a post could report eight negative comments while every comment in its own
  list read `neutral`, and the totals no longer summed to the comment count;
- `method_breakdown` never saw `"llm"`, so `provenance` reported **0% LLM for a run in
  which every comment was sent to one** — understating the cost story in the project's own
  dashboard. This is OPEN_ISSUES §4's pattern ("computed, paid for, discarded") recurring.

### Fix

`_merge_ensemble()` is now the **single writer** of `sentiment`, `sentiment_score`,
`method` and every aggregate, because it is the only place that has seen every voter.
`_run_llm_comment_labeling` contributes one opinion and computes no aggregates.

### Test to leave behind

[test_comment_ensemble_merge.py](tests/test_comment_ensemble_merge.py) — 15 tests, of which
`test_the_breakdown_totals_the_comments_it_describes` and
`test_every_comment_carries_the_label_the_counts_were_built_from` are the two that fail on
the old code.

---

## 3 — Three dead reads in the watchlist feature

### Where

[stage2_llm/worker.py](src/defense/services/workers/stage2_llm/worker.py) `_comment_lane` ·
[assembler/builder.py](src/defense/services/workers/assembler/builder.py)

### What's wrong

The feature shipped in `274068b` could not fire on three separate paths:

1. **Post-level check reads a key Stage 1 never emits.** `stage1_result.get("post_text")` —
   `post_text` is created by the _assembler_ (`builder.py`), not Stage 1. Always `""`, so
   "an `always` target mentioned in the post" never alerted. The caption was available two
   lines away as `partial_result["caption"]`.
2. **LLM entity stances never reach the field the alert reads.** The LLM writes
   `parallel_labels.llm.target_stances`; the alert and the rollup read
   `comment["target_stances"]`. Two fields that never met — so the ~40 tokens/entity/comment
   spent on target stance changed neither the alert nor any aggregate.
3. **`post_context` is dead.** Built with a comment explaining why it decouples the lanes,
   then never used: the stance prompt took `stage1_result["post_summary"]`, which is
   `"(no summary provided)"` whenever `STAGE1_LLM` is off. The _context-aware_ stance pass
   ran without context.

A fourth, structural: `watchlist_alert` was computed **only in Stage 2**, so the Warnings
page silently depended on the routing gate being broken (issue 5). Fixing one would have
silently disabled the other.

### Fix

Caption as the post-level source; `_merge_llm_target_stances()` folds the LLM's verdicts
into `target_stances` tagged `method: "llm"`; `post_context` is what the prompt uses; and
`_watchlist_alert()` in the assembler computes the alert for **every** post, with Stage 2
upgrading it when it ran.

### Test to leave behind

`test_llm_target_stances_are_merged_into_the_comments_own_field`,
`test_an_always_target_in_the_post_text_alerts` and the `_watchlist_verdict` set in
[test_comment_ensemble_merge.py](tests/test_comment_ensemble_merge.py).

---

## 4 — The shipped watchlist names a real person and two adjectives

### Where

[config/stance_targets.yml](config/stance_targets.yml)

### What's wrong

`274068b` added three entries to the `always` bucket — which alerts on **any** mention,
whatever the stance:

- a real, named political figure, committed to the repository;
- `id: positive` (alias `"positive"`) and `id: negative` (aliases `"negative"`,
  `"negetive"`).

So **every comment containing the English word "positive" raised "Watchlist Under Attack"**,
and both words were injected into the LLM's stance prompt as named entities to judge stance
toward, next to real people. The file's own header says it ships EXAMPLES ONLY, and
`test_shipped_config_loads_and_is_marked_as_an_example` asserts it — that test was among
the ones not running.

The alert logic was wrong too: it fired on `stance == "opposing"` toward **any** listed
target, so a _monitoring_ watchlist (`neutral` polarity — "report stance, impose no
framing") behaved like an advocacy one.

### Fix

Tracked file back to examples-only. `load_targets()` now prefers a gitignored sibling
`*.local.yml`, which is where operator entries belong; the real entry was moved there and
the two adjectives commented out with the reasoning rather than silently dropped. Alerts
now require `always` (any mention) or `favored` + `opposing` (the "under attack" case), and
every alert carries `watchlist_alert_reason` naming the target and the rule.

### Test to leave behind

`test_the_tracked_watchlist_declares_no_real_entities`,
`test_an_operator_override_is_loaded_instead_when_present`, and
`test_opposing_an_undeclared_target_does_not_alert`.

---

## 5 — The routing gate stopped gating (again)

### Where

[router/rules.py](src/defense/services/workers/router/rules.py) `should_use_llm` rule 3 ·
[router/router.py](src/defense/services/workers/router/router.py) logging

### What's wrong

`49efec3` changed rule 3 from `options.get("want_summary", False)` to
`options.get("want_summary", True)`. **One rule firing is enough to route**, so every post
went to Stage 2, `estimated_llm_share` became the constant 1.0, and the measured 16% quoted
in the docs was void.

Worse, `router.py` logged `want_summary=options.get("want_summary", False)` — a _different
default from the rule it describes_ — so the log line said the rule had not fired while it
was firing on every post. That is verbatim the failure the module docstring warns about
("it used to log raw key lookups… and made three dead rules look like passing ones").

The product requirement behind the change was real: every post needs a summary.

### Fix

Not a revert. Stage 1 generates the summary itself (`49efec3` also added that), the
assembler falls back to it (`post_summary_source: "stage1"`), and rule 3 defaults to
`ROUTER_SUMMARY_ROUTES=false`. Every post still gets a summary; wanting one is no longer a
reason to pay for the bigger model. The log reads through the same constant the rule does.

**Note for the defense:** in `MODEL_STUB_MODE` the gate still routes everything, and that is
correct — `_stub_post_type` is English seed words and cannot type a Bangla caption, so rule 2
fires honestly. Any "the gate is dead" measurement taken in stub mode is measuring the stub.

### Test to leave behind

`test_summary_alone_no_longer_routes_every_post`, `test_bypass_leg_is_reachable` and
`test_stub_mode_routes_everything_for_a_stated_reason` in
[test_integration.py](tests/test_integration.py) — the last of which asserts the _reason_
set, so a future default flip fails with an explanation.

---

## 6 — Every post pays for two summaries

### Where

[stage1_nlp/worker.py](src/defense/services/workers/stage1_nlp/worker.py) §3 ·
[assembler/builder.py](src/defense/services/workers/assembler/builder.py)

### What's wrong

Stage 1 generates a summary for every post (uncached, a full LLM call). Stage 2 then
regenerated it, because `want_summary` defaulted to `True` (issue 5). The assembler read
**only Stage 2's**. So: one wasted call per post, every post — and if Stage 2 was skipped or
its summary task errored, `post_summary` came out `null` _even though a summary had been
generated one stage earlier_. That null is what pushed the router into sending every post
to Stage 2 to fill the field, which is a circular justification for issue 5.

### Fix

The assembler prefers Stage 2's summary and falls back to Stage 1's, tagging the source.
`get_task_flags` asks Stage 2 for a summary only on explicit request or when Stage 1 came
back empty — with an explicit `want_summary: False` still winning over the fallback.

### Test to leave behind

`test_stage_2_does_not_re_summarise_what_stage_1_already_wrote` and the
`post_summary_source == "stage1"` assertion in
`test_bypassed_post_yields_schema_valid_output`.

---

## 7 — The router rewrites comment text after Stage 1 scored it

### Where

[router/router.py](src/defense/services/workers/router/router.py) `_process_message`

### What's wrong

`49efec3`/`274068b` had the router strip emoji, replace URLs with the literal string
`link`, and set `kind = "filtered"` — **after** Stage 1 had classified those comments from
the very characters being deleted. Consequences:

- an emoji reaction keeps a sentiment derived from text that no longer exists, and renders
  as an empty row carrying a label;
- `kind == "emoji"` is erased, so `reaction_only` is **pinned to 0 forever** and emoji
  reactions are counted as written opinion in `sentiment_breakdown_substantive`;
- persisted comment text no longer matches the source — a provenance defect in Postgres and
  ClickHouse alike;
- `FILTER_EMOJI_ONLY` ([config.py](src/defense/libs/common/config.py)) became a dead knob;
- `import emoji` inside the per-comment loop, and business logic in the routing layer.

### Fix

The router touches nothing. Normalisation moved into Stage 1 next to the classifier that
reads it, as an **additional** field (`text_norm`, consumed by the models and the cache key)
— the original `text` is never overwritten. `FILTER_EMOJI_ONLY` is honoured in the one place
that now decides who gets an LLM call.

### Test to leave behind

`test_normalisation_adds_a_field_instead_of_overwriting_the_text`,
`test_an_emoji_only_comment_normalises_to_nothing_but_keeps_its_text`, and
`test_emoji_reactions_stay_reactions`.

---

## 8 — The cheap classifiers fabricate verdicts

### Where

[stage2_llm/worker.py](src/defense/services/workers/stage2_llm/worker.py) — the XLM-R /
DistilBERT path

### What's wrong

- **On any failure they emit `{"sentiment": "neutral", "score": 0.0}` for every comment**,
  which the dashboard renders as a model verdict. This is the exact pattern the README says
  was fixed for images ("a failed image fetch now reports as a failure instead of as a
  neutral verdict"), reintroduced one layer over.
- `_map_sentiment_label` returned `"neutral"` for **any** unrecognised label — including
  `LABEL_0`/`LABEL_1`, what a head with no `id2label` emits. Every comment neutral, reported
  as a measurement.
- **No `MODEL_STUB_MODE` check**: the documented default run ("No GPU / ML weights
  required") tries to download two multilingual encoders on the first routed post.
- `import torch` / `from transformers import pipeline` at module top, and both promoted to
  **core** dependencies in [pyproject.toml](pyproject.toml), contradicting the `ml` extra.
- One `pipe(texts)` call with the whole thread — a multi-GB allocation on a 2,857-comment
  post before the first token is embedded.

### Fix

Stub-mode aware, lazily imported, batched at 64, failures recorded **once** per process,
and an unmappable label is an **abstention, not a vote** — the ensemble carries on with the
voters that did speak.

### Test to leave behind

`test_an_unmappable_classifier_label_is_an_abstention_not_a_neutral`,
`test_classifiers_are_skipped_in_stub_mode`, and
`test_a_source_that_did_not_run_is_not_a_voter` in [test_ensemble.py](tests/test_ensemble.py).

---

## 9 — `/v1/analysis/export` is unbounded, unescaped and dishonest

### Where

[services/api/routers/analysis.py](src/defense/services/api/routers/analysis.py) —
`export_analysis`, `_generate_pdfs`, `_result_to_html`

### What's wrong

1. **Unbounded.** `LIMIT 5000` and then 5,000 WeasyPrint renders inside a single request:
   minutes of blocked worker and gigabytes of ZIP in memory, repeatable by any authenticated
   caller.
2. **`only_warnings` filtered _after_ the limit**, so every alert older than the newest
   5,000 posts silently vanished from a "download all warnings" export.
3. **Not tenant-scoped** — `SELECT … FROM analysis_results` with no filter.
   **⚠️ NOT FIXED — see [AUDIT_PASS8 §3](AUDIT_PASS8.md#3--analysis_results-is-not-tenant-scoped-anywhere).**
   The `campaign_id` parameter added below is an optional caller-supplied filter, not an
   isolation boundary, and `analysis_results` has no `tenant_id` column to filter on. Every
   reader of that table is cross-tenant. Fixing it needs a migration, so this sub-item
   remained open while the issue was marked fixed.
4. **HTML injection.** `post_text`/`summary`/`insight` were escaped by hand; `platform`,
   `language`, `post_type` and both emotion fields were not — and `post_type`/emotion are
   **LLM output**, the one source that can contain markup nobody reviewed.
5. **"Image: neutral 0.000" on text-only posts** — issue 8's pattern a third time, in the
   artefact a reader is most likely to treat as a record.
6. Duplicated `from datetime import …` imports, mid-file, twice.

### Fix

Capped at 200 per request with `offset` and `campaign_id`; `only_warnings` filters in SQL
via JSONB containment; every interpolated value goes through one `_esc()`; an absent
component renders as "not measured", never as neutral; a per-post render failure writes an
`.ERROR.txt` into the ZIP instead of losing the other 199; and `X-Export-Count` /
`X-Export-Limit` headers let a caller tell a truncated page from a complete export.

### Test to leave behind

`test_export_reports_a_missing_image_sentiment_as_missing`,
`test_export_escapes_model_written_fields`, `test_export_is_bounded`.

---

## 10 — Smaller ones

|     | Issue                                                                                                                                                                                                      | Fix                                                             |
| --- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------- |
| a   | `_parse_json`'s regex salvage applied to **every** task, so a truncated post-type response returned `{"labels": […]}` — a well-formed answer to a different question — and logged nothing                  | Opt-in (`salvage_labels=True`, batch responses only) and logged |
| b   | `traceback.print_exc()` left in the stance retry path                                                                                                                                                      | Removed                                                         |
| c   | `asyncio.gather(...)` without `return_exceptions` in the comment lane — one HF failure killed the whole stance merge                                                                                       | Isolated, and crashed batches are logged                        |
| d   | `_COMMENT_SUMMARY_ENABLED` was unreachable when `COMMENT_STANCE=false`                                                                                                                                     | Independent again                                               |
| e   | `c.sentiment_score.toFixed(2)` crashed the modal on a `null` score                                                                                                                                         | `Number.isFinite` guard                                         |
| f   | `f"router-0"` / `f"stage2-llm-0"` — f-strings with no placeholder                                                                                                                                          | Plain strings                                                   |
| g   | JWT secret rotation stopped working without a restart (cached `Settings`); `LLMClient` and `ModelRegistry` both froze their config at import, so a client built after a config change served the old model | All three read settings at construction / call time             |

---

## 11 — The React rewrite lost two shipped fixes

### Where

[dashboard/src/pages/Reports.jsx](dashboard/src/pages/Reports.jsx) ·
[dashboard/src/components/PostModal.jsx](dashboard/src/components/PostModal.jsx)

### What's wrong

- **OPEN_ISSUES §4 regressed.** Report cluster summaries — one real LLM call each — were
  surfaced through the API and the legacy dashboard by Pass 6. The React rewrite renders
  `summary` and a raw-JSON dump, and never mentions `embedding_clusters`. Computed, paid
  for, discarded, again. `test_the_dashboard_renders_the_expensive_clusters` was pointed at
  the deleted `dashboard/app.js` and so could not catch it.
- **`uncertain` rendered in the same grey as `neutral`**, making an abstention visually
  identical to a neutral verdict — the one distinction the ensemble exists to make.
- **`watchlist_alert_reason` never reached the UI**: added to the schema and the assembler
  but not to `AnalysisResultResponse` or `_row_to_result`, so the dashboard could only ever
  describe every alert the same way.

### Fix

Clusters rendered with their `embedding_clusters_are_stub` honesty flag; `uncertain` in
amber; the reason wired end-to-end and given its own **"Why it alerted"** column on the
Warnings page. `test_report_clusters_survive` now reads the React sources.

---

## What else changed in this pass

Not defects — the upgrades the audit motivated. Full rationale in the module docstrings.

- **[libs/ensemble.py](src/defense/libs/ensemble.py)** — combination and abstention
  (`uncertain` ≠ `neutral`). Four labellers vote on each comment: the Stage-1 heuristic,
  XLM-R, DistilBERT and the LLM. **`COMMENT_LLM_MODE=all` is the default** — every comment
  with text to read gets an LLM verdict, grounded on the post summary, so the UI can show
  three comparable model answers on the same comment. `should_escalate()` and
  `COMMENT_LLM_MODE=escalate` keep the cost-optimised path available (measured at ~20% of
  comments), with every decision recorded as a counted reason.
- **The half-the-room rule.** With four voters an exact 2-2 split is common, and calling all
  of those "uncertain" throws away the one opinion that had the post as context. A label
  stands when at least half the voters back it **and the LLM is one of them**; below half,
  or with the LLM dissenting, it still abstains. A tie-break is never dressed up as
  consensus — `label_agreement` reports the real 0.5 and `tie_broken_by` names the decider,
  in the schema, the API and the UI.
- **`llm_labelled` is counted from the labels that came back**, not from what was requested,
  and `capped_out` reports what `COMMENT_STANCE_MAX_PER_POST` dropped — so "every comment
  got an LLM verdict" is checkable rather than assumed. Note `run_all.py` sets that cap to
  **40**; set it to 0 to label every comment, at real runtime cost on long threads.
- **[libs/comment_groups.py](src/defense/libs/comment_groups.py)** — near-duplicate
  comments take their representative's label, tagged `propagated`. Exact match after
  normalisation, so the prompt would have been identical: a cache, not an approximation.
- **Measured on the 2,857-comment corpus post: 80.4% unanimous, 5.5% deduplicated**; in
  `escalate` mode 19.5% of comments would reach the LLM. Cost is comment-dominated (~85% of
  LLM calls), so this is the lever the routing rate never was.
- **[eval/build_gold_set.py](eval/build_gold_set.py) + [eval/score_gold.py](eval/score_gold.py)**
  — a 300-comment stratified gold set. **Every label is `null` on purpose**: seeding them
  from any model in this repo and then scoring that model against them measures agreement
  with itself. Until a human adjudicates it, **the project still has no measured accuracy** —
  that is the honest status, and the scorer refuses to print a number that pretends otherwise.
- `watchlist_timeline` in the analytics MCP, plus `watchlist_alert` / `label_agreement`
  columns on `analysis_events` and `label_agreement` / `label_source` on
  `comment_sentiments` — turning a per-post boolean into an early-warning series.

## Verified against the real models

Everything above was first checked with a fake LLM, which proves the wiring and nothing
about the models. It was then run for real: **Ollama `qwen2.5:7b`** plus both real HF
checkpoints, on 12 real Bangla comments from the largest corpus post, with the response
cache disabled so every label is a genuine model call. That run found two more defects that
no amount of reading or unit-testing would have surfaced.

### 12 — The second classifier silently lost to the LLM for GPU memory

`classifier_load_failed  distilbert  CUDA out of memory. Tried to allocate 352.00 MiB. GPU 0
has a total capacity of 3.68 GiB of which 285.00 MiB is free.`

Ollama holds 2.76 GB of the 4 GB card for `qwen2.5:7b`. The first classifier took what was
left; the second got nothing, was marked permanently failed, and **dropped out of the
ensemble** — leaving two voters where the UI promises three, non-deterministically,
depending on which won the race.

These are ~135M-parameter encoders. CPU is entirely adequate for them, and a working third
opinion beats an absent one. `_get_pipeline` now retries on CPU when the GPU rejects it,
serialises construction behind a lock so the two cannot race for the last few hundred MB,
and records the device it ended on. `STAGE2_CLASSIFIER_DEVICE=cpu` skips the GPU attempt —
the right setting when the card is dedicated to the LLM, and **3× faster end to end** here
(22.9 s vs 78.8 s for 12 comments) because it stops the classifiers thrashing against
Ollama.

### 13 — The hash stub was voting in the ensemble

The same run printed `stage-1 method_breakdown: {'fast': 1, 'stub': 11}`. With no sentiment
model loadable, Stage 1 labels via `stub` — the deterministic hash fallback whose own schema
description reads _"it is reproducible, and it is not sentiment"_. Every one of those was
being seeded into the ensemble as a heuristic vote.

On this thread that is **11 of 12 comments**: hash noise would have been the single largest
voting bloc, and on a 2-2 split it would have decided the label. `_seed_heuristic_vote` now
votes only for methods that represent an actual reading (`fast`, `emoji`, `model`, `llm`,
`link`); `stub` and `failed` abstain, exactly as an unavailable model does.

### The run

```text
comment                                    HEUR     XLM-R     DBERT     LLM      -> FINAL
ও আচ্ছা 😐                                  neutral  positive  positive  neutral  -> neutral [tie] (0.5)
Chatte thak hae golam 🤡                    —        negative  negative  negative -> negative (1.0)
ইলিয়াস ভাই চালিয়ে যান, আপনি সঠিক পথেই আছেন   —        positive  positive  positive -> positive (1.0)
…
voters: heuristic, xlmr, distilbert, llm
llm_labelled 12/12 (100%) · unanimous 67% · abstained 0 · mode=all · capped_out=0
breakdown {positive: 10, negative: 1, neutral: 1, uncertain: 0} · sums=True
```

The `—` in the HEUR column is issue 13 working: those are the stub labels, correctly
declining to vote. The one `[tie]` is the half-the-room rule — an emoji comment where the
two encoders read the 😐 as positive and both the heuristic and the LLM read it as neutral.

## What was NOT verified

- **[tests/test_pipeline_e2e.py](tests/test_pipeline_e2e.py)** still fails. It shells out to
  `run_all.py --reset`, which wipes the live datastores and restarts the dev API on :8001;
  it needs a full Docker + Ollama stack. It was failing before this pass too. Not run here
  because `--reset` is destructive to a running environment.
- **The routing rate was not re-measured end to end** — that needs `STAGE1_LLM=true` against
  Ollama. See the note in issue 5 about why a stub-mode measurement would be misleading.

# Open Issues — Pass 8 remediation list

**Found:** 14 August 2026, fresh-eyes audit of the tree in the state Pass 7 left it —
that is, the ~3,000-line uncommitted working diff on `new_updates`, which had never been
reviewed by anyone. Pass 7's list is [AUDIT_PASS7.md](AUDIT_PASS7.md); Pass 6's is
[OPEN_ISSUES.md](OPEN_ISSUES.md).

**Status: 5 of 5 FIXED** and regression-tested. The fifth (#3) was resolved in Pass 9 via full multi-tenant schema, model, API, worker envelope, and MCP isolation.

**Test suite: 826 passing / 1 failing in 153 s → 832 passing / 1 skipped in ~28 s.**

Two of the five are **incomplete Pass 7 fixes** — the interesting kind, because in both
cases the fix that shipped was correct as far as it went and the defect survived one layer
over. #1 is issue 13 (the hash stub voting in the ensemble): the stub was correctly stopped
from *voting*, and its label went on being *counted*. #4 is issue 3/11 (the watchlist alert
diverging between paths): the fix gave the two paths the same rule by writing it out twice.

---

## At a glance

| #                                                             | Issue                                                 | Impact                                                          | Status         |
| ------------------------------------------------------------- | ----------------------------------------------------- | --------------------------------------------------------------- | -------------- |
| [1](#1--the-hash-stubs-label-still-had-the-last-word)         | The hash stub's label still had the last word          | **Honesty** — two contradicting aggregates in one payload       | ✅ Fixed       |
| [2](#2--a-plain-pytest-wipes-the-developers-datastores)       | A plain `pytest` wipes the developer's datastores      | **Destructive** — and 85% of the suite's wall clock             | ✅ Fixed       |
| [3](#3--analysis_results-is-not-tenant-scoped-anywhere)       | `analysis_results` is not tenant-scoped anywhere       | **Cross-tenant data exposure**                                  | ✅ Fixed (Pass 9) |
| [4](#4--the-watchlist-rule-shipped-as-two-copies)             | The watchlist rule shipped as two copies               | Divergence between the cheap and expensive paths                | ✅ Fixed       |
| [5](#5--one-voter-was-counted-as-unanimity)                   | One voter was counted as unanimity                     | **Honesty** — inverts the headline agreement number             | ✅ Fixed       |

---

## 1 — The hash stub's label still had the last word

### Where

[stage2_llm/worker.py](src/defense/services/workers/stage2_llm/worker.py) `_merge_ensemble`

### What's wrong

Pass 7 issue 13 stopped the deterministic hash stub from voting in the ensemble —
`_seed_heuristic_vote` refuses methods that do not represent a reading, and it works. But
the stub's label was already sitting in `comment["sentiment"]` from Stage 1, and the merge
only overwrote that field **when at least one voter spoke**:

```python
if verdict.voters:
    c["sentiment"] = verdict.label
    ...
# ...and when nothing voted, Stage 1's label simply stayed.
```

So in the configuration Pass 7 itself measured — no sentiment model loadable, `stub` for 11
of 12 comments — plus no cached classifier weights and no LLM verdict, **every comment kept
its hash label, and `sentiment_breakdown` counted it as a measurement** while
`ensemble.abstained` reported that all of them had abstained. One payload, two aggregates,
no relationship between them: precisely Pass 7 issue 2's pattern, reintroduced by the fix
for issue 13 not going one line further.

`uncertain` was already plumbed end to end for exactly this — schema enum, breakdown
bucket, amber in the UI, an API filter. Nothing was using it on this path.

### Reproduce

```python
comments = [{"text": "ও আচ্ছা", "sentiment": "positive", "method": "stub", "kind": "text"}, ...]
for c in comments: _seed_heuristic_vote(c)
_merge_ensemble({"comments": comments}, None, escalation_reasons={}, dedup_stats={}, voters_used=[])
# per-comment       : positive, negative, positive   (label_agreement: None)
# sentiment_breakdown: {'positive': 2, 'negative': 1, 'neutral': 0, 'uncertain': 0}
# ensemble.abstained : 3 of 3
```

### Fix

A comment no labeller read is `uncertain`, with `sentiment_score: 0.0`,
`label_agreement: 0.0`, `label_voters: 0` and `label_sources: []`. `method` is deliberately
left alone, so `provenance` still discloses that the stub was the only thing that ran —
the breakdown now says nothing was measured, and the provenance says what failed to
measure it. `ensemble.unread` counts them.

The UI needed the same treatment: `label_voters: 0` rendered as "Agree: 0%" beside
"Score: 0.00", which reads as *the labellers disagreed completely* rather than *nobody
read this*. Both now render as "not read".

### Test to leave behind

`test_a_comment_nobody_read_is_uncertain_not_its_stub_label` in
[test_comment_ensemble_merge.py](tests/test_comment_ensemble_merge.py) — it asserts the
per-comment labels and the aggregate agree, which is the property the defect violated.

---

## 2 — A plain `pytest` wipes the developer's datastores

### Where

[tests/test_pipeline_e2e.py](tests/test_pipeline_e2e.py) · [pyproject.toml](pyproject.toml)

### What's wrong

The test shells out to `run_all.py --reset` — which is a Redis `FLUSHALL` plus a truncate of
Postgres and ClickHouse — with **no marker, no skip guard and no opt-in**. Any developer
running `pytest` on a machine with a live dev stack lost its contents.

It also could not pass: it needs a full Docker + Ollama stack, so it spent **129.8 s of the
suite's 153 s** failing. That is why Pass 7's headline "817 passing in 22 s" is not
reproducible from a bare `pytest` — the measurement must have excluded this file, and
nothing recorded that it had to be.

AUDIT_PASS7 states the right decision — *"not run here because `--reset` is destructive to a
running environment"* — but left it in prose. Prose does not stop a test from running.

### Fix

`@pytest.mark.e2e` + `@pytest.mark.destructive` + a `skipif` on `RUN_DESTRUCTIVE_E2E`,
with both markers registered in `pyproject.toml`. Also fixed while there: `sys.executable`
instead of bare `"python"` (the venv is where the deps are), and the subprocess teardown
moved into a `finally` with a `kill` fallback, so a failed run stops orphaning workers that
hold Redis consumer groups.

### Test to leave behind

`test_the_destructive_e2e_test_cannot_run_by_accident` in
[test_audit_regressions.py](tests/test_audit_regressions.py) — asserts the guard exists,
names the opt-in variable, and is closed when the variable is unset.

---

## 3 — `analysis_results` is not tenant-scoped anywhere

> Reported, not fixed. This one needs a migration and a decision.

### Where

[deploy/init-db.sql](deploy/init-db.sql) `analysis_results` · every reader of that table:
[analysis.py](src/defense/services/api/routers/analysis.py) (`export_analysis`,
`get_post_comments`, the overview aggregates) · [reports.py](src/defense/services/api/routers/reports.py) ·
[search.py](src/defense/services/api/routers/search.py) · [usage.py](src/defense/services/api/routers/usage.py) ·
[retrieval_mcp/server.py](src/defense/mcp_servers/retrieval_mcp/server.py)

### What's wrong

Pass 7 issue 9 lists "**Not tenant-scoped** — `SELECT … FROM analysis_results` with no
filter" as sub-item 3 of five, and marks issue 9 ✅ Fixed. The other sub-items were fixed
(bounding, SQL-side `only_warnings`, escaping, honest absence, duplicate imports). This one
was not, and the fix list quietly stops mentioning it — the `campaign_id` parameter that was
added is an *optional caller-supplied filter*, not an isolation boundary.

It is not a small oversight, because tenancy is otherwise real and enforced in this
codebase: `tenant_id` comes from the `api_keys` row rather than the client (§5.6),
`tenant_is_privacy_locked` fails closed, and `get_analysis` scopes `jobs` by
`selector->>'tenant_id'`. So the system takes care to bind a principal to a tenant and then
serves that principal every tenant's analysed posts, comment text included.

**`analysis_results` has no `tenant_id` column at all**, and no `job_id` to join back to
`jobs`. So this cannot be fixed at the query layer — which is presumably why it was skipped.

### Recommended fix

A migration adding `tenant_id` to `analysis_results` (and to `analysis_events` /
`comment_sentiments` in ClickHouse, which have the same gap), written by the assembler from
the job envelope, backfilled via `jobs.selector`, then a filter on every read path above.
Until then this is a known cross-tenant read in any deployment with more than one tenant —
which is worth stating in the docs rather than leaving inside a fix list marked ✅.

---

## 4 — The watchlist rule shipped as two copies

### Where

[stage2_llm/worker.py](src/defense/services/workers/stage2_llm/worker.py)
`_watchlist_verdict` · [assembler/builder.py](src/defense/services/workers/assembler/builder.py)
`_watchlist_alert`

### What's wrong

Pass 7 issue 3 correctly identified that computing `watchlist_alert` only in Stage 2 made
the alert depend on the routing gate being broken, and fixed it by computing the alert in
the assembler too. Both implementations of the two rules (`always` → any mention,
`favored` + `opposing` → "under attack") were then written out **verbatim, twice**.

The whole point of the fix is that the cheap path and the expensive path answer the same
question the same way. Two copies is the setup for them not to — and this codebase has
already been bitten twice by exactly that (issue 11: "the React rewrite lost two shipped
fixes").

A supporting symptom: `test_opposing_an_undeclared_target_does_not_alert` was passing while
its arguments were in the wrong order, because an empty first argument short-circuits to
`False`. A test that returns the right answer from the wrong path is not guarding anything.

### Fix

The rule moved to `libs/stance_targets.watchlist_verdict()` — the module that already owns
polarity semantics and alias matching, and is already pure and offline-testable. Both
callers now call it.

### Test to leave behind

`test_both_paths_share_one_alert_rule` — asserts both modules resolve to the *same function
object*, so a future re-inlining fails rather than drifts.

---

## 5 — One voter was counted as unanimity

### Where

[libs/ensemble.py](src/defense/libs/ensemble.py) `combine`, `agreement_summary`

### What's wrong

`unanimous = len(tally) == 1` — true whenever every voter agreed, **including when there was
one voter**. `agreement_summary.unanimous_share` documents itself as

> the fraction of comments every voter labelled the same way; **that is the share the
> expensive model never needed to see**

which inverts the truth exactly when the LLM is the only labeller that ran — stub-mode
Stage 1 abstaining (issue 13), no cached classifier weights, `COMMENT_LLM_MODE=all` sending
every comment to the LLM. Every comment then reports `unanimous`, and the headline says the
cheap voters settled a corpus the expensive model carried single-handed:

```
one voter (llm only): unanimous_share = 1.0 | mean_agreement = 1.0
```

The per-comment path already had this right — Pass 7 added `label_voters` precisely because
`label_agreement: 1.0` over one voter "reads as consensus when only one model spoke". The
corpus-level number never got the same treatment.

### Fix

`unanimous` requires at least two voters. `agreement_summary` additionally reports
`single_voter` / `single_voter_share` (one labeller — nothing to agree with) and `unread`
(no labeller at all, per issue 1), because `unanimous_share` is only interpretable next to
them. All three are in the schema with descriptions that say so.

### Test to leave behind

`test_one_voter_is_not_unanimity` — a two-comment corpus where only the LLM voted reports
`unanimous_share: 0.0` and `single_voter_share: 1.0`, and the labels still stand (a single
voter is evidence, just not agreement).

---

## What was NOT verified

- **Not re-run against the real models.** Pass 7's two most interesting findings (#12, #13)
  were only visible with Ollama + real HF checkpoints against real Bangla comments. Every
  fix here is exercised offline; #1 and #5 are *reachable only* in the degraded
  configuration that run found, so they are the two most worth re-checking on a real run.
- **`analysis_results` tenant scoping (#3)** — reported, not fixed.
- **The destructive e2e test still does not pass.** It is now skipped by default rather than
  failing by default; nothing was done to make the stack it needs available.
- **The routing rate was not re-measured**, for the same reason as Pass 7: it needs
  `STAGE1_LLM=true` against Ollama, and a stub-mode measurement would be measuring the stub.

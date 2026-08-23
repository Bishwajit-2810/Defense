# Stage 2 — Selective LLM and the Eight-Labeller Comment Ensemble

> **Scope.** What the LLM is spent on: the post-level lane (summary, post type,
> insight) and the comment lane (seven cheap classifier heads + the
> context-aware LLM stance pass, combined by one writer). Plus the caching,
> batching and truncation machinery that makes those calls affordable and
> honest.
>
> Code: [`workers/stage2_llm/worker.py`](../src/defense/services/workers/stage2_llm/worker.py)
> (the two lanes), [`prompts.py`](../src/defense/services/workers/stage2_llm/prompts.py),
> [`cache.py`](../src/defense/services/workers/stage2_llm/cache.py),
> [`libs/ensemble.py`](../src/defense/libs/ensemble.py) (pure combination +
> escalation logic). Position in the chain: [PIPELINE.md](PIPELINE.md) §1.

---

## 1. Two lanes, run concurrently

`asyncio.gather` runs both lanes over the same post. They share no mutable state:
lane A writes only to its returned dict; lane B mutates
`stage1_result["comment_analysis"]` in place.

| Lane | Runs when | Tasks |
| ---- | --------- | ----- |
| **A — post level** | `task_flags.post_level_routed` (the router's gate) | `post_summary`, `post_type`, insight / topic refinement |
| **B — comments** | **Always, for every post** | Seven cheap heads + the LLM stance pass + the comment-thread summary |

That asymmetry is the whole cost story. Post-level work is gated; comment work is
not, because the ensemble lives here and a bypassed post's comments would
otherwise carry a Stage-1 heuristic label alone. Measured consequence: **85–96%
of all LLM calls are comment-level**, so the router's gate governs 30–55% of
spend depending on how good Stage 1 is ([ROUTER.md](ROUTER.md) §7).

`processing.post_level_routed` and `processing.comment_llm_used` are carried on
the result rather than inferred, because "a `stage2_result` exists" is now true of
every post and would report 100% LLM use.

## 2. Lane A — post-level tasks

### Post summary, in the post's own language

A Bangla post gets a Bangla summary. `post_summary_lang` records the language and
`post_summary_grounding` records which inputs informed it (caption, comments,
OCR, image).

**Summarization has its own role.** `summary` resolves to a different model from
`stage2`: classification picks from a fixed vocabulary and wants a cheap
constrained model, while summarization writes prose and wants a fluent one. The
expensive model is therefore spent once per post, not on every classification
call — and that split is also the honest version of the cost story. See
[LLM_BACKENDS.md](LLM_BACKENDS.md) §2.

### Truncation recovery — and why it was biased against Bangla

`finish_reason` is inspected. A reply that hit its token ceiling is
auto-continued up to `LLM_MAX_CONTINUATIONS` (2), trimmed back to its last
complete sentence (Bangla `।`/`॥` included in the sentence-end set), flagged
`post_summary_truncated`, and **never cached**.

The ceilings used to be hardcoded literals (512 summary, 512 insight, 256 comment
summary, 128 post type) with nothing checking whether a reply had actually
stopped at them. Bangla costs far more tokens per character than English under
these tokenizers, so one fixed ceiling truncates Bangla and Banglish while
sparing English — the exact wrong bias for this project. Current ceilings:
`SUMMARY_MAX_TOKENS=1024`, `INSIGHT_MAX_TOKENS=768`,
`COMMENT_SUMMARY_MAX_TOKENS=640`, `POST_TYPE_MAX_TOKENS=128`. The continuation
prompt is deliberately language-neutral so the model does not switch language
mid-summary.

Truncation rate belongs in the per-language bucket table, not the aggregate —
[evaluation.md](evaluation.md) §4.

### Post type and insight

`post_type` uses the nine-label taxonomy shared from
[`libs/labels.py`](../src/defense/libs/labels.py), so Stage 1, this prompt and
the router gate cannot drift apart.

The insight task refines `topics`/`intents` and writes a one-line `insight`.
**This task's output was discarded for a period:** the call ran and was paid for,
but the assembler read `topics`/`intents` from Stage 1 only and never read
`insight` at all, which was also absent from `output_schema.json`. It is merged
now (`builder._merge_stage2_labels`, schema `1.3`), pinned by
`tests/test_stage2_insight_survives.py`, and reaches the dashboard as its own
section beside the summary plus a Trace-tab row.

## 3. Lane B — the eight-labeller comment ensemble

### The roster

Every analysed comment collects up to **eight** independent verdicts in
`parallel_labels`:

| Source | Checkpoint | Kind |
| ------ | ---------- | ---- |
| `xlmr` | `tabularisai/multilingual-sentiment-analysis` | cheap, batched on CPU |
| `distilbert` | `lxyuan/distilbert-base-multilingual-cased-sentiments-student` | cheap |
| `twitter_xlmr` | `cardiffnlp/twitter-xlm-roberta-base-sentiment-multilingual` | cheap |
| `banglabert` | `ADn-001/banglabert-sentnob-sentiment` | cheap |
| `bengali_sentiment_bert` | `ahs95/banglabert-sentiment-analysis` | cheap |
| `mbert` | `nlptown/bert-base-multilingual-uncased-sentiment` | cheap |
| `modernbert` | `clapAI/modernBERT-base-multilingual-sentiment` | cheap |
| `llm` | the `stage2` role, context-aware stance pass | expensive, per batch |

Override each slot with `STAGE2_CLASSIFIER_1..7`. `ensemble.CHEAP_SOURCES` must
match `Settings.stage2_classifier_names` exactly — a voter missing from the first
list is collected into `parallel_labels` and then ignored by `should_escalate`,
which is a silent way to buy a model and not use it. `tests/test_ensemble.py`
asserts the two agree.

"Cheap" is per comment, not per run: these are ~135–280M-parameter encoders that
batch, while the LLM is a per-batch generation call. What they are **not** is
seven independent readings — five of the seven are multilingual encoders trained
on overlapping data, so their agreement is **correlated** and a 7–0 vote is
weaker evidence than seven unrelated models would be. State that whenever the
agreement number is quoted.

### Only a model may label a comment

Stage 1's emoji + keyword rule stopped voting on 17 Aug 2026. It answers on
*every* comment — and 71.3% of those answers are the deterministic hash stub or
the emoji rule — so a voter that can never abstain makes every agreement number
unfalsifiable. On the corpus's Bangla threads that rule was the *majority* voter.
Its label is still disclosed in `method` / `provenance`; it is just never counted
as a verdict. **Do not add `heuristic` back to make abstentions go away.**

### A voter that cannot vote abstains

A head that fails to load, or returns a label outside
`{positive, negative, neutral}`, contributes **nothing** — never a fabricated
neutral. The reporting that makes this legible:

| Field | Meaning |
| ----- | ------- |
| `label_voters` | How many sources actually spoke |
| `label_sources` / `winning_sources` | Who spoke, and who was behind the winner |
| `label_agreement` | Share of voters behind the winning label |
| `unanimous` / `abstained` | Self-explanatory |
| `tie_broken_by` | Set when an exact tie was decided by one source rather than by weight of numbers — a tie-break is not consensus, and `agreement` still reports the tie |

Zero voters is **`uncertain`**, not `neutral`: neutral is a verdict about the
comment, uncertain is a verdict about our confidence. Without this,
`label_agreement: 1.0` rendered as "100% agree" for a comment one model read.

A declared-but-silent head is logged at WARNING
(`stage2_cheap_voters voted=2 declared=7`). Five of the original roster's
checkpoints were base encoders or did not exist on the Hub, and voted **zero**
times while reading as coverage. `deploy/prefetch_classifiers.py` downloads the
seven and runs each on a Bangla/English/Banglish probe, so "downloaded" is never
mistaken for "voting". `MODEL_STUB_MODE=true` means *download nothing*, so an
unprefetched box degrades to LLM-only rather than stalling on a 3 GB fetch.

### One combiner, one writer

`ensemble.combine()` is the **single writer** of a comment's final `sentiment`.
Three places used to write it and the last one won — which is how a post could
report eight negative comments while every comment in the list read neutral, and
how `method_breakdown` reported 0% LLM for a run in which every comment had been
sent to one. `DEFAULT_MIN_AGREEMENT=0.0` means plurality wins; below that share
the ensemble abstains.

The dashboard shows all eight columns on one row, and the comparison is either
fully populated for a comment or absent for it — never a partial row.

### Who gets an LLM call

`COMMENT_LLM_MODE` selects the policy:

- **`all` (default)** — every comment with text gets an LLM verdict, so the UI can
  show the LLM beside all seven heads on the same comment.
- **`escalate`** — `ensemble.should_escalate()` spends the LLM only where it buys
  something. Its reasons are logged and counted, so the share of budget each
  consumes is measured rather than assumed:

  | Reason | Escalates? |
  | ------ | ---------- |
  | `no_text` (`emoji`/`filtered`/`link`) | No — cheapest possible way to waste tokens |
  | `watchlist_mention` | **Yes** — a wrong call about a listed entity is the expensive kind of wrong |
  | `insufficient_cheap_voters` (< 2 voted) | **Yes** |
  | `cheap_disagreement` | **Yes** |
  | `cheap_consensus` | No |

`FILTER_EMOJI_ONLY=true` keeps textless kinds out of batches in either mode.
Note what follows from Stage 1's rule no longer voting: those comments end up with
**no verdict at all** — `uncertain` at zero voters — and are counted in
`reaction_only`. An emoji reaction is crowd signal, not a sentiment measurement,
and the output now says so rather than passing off a lexicon guess as a label.

### Two dedup layers

1. **Selection dedup (router).** Repeat comment texts are collapsed before Stage 2
   ever sees them; only the most-liked occurrence is selected
   ([ROUTER.md](ROUTER.md) §5).
2. **Verdict propagation (`COMMENT_DEDUP_PROPAGATE=true`).** Within the pool,
   identical (post-normalisation) comments share their representative's LLM
   verdict. Grouping is over the **pool**, not the whole thread — grouping the
   thread would let a representative outside the analysis set propagate a verdict
   it was never given. Reported as `ensemble.deduplicated` /
   `duplicate_share`. Switchable off, because "every comment got its own call" is
   sometimes the claim being made.

### The pool

`pool = [c for c in comments if c.get("stage2_selected") is not False]`. Only the
router ever sets that flag `False`, so an older payload — or a run with the cap
off — behaves as it always did. Every voter reads this one list.

### Coverage reporting

`comment_analysis.ensemble` carries: `analysed`, `not_analysed`,
`llm_labelled`, `llm_share_analysed`, `capped_out`, `mode`, `deduplicated`,
`duplicate_share`, `voters`. At the shipped `ROUTER_COMMENT_TOP_N=0` the
post-level breakdown and the per-comment table describe the same comments; with a
positive cap they diverge and these fields say by how much.

### Batching

Comments batch at `COMMENT_STANCE_BATCH=25` with `COMMENT_STANCE_CONCURRENCY=3`
batches in flight, `COMMENT_STANCE_BATCH_RETRIES=1`, and index-alignment
assertions on every reply. That replaced a strictly sequential loop — which is
the reason the old per-post caps existed at all.

## 4. Response cache

Key: `llm_cache:{backend}:{resolved_model_id}:{task}:{content_hash}`, TTL 7 days.

**The model slot must be the resolved model id, not a role label.** It used to be
the role (`stage2`, `vlm`), so the concrete model id was not in the key at all —
and with 7-day entries, changing the model and re-running the same posts returned
the *previous* model's answers. That is a correctness bug and a direct threat to
any model comparison: a bake-off across BanglaBERT, XLM-R, gemma3:4b, qwen2.5:7b
and llama-3.3-70b would silently compare each model against its own cached
output. Splitting `summary` onto its own role turned the latent bug active,
because two models are then in play under one key.

Set `LLM_CACHE_DISABLED=1` for evaluation runs. Truncated replies are never
cached.

## 5. Watchlist stance

Stage 2 re-scores watchlist targets with the post as context and aggregates them
(`libs/stance_scoring.py`, `libs/stance_targets.py`). Deterministic matching
already ran in Stage 1, so a bypassed post still carries mention volume. Full
design: [stance_targets.md](stance_targets.md).

## 6. Configuration

| Env var | Default | Effect |
| ------- | ------- | ------ |
| `COMMENT_STANCE` | `true` | The context-aware LLM stance pass |
| `COMMENT_STANCE_ROLE` | `stage2` | Which role serves it |
| `COMMENT_STANCE_BATCH` / `_CONCURRENCY` / `_BATCH_RETRIES` | `25` / `3` / `1` | Batch queue shape |
| `COMMENT_STANCE_MAX_PER_POST` | `0` | A second, tighter cap *inside* the router's set. `0` is the right value — a positive one gives the LLM fewer comments than the cheap heads got |
| `COMMENT_LLM_MODE` | `all` | `all` \| `escalate` |
| `COMMENT_DEDUP_PROPAGATE` | `true` | Share a verdict across identical comments in the pool |
| `FILTER_EMOJI_ONLY` | `true` | Keep textless kinds out of batches |
| `STAGE2_CLASSIFIERS_ENABLED` | `true` | The seven cheap heads |
| `STAGE2_CLASSIFIER_1..7` | see §3 | Roster slots |
| `STAGE2_CLASSIFIER_DEVICE` | `auto` (repo `.env` ships `cpu`) | Where the heads run |
| `COMMENT_SUMMARY` | `true` | The comment-thread summary |
| `SUMMARY_ROLE` | `summary` | The fluent model, separate from `stage2` |
| `SUMMARY_MAX_TOKENS` / `INSIGHT_MAX_TOKENS` / `COMMENT_SUMMARY_MAX_TOKENS` / `POST_TYPE_MAX_TOKENS` | `1024` / `768` / `640` / `128` | Per-task ceilings |
| `LLM_MAX_CONTINUATIONS` | `2` | Auto-continuation budget |
| `LLM_CACHE_DISABLED` | `false` | Bypass the response cache |

Full list with failure modes: [env.example.md](env.example.md).

## 7. Evidence class

| Claim | State |
| ----- | ----- |
| Summary in the post's own language; grounding recorded | ✅ Measured |
| Separate `summary` role resolves to a different model | ✅ Measured |
| Truncation recovery + `post_summary_truncated` | ✅ Measured |
| Post-type classification from the shared taxonomy | ✅ Measured |
| Insight survives to the API, stores and dashboard | 🟡 Works, unmeasured (pinned by test) |
| Context-aware comment stance | ✅ Measured |
| Abstention / `label_voters` / `uncertain` at zero voters | ✅ Measured |
| Roster prefetch + vote check | ✅ Measured |
| Cache keyed on the resolved model id | ✅ Measured |
| Bounded-concurrency batch queue | ✅ Measured |
| Eight-labeller ensemble **accuracy** | 🟡 Works, unmeasured — the roster grew from 2 heads to 7 on 16 Aug 2026 and **has not been scored since**; scoring needs the gold set |
| Comment-thread summary quality | 🟡 Works, unmeasured |
| Dedup savings per corpus | 🟡 Works, unmeasured |
| VLM image-grounded summary | ⚠️ **Unexercised** — no image is fetchable |

Cross-references: [PIPELINE.md](PIPELINE.md) · [ROUTER.md](ROUTER.md) ·
[STAGE1_NLP.md](STAGE1_NLP.md) · [LLM_BACKENDS.md](LLM_BACKENDS.md) ·
[ASSEMBLER.md](ASSEMBLER.md) · [models.md](models.md) ·
[evaluation.md](evaluation.md) · [cost_estimation.md](cost_estimation.md).

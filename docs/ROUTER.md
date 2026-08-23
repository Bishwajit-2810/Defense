# The Router — The "Thinking Layer"

> **Scope.** The component the whole cost claim rests on: six gates that decide
> how much intelligence each post needs, plus a *second, separate* decision about
> which **comments** Stage 2 analyses. What each gate reads, what it costs to get
> a field name wrong, and how to read the routing rate honestly.
>
> Code: [`workers/router/rules.py`](../src/defense/services/workers/router/rules.py)
> (pure decision logic, no I/O) and
> [`workers/router/router.py`](../src/defense/services/workers/router/router.py)
> (the stream worker). Position in the chain: [PIPELINE.md](PIPELINE.md) §1.

---

## 1. What it decides — two decisions, not one

| Decision | Function | Effect |
| -------- | -------- | ------ |
| **Post-level LLM work** | `should_use_llm(stage1_result, options)` | Sets `task_flags.post_level_routed`. Governs summary / post-type / insight. |
| **Which comments Stage 2 analyses** | `select_comments_for_stage2(...)` | Marks each comment `stage2_selected` and writes `comment_analysis.stage2_selection`. |
| *(derived)* **Which post-level tasks** | `get_task_flags(stage1_result, options)` | So a confidently-typed post does not pay for a redundant `post_type` call. |

**Neither decision routes a post away from Stage 2.** Every post is `XADD`ed to
`llm:stage2:queue` — see [PIPELINE.md](PIPELINE.md) §1. The gate is a spend
decision, not a destination.

## 2. The six gates

`should_use_llm` returns `(use_llm, reasons)`. **Any** rule firing is enough to
route, and every reason is a human-readable string logged on the decision.

| # | Rule | Fires when | Threshold knob (default) |
| - | ---- | ---------- | ------------------------ |
| 1 | **Low / absent confidence** | Stage-1 overall confidence `< threshold`, **or absent entirely** | `ROUTER_CONFIDENCE_THRESHOLD` (`0.8`) |
| 2 | **Post type unknown or weak** | `post_type is None`, or its confidence `< threshold` | `ROUTER_POST_TYPE_CONFIDENCE_THRESHOLD` (`0.8`) |
| 3 | **Caller asked for a Stage-2 summary** | `options.want_summary` | `ROUTER_SUMMARY_ROUTES` (`false`) |
| 4 | **Image post with no image verdict** | `photo_count > 0` and image sentiment is `None` | — |
| 5 | **High toxicity** | `toxicity_score > threshold` | `ROUTER_TOXICITY_THRESHOLD` (`0.7`) |
| 6 | **Long code-mixed text** | caption chars `>` limit **and** Banglish/mixed script | `ROUTER_LONG_TEXT_CHARS` (`1000`) |

Two design decisions inside that table are worth stating outright.

**An absent confidence routes.** Rule 1 treats `None` as uncertain rather than
ignoring it, because silently skipping the gate is exactly how it came to be
inert (§3).

**Rule 3 ships OFF, and that is what makes the gate a gate.** It defaulted to
`True` for a while, which made the whole router a pass-through: one rule firing
is enough, so every post routed and `estimated_llm_share` became the constant
`1.0`. The product requirement it served — every post gets a summary — is still
met, but by Stage 1, which writes one itself and hands it to the assembler.
Stage 2 re-summarises only when someone asks for the better model. Set
`ROUTER_SUMMARY_ROUTES=true` to go back, and expect a 100% routed share.

**The thresholds are env-overridable because a gate that cannot be swept is
merely arbitrary.** `eval/sweep_threshold.py` turns the cascade into an
empirical cost curve over `ROUTER_CONFIDENCE_THRESHOLD` — see
[evaluation.md](evaluation.md) §4.

## 3. Named readers — why a field rename is a test failure

Every rule reads its inputs through a named reader (`read_overall_confidence`,
`read_photo_count`, `read_image_sentiment`, `read_text_length`,
`_is_code_mixed`) that accepts **all three shapes the pipeline emits** for one
concept: Stage 1's flat `confidence`, the assembler's nested
`confidence.overall`, and the legacy `overall_confidence`.

This exists because three of the six rules were silently dead, and the failure
mode was not "wrong answer" but "100% routed":

- Rule 1 read `overall_confidence`; Stage 1 emits `confidence`. The
  `is not None` guard therefore always short-circuited, and a post with
  `confidence: 0.0` passed a `0.65` threshold.
- Rule 2 read `post_type is None` — which Stage 1 sets *deliberately* as a
  "Stage 2 will fill this" placeholder. It fired for every post.
- Rules 4 and 6 read `photo_urls` / `caption`, which live only in
  `normalized_post`, never in the Stage-1 result the router is passed.

The worker also logs `router_rules_evaluated` **through the same readers the
rules use**, so the log can never report a field the gate did not see. It used to
log raw key lookups, which read `None` for fields Stage 1 emits under another
name — and made three dead rules look like passing ones.

## 4. The gate governs post-level work only

Comment analysis is **not** gated. Every post reaches Stage 2 for its comments,
because the entire comment ensemble — seven HF heads plus the LLM stance pass —
lives there ([STAGE2_LLM.md](STAGE2_LLM.md) §3). When bypassed posts went
straight to the assembler, the majority of posts carried their Stage-1 comment
label alone and the per-comment comparison showed three empty columns with
nothing explaining why.

The decision is carried explicitly rather than inferred:

```text
task_flags.post_level_routed  →  stage2_result.processing.post_level_routed  →  llm_used
```

Inferring `llm_used` from "a `stage2_result` exists" would now report 100% LLM
use on every post, which is why it is carried. `stats:llm_routed` counts only
posts that got post-level tasks, against `stats:total_processed`.

## 5. Comment selection

`select_comments_for_stage2` picks the set **once**, so every Stage-2 voter reads
the same comments — the seven cheap heads, the near-duplicate cache and the LLM
stance pass. Before that, a cap lived in the stance pass alone, so the LLM column
of the per-comment table was empty on comments all seven heads had voted on.

It runs in three steps:

1. **Eligibility.** Textless kinds are excluded outright —
   `TEXTLESS_KINDS = {emoji, filtered, link}`. There is nothing for a model to
   read, and a 900-like ❤️ must not take a slot it cannot use. Comments below
   `ROUTER_COMMENT_MIN_WORDS` word tokens (Latin **or** Bengali, counted by
   regex) are marked `stage2_skip_reason: below_min_words`.
2. **Deduplication.** Candidates are sorted by `likes` descending (ties broken by
   original index, so the choice is reproducible), then collapsed on a normalised
   text key from [`libs/comment_groups.py`](../src/defense/libs/comment_groups.py).
   The most-liked occurrence survives; repeats get
   `stage2_skip_reason: duplicate`, `is_duplicate: true` and `duplicate_of: <id>`.
   On a corpus where the same "মাশাল্লাহ" appears dozens of times in one thread,
   this is a real saving that costs no coverage.
3. **Top-N.** `ROUTER_COMMENT_TOP_N` keeps that many of the survivors; the rest
   get `stage2_skip_reason: below_top_n`.

Nothing is ever **removed** from the payload. Comments outside the cut are
marked, not filtered, so all three stores receive the whole thread and the result
reports how many went unread (`ensemble.not_analysed`) rather than implying full
coverage.

Per post, `comment_analysis.stage2_selection` records:

| Field | Meaning |
| ----- | ------- |
| `strategy` | `top_reactions` |
| `limit` | The cap in force; `0` = uncapped |
| `total` / `eligible` / `selected` / `skipped` | Thread size, post-filter survivors, chosen, cut |
| `duplicates` | How many were collapsed as repeats |
| `cutoff_likes` / `top_likes` | The reaction count of the lowest and highest selected comment, so the cut is inspectable without re-sorting the thread |

### The default is full coverage

`ROUTER_COMMENT_TOP_N=0` and `ROUTER_COMMENT_MIN_WORDS=0` — **every unique
comment with text**. At the default the post-level breakdown and the per-comment
table describe the same comments. With a positive cap they diverge, and the
output says so: `ensemble.analysed` / `not_analysed`, and `uncertain` on every
comment no model read.

Cap it when a run is too slow, not by default: seven CPU heads cost ≈0.92 s per
comment (≈44 min on the corpus's 2,857-comment thread). A positive cap is also
**engagement-ordered selection on top of an already engagement-ordered sample**,
which deepens the sampling bias rather than fixing it —
[evaluation.md](evaluation.md) §1.

## 6. Task flags — not paying twice

`get_task_flags` decides *which* post-level tasks run, for a routed post:

| Flag | True when |
| ---- | --------- |
| `want_summary` | Stage 1's summary is missing or shorter than 6 characters — **or** the caller explicitly asks. An explicit `False` always wins. |
| `want_post_type` | Stage 1 produced no `post_type`, or one below `ROUTER_POST_TYPE_CONFIDENCE_THRESHOLD`, or the caller asks. Mirrors gate 2. |
| `want_insight` | Fewer than 2 topics, or the caller asks. |
| `target_lang` | Explicit option, else Stage 1's detected language. `None` = auto. |

`want_summary` defaulting to `True` meant every routed post paid for a second
summary that overwrote an identical first one.

## 7. The routing rate — how to read it

Measured on the 43-post captioned corpus, cold cache, 25 comments per batch
(`python -m eval.measure_routing_rate`):

| | Keyword stub | Stage-1 LLM (shipped) |
| --- | --- | --- |
| Posts routed for post-level work | 32 / 43 (**74%**) | 7 / 43 (**16%**) |
| Post-level LLM calls | 124 (15%) | 22 (4%) |
| Comment-level LLM calls | 689 (85%) | 507 (96%) |
| Total calls per run | 813 | 529 |
| Share of calls the gate governs | 55% | **30%** |

Two conclusions, both of which constrain how the number may be quoted:

- **The rate measures Stage-1 quality, not cost efficiency.** A Stage 1 that
  types a post confidently bypasses post-level Stage-2 work, so the rate *falls
  as Stage 1 improves* — same code, two engines, two rates. A **rising** rate is
  a quality regression to alert on, not a cost regression.
- **The better Stage 1 gets, the less the gate governs.** Comment labelling runs
  for every post, so improving Stage 1 shrinks the gate's share of spend
  (55% → 30%) without shrinking the bill. What sets the bill is how many comments
  exist. The single-digit-% target that used to appear here is **retired**; it was
  never measured and it treated a quality signal as a cost dial.

In stub mode the gate cannot gate at all: the keyword stub's toxicity never
exceeds 0.2, so rule 5 is inert, and confidence is a hash-derived constant.
Both facts are logged rather than assumed.

## 8. Configuration

| Env var | Default | Effect |
| ------- | ------- | ------ |
| `ROUTER_CONFIDENCE_THRESHOLD` | `0.8` | Gate 1 |
| `ROUTER_POST_TYPE_CONFIDENCE_THRESHOLD` | `0.8` | Gates 2 and `want_post_type` |
| `ROUTER_TOXICITY_THRESHOLD` | `0.7` | Gate 5 |
| `ROUTER_LONG_TEXT_CHARS` | `1000` | Gate 6 |
| `ROUTER_SUMMARY_ROUTES` | `false` | Gate 3 — `true` routes everything |
| `ROUTER_COMMENT_TOP_N` | `0` | Comment cap; `0` = every unique comment with text |
| `ROUTER_COMMENT_MIN_WORDS` | `0` | Minimum word tokens for eligibility |
| `ROUTER_STREAM` / `ROUTER_GROUP` / `ROUTER_CONSUMER` | see [PIPELINE.md](PIPELINE.md) | Stream identity |
| `ROUTER_MAX_RETRIES` | `3` | Before dead-lettering to `router:queue:dlq` |
| `ROUTER_BATCH_SIZE` | — | Messages per poll |

Every knob is also documented with its failure mode in
[env.example.md](env.example.md).

## 9. Evidence class

| Claim | State |
| ----- | ----- |
| 16% routed on the shipped Stage-1 LLM, 74% under the keyword stub | ✅ **Measured** — `eval/measure_routing_rate.py` |
| Post-vs-comment call split (85% / 96% comment-level) | ✅ **Measured** |
| All six gates read the field they intend to | ✅ Measured — named readers + `router_rules_evaluated` |
| The bypass leg validates against the output schema | ✅ Measured — that branch had never once executed before the audit |
| Deduplicated / min-words comment selection | 🟡 Works, unmeasured — no figure for how many calls dedup saves per corpus |
| Threshold sweep produces a cost curve | ✅ Measured (cost axis only — the accuracy axis needs gold labels) |
| Gate *accuracy* — do the routed posts deserve it? | 📋 **Unmeasured.** Needs the gold set ([evaluation.md](evaluation.md) §8) |

Cross-references: [PIPELINE.md](PIPELINE.md) · [STAGE1_NLP.md](STAGE1_NLP.md)
(what the gates read) · [STAGE2_LLM.md](STAGE2_LLM.md) (what routing buys) ·
[architecture.md](architecture.md) §5 · [evaluation.md](evaluation.md) §4 ·
[cost_estimation.md](cost_estimation.md).

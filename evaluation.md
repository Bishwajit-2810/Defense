# Evaluation Plan — How We Evaluate the Smart Layer

How we measure whether **this system** is good enough to ship and stays good over
time. It is written against our actual components and data: the **post-with-details**
input ([data_contract.md](data_contract.md)), the pipeline
([architecture.md](architecture.md) §3, §6), the model choices
([models.md](models.md)), and the agentic insight layer
([architecture.md](architecture.md) §11). Evaluation is **per-task** (is each signal
correct?), **system-level** (is it fast/cheap/selective?), and **agent-level** (are
reports grounded and useful?). Wherever a model can be swapped or fine-tuned, an
**eval gate** decides if it ships ([plan.md](plan.md)).

---

## 0. What "good" means here (the things we actually score)

Our output schema ([architecture.md](architecture.md) §6) is the contract we
evaluate against, field by field:

- **Text sentiment** (post caption + every comment) — recomputed by us.
- **Image sentiment** (visual model on the photo) — *currently unscoreable: no
  image bytes are reachable, so `vision_status` is never `ok` (§5.2)* — and the
  **fused** `overall_sentiment`/`sentiment_score`, which today is a text-only
  fusion because weights renormalise over the terms that carry a real verdict.
- **Comment-thread analysis** — `comment_analysis.sentiment_breakdown` over the
  **stored sample**, that `coverage` is reported honestly (clamped, with
  `coverage_anomaly` for upstream mismatches), and that `provenance` states how
  many of those labels a model actually produced.
- **`post_summary`** — grounded on caption (+ OCR/image when available), in the
  post's own language, and **not silently truncated** (`post_summary_truncated`).
- Supporting signals — language/Banglish detection, emotion, topics, intents, NER /
  brand mentions, toxicity/hate, **our OCR** text.
- **Agentic insight layer** — analyst answers and generated reports.
- **System properties** — LLM-routing rate **and the post-vs-comment call split**
  (the routing rate alone is not the cost story), latency/throughput, cost-per-1k
  **per backend and model**, cache hit rate, and **`local`⇄`groq` parity**.

Two data facts shape the whole plan (from [data_contract.md](data_contract.md)):

1. **Bangla + Banglish is the real traffic** (the sample is 100% Bangla Facebook,
   comments heavily romanized/code-mixed). Every metric is reported **per language
   bucket: `bn` / `en` / `banglish`**, because off-the-shelf models fail most on
   Banglish and an aggregate number hides that.
2. **We see only a stored sample of comments** (`storedCommentRows` of
   `commentCount`). Thread-level metrics are computed **on the stored sample**, and
   we explicitly score whether `coverage` is reported correctly — never claim
   full-thread accuracy.

---

## 1. Evaluation datasets

| Set                      | Built from                                                                                                                                                                                                            | Used for                                                          |
| ------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------- |
| **Gold eval set**        | A stratified, **human-labeled** sample drawn from [posts_text_only.json](posts_text_only.json) (43 captioned posts, 8,965 comments — see the sampling-frame note below) and ongoing pulls — stratified by `postType` (TEXT/PHOTO_TEXT), language bucket, and comment `kind` | Per-task accuracy, the ship/no-ship gate                          |
| **Banglish-heavy slice** | Comments + captions that are romanized/code-mixed                                                                                                                                                                     | The hardest, highest-priority bucket — gets first labeling effort |
| **Comment gold set**     | Human-labeled sentiment on a sample of **embedded** comments per post                                                                                                                                                 | Per-comment sentiment + thread `sentiment_breakdown`              |
| **Image gold set**       | *Blocked* — no image bytes are reachable (§5.2), so there is nothing to label. Restore when the 69 objects are uploaded.                                                                                               | `image_sentiment` and OCR (CER/WER)                               |
| **Summary gold set**     | Posts with reference key-points + "must-not-say" (hallucination probes)                                                                                                                                               | Summary faithfulness/coverage/grounding                           |
| **Agent QA set**         | Analyst questions with reference answers + the citing posts/comments                                                                                                                                                  | Insight-agent groundedness, citation accuracy, relevance          |
| **Regression set**       | A frozen batch replayable from Kafka                                                                                                                                                                                  | Catch regressions on every model/prompt/backend change            |

### The sampling frame — write this down before labelling anything

Required before a single label is collected, because it determines what the
labels can support (PROJECT_ASSESSMENT §5.4, §7.3):

- **Corpus coverage is ~3.75%**: 10,272 stored comments against 274,126 reported
  by the platform. The per-post median is 26.7% and 12 posts are below 10%. The
  per-post number is what the API and dashboard show; the aggregate is now also
  reported (`corpus_coverage` on `/v1/analysis/overview`).
- **The sample is not random.** Stored comments are the platform's returned set,
  which is **engagement-ordered**: 64% have zero likes, the mean is 4.0, the max
  946. Aggregating sentiment over an engagement-biased 3.75% sample and calling
  it "the thread's sentiment" is the **single biggest validity threat** to any
  paper built on this corpus.
- **Therefore, claims must be scoped to what the data supports.** Defensible:
  *"the sentiment of the most-engaged N comments per post."* Not defensible:
  *"public sentiment on this post."* Report coverage-weighted intervals, or
  restrict the claim — and state which, once, in writing.
- **Five posts store more comments than the platform reports** (up to 112 against
  42). Coverage is clamped to 1.0 and the discrepancy surfaces as
  `coverage_anomaly`; exclude those posts from any coverage-weighted statistic or
  say why you did not.
- **The working corpus excludes 7 image-only posts** (43 of 50 remain, 8,965 of
  10,272 comments). They have null captions and no reachable image, so there is
  nothing to label. Regenerate with `python -m eval.make_text_corpus`.

**Labeling discipline.** Written annotation guidelines per task; **≥2 annotators**
on a subset with **inter-annotator agreement** (Cohen's κ) reported — if humans
can't agree, the metric is noise. Banglish gets a transliteration-aware guideline.
Stratify by comment `kind` as well as language: `emoji` comments carry a
sentiment but no text, so they need a different guideline (or explicit exclusion)
from `substantive` ones.

**Weak/free signals (comparison, _not_ ground truth):**

- **`reaction_breakdown`** (LIKE/LOVE/HAHA/WOW/SAD/ANGRY/CARE) — a crowd emotion
  signal. We measure **agreement** between our sentiment/emotion and the reaction
  mix (e.g. SAD/ANGRY-dominant ⇒ expect negative). Disagreement is a flag to review,
  not an error by itself.
- **Upstream `baseline_sentiment`** — we report **our-vs-baseline** divergence to
  catch both upstream miscalibration and our own regressions; the gold labels, not
  the baseline, decide who's right.

---

## 2. Per-task metrics & targets

Targets are **launch bars** (MVP), tracked per language bucket; tighten over time.

| Task                             | Metric                                                               | Launch target (bn / banglish)                  |
| -------------------------------- | -------------------------------------------------------------------- | ---------------------------------------------- |
| Language + Banglish detection    | Accuracy; Banglish recall                                            | ≥0.95 acc; Banglish recall ≥0.90               |
| **Text sentiment** (post)        | Macro-F1 (pos/neg/neu); MAE on `sentiment_score`                     | Macro-F1 ≥0.75; MAE ≤0.20                      |
| **Comment sentiment**            | Macro-F1 on the comment gold set                                     | Macro-F1 ≥0.70 (lower bar — short, noisy text) |
| **Thread `sentiment_breakdown`** | breakdown error (L1 on pos/neg/neu proportions) on the stored sample | ≤0.10                                          |
| **Image sentiment**              | Accuracy on image gold set                                           | ≥0.70                                          |
| **Fused `overall_sentiment`**    | Macro-F1; **agreement with `reaction_breakdown`**                    | Macro-F1 ≥0.78; reaction-agreement ≥0.70       |
| Emotion                          | Macro-F1; reaction cross-check                                       | Macro-F1 ≥0.60                                 |
| **`post_type`** (semantic)       | Macro-F1 (complaint/promotion/news/opinion/…)                        | ≥0.70                                          |
| Topics / intents                 | Micro-F1 (multi-label)                                               | ≥0.65                                          |
| NER / brand mentions             | Entity-level F1                                                      | ≥0.70                                          |
| Toxicity / hate                  | F1 **and** PR-AUC (class-imbalanced)                                 | PR-AUC ≥0.75; high-recall operating point      |
| **OCR** (our own)                | CER / WER (bn + en)                                                  | CER ≤0.15 on legible images                    |
| **`post_summary`**               | Faithfulness, coverage, language-correctness (below)                 | Faithfulness ≥0.90; correct-language =100%     |

**Calibration.** Beyond F1 we check **confidence calibration** (reliability curve /
ECE) on the classifiers — because the **Router** ([architecture.md](architecture.md)
§5) gates Stage-2 on confidence, a miscalibrated model either wastes LLM budget or
ships low-quality results. Confidence thresholds are tuned on the validation set,
not guessed.

---

## 3. Summary evaluation (the LLM/VLM output)

`post_summary` is generative, so it gets a dedicated rubric — scored by an **LLM
judge** (a strong model, run offline) **and** human spot-checks, never self-scored
by the producing model:

- **Faithfulness / no hallucination** — every claim is supported by the caption,
  OCR, image, or comments. We include **"must-not-say" probes** in the gold set.
- **Image grounding** — for photo posts, does the summary use what's _only_ in the
  image (verified against `post_summary_grounding` listing `image`)? A photo-only
  `null`-caption post must still get a meaningful summary.
- **Coverage** — captures the key point(s) and the dominant comment stance.
- **Language correctness** — summary is in the post's own language
  (`post_summary_lang`); Banglish folded to the dominant language. This is a
  **hard pass/fail**.
- **Conciseness / no filler.**

Comment-thread `themes` are scored the same way (grounded in the stored comments,
weighted toward high-`likes` comments).

---

## 4. System-level evaluation

The hybrid design's promises are themselves tested ([architecture.md](architecture.md)
§5, [cost_estimation.md](cost_estimation.md)):

- **LLM-routing rate** — % of posts that reach Stage-2. **Measured: 16% shipped
  (`gemma3:4b`), 74% keyword stub**, on the 43-post corpus
  (`python -m eval.measure_routing_rate`).
  ~~Target single digits %.~~ **This target is retired.** It was never measured,
  and it treats the rate as a cost dial when it is really a *quality* signal: a
  Stage 1 that types a post confidently bypasses Stage 2, so the rate falls as
  Stage 1 improves. A rising rate means Stage 1 got worse (or the corpus got
  harder) — alert on it as a **quality** regression, and read it alongside the
  call split below rather than on its own.
- **Post-level vs comment-level call split** — the number that actually bounds
  cost. Measured: **85% of calls are comment-level** under the keyword stub,
  **96%** under the shipped Stage-1 LLM; the routing gate governs **55%** and
  **30%** respectively. Alert on calls-per-thread, not on the routing rate.
- **Comment LLM coverage** — share of non-emoji comments that received an LLM
  label. Should be ~100% with the caps at 0; anything less means a batch failed
  and its comments fell back to the Stage-1 heuristic. Readable from
  `comment_analysis.provenance.inferred_share`.
- **Label provenance** — `provenance.inferred` vs `provenance.heuristic` per
  post. A sentiment chart that cannot say where its labels came from is the §4
  failure in miniature; this is the assertion that keeps it honest.
- **Cost-per-1k threads** — on both backends, **priced separately**. `local` is
  0.0/token by definition; Groq is per-model. `GET /v1/usage` reports
  `tokens_by_backend_model` and `cost_by_backend_model`; a single blended rate is
  wrong for both backends in opposite directions.
- **Latency / throughput** — per-stage p50/p95; end-to-end per batch (1k / 10k).
- **Cache & dedup hit rates** — dedup on repetitive feeds, LLM-response cache.
  Note the cache key includes the **resolved model id**, so a model change must
  show as a miss; if it does not, the benchmark is comparing cached answers.
- **Truncation rate** — share of summaries that hit the token ceiling even after
  auto-continuation (`post_summary_truncated`). Expected to be
  **language-correlated**: Bangla costs far more tokens per character than
  English, so this metric belongs in the per-language bucket table, not the
  aggregate.
- **Coverage honesty** — assert `comment_analysis.analyzed == storedCommentRows`
  and `coverage` reflects `commentCount`. Coverage is **clamped to 1.0**; when
  the stored rows exceed the reported count the excess appears as
  `coverage_anomaly`, never as coverage above 100% (5 posts in the corpus do
  this, up to 112 stored against 42 reported). A correctness check, not a model
  metric.
- **Vision status** — `image_analysis.vision_status`. Only `ok` licenses a claim
  about image sentiment; every other value (`stub`, `fetch_failed`,
  `model_unavailable`, `model_failed`) is an absence. Currently **never `ok`** —
  no image bytes are reachable (§5.2), which is why post sentiment is a text
  measurement today.

### `local` ⇄ `groq` backend parity

Run the **same gold/regression sets through both backends** and compare summary
quality (judge scores), latency, and cost. A backend switch must **not** regress
quality beyond a small tolerance; if `groq` model IDs change, re-run parity before
adopting. Privacy-locked tenants are validated to **never egress** (eval asserts
their runs stayed `local`).

---

## 5. Agentic insight layer evaluation

Agents ([architecture.md](architecture.md) §11) are evaluated separately from the
per-post pipeline, on the **Agent QA set**:

- **Groundedness / citation accuracy** — does each cited post/comment actually
  support the claim? (Retrieval-augmented answers are checked claim-by-claim.)
- **Faithfulness / hallucination rate** — no invented facts or numbers; trend
  numbers must match what `analytics-mcp` actually returns.
- **Answer relevance & completeness** — does it answer the analyst's question?
- **Tool-use correctness** — did the agent call the right MCP tools with sane
  arguments (no needless calls)? Measured from the recorded `tools_used`.
- **Coverage deep-dive trigger** — precision/recall of "should we pull more
  comments?" against labeled low-coverage/viral cases.
- **Budget adherence** — stays within per-run tool-call/token caps (a cost test).

Method: **LLM-as-judge** for groundedness/relevance at scale + **human review** on a
sample; every agent run is auditable (backend, model, tools, tokens) so failures are
traceable.

---

## 6. Ship gates, regression & drift

- **Ship gate.** No model/prompt/backend change ships unless it **beats the current
  one on the per-language gold set** and does **not** raise the LLM-routing rate or
  hallucination rate ([models.md](models.md) §4). Both quality **and** cost must hold.
- **Regression CI.** On every change, replay the frozen **regression set** from
  Kafka and diff metrics; block on regressions. Record `model_versions` per result
  for reproducibility.
- **Live drift monitoring** (proxies, no labels needed —
  [infrastructure.md](infrastructure.md)): watch the **confidence distribution**,
  **LLM-routing rate**, **our-sentiment-vs-`reaction_breakdown` agreement**, and
  **our-vs-`baseline_sentiment` divergence** over time; a shift flags content drift
  or model rot and triggers a fresh labeling/eval round.
- **Active-learning loop.** Router-flagged low-confidence and high-disagreement items
  feed the next labeling batch — exactly the examples worth labeling and the ones
  most likely to need fine-tuning ([models.md](models.md) §4).

---

## 7. Tooling & cadence

- **Eval harness** — a CLI/job that runs all gold sets through the pipeline (both
  backends) and emits a per-task, per-language scorecard + the system metrics;
  wired into CI for the regression gate.
- **Dashboards** — eval scorecards alongside the live metrics (LLM-slice,
  cost-per-1k, drift proxies) in Grafana.
- **Cadence** — full gold-set eval before every model/prompt/backend ship; regression
  set on every PR; drift proxies continuously; re-label + expand gold sets each
  iteration, prioritizing the Banglish and comment buckets.

---

## 8. Current state — what exists, and the one thing that does not

**Status 5 August 2026.** Everything above is the plan. This is the gap between
it and the repository.

| | State |
| --- | --- |
| **Structural checks** | [eval/harness.py](eval/harness.py) — `run_input_validation`, `run_platform_detection`, `run_coverage_check`. These pass and are real. |
| **System-property measurement** | [eval/measure_routing_rate.py](eval/measure_routing_rate.py) (routing rate, comment volume, post-vs-comment call split), [eval/make_text_corpus.py](eval/make_text_corpus.py) (corpus + what it dropped), [eval/bakeoff_summary.py](eval/bakeoff_summary.py) (per-model latency, truncation, language fidelity), `GET /v1/usage` (tokens + cost per backend/model). |
| **Accuracy metrics** | **None.** Zero gold labels, zero F1, no scorecard. |

The third row is the decisive gap and the only remaining blocker on the research
side ([PROJECT_ASSESSMENT.md](PROJECT_ASSESSMENT.md) §7.2). Nothing in the
codebase can produce a results table, and a paper *is* its results table.

### 8.1 The shortest path to a first accuracy number

Deliberately smaller than the full plan above — the point is to have **one honest
number with a confidence interval**, not a complete scorecard:

1. **Label ~300 comments** from `posts_text_only.json`, stratified by language
   bucket and comment `kind`. One annotator is enough for a first number; say so,
   and report the small-n caveat.
2. **Score the existing pipeline against them.** Filter to comments whose
   `method` is `model` or `llm` — scoring a `stub` label measures a hash of the
   text, not a model (see `provenance`).
3. **Report macro-F1 per language bucket** with a confidence interval, and state
   the sampling frame from §1 in the same breath.

That converts "we have no idea how well it works" into "here is how well it
works, on this much data, with this much uncertainty" — which is the difference
between a demo and a defensible claim.

### 8.2 If the watchlist is built, validate it separately

[stance_targets.md](stance_targets.md) §7 specifies its own ~150-comment
validation with two metrics that must be reported apart, because they fail
differently: **mention detection** (precision/recall of the alias matcher — the
number that proves or disproves the hard part) and **stance agreement** (of the
correctly-matched mentions, how often the verdict matches a human). Do not fold
either into the general sentiment scorecard; target stance is a separate field
for the same reason it needs a separate metric.

### 8.3 Metrics that became measurable in the 4 August pass

Worth adding to the scorecard before the next round, because the data now exists
and none of it needs labels:

- **`provenance.inferred_share`** per post — how much of a sentiment chart is
  actually model output.
- **`post_summary_truncated`** rate, broken out by language — expected to be
  Bangla-skewed, which is itself a finding.
- **`vision_status`** distribution — currently 100% non-`ok`, which is why post
  sentiment is a text measurement (§5.2).
- **`coverage_anomaly`** count — upstream data-quality events, previously
  absorbed silently as >100% coverage.
- **Post-level vs comment-level call split** — the cost axis any efficiency
  claim has to be plotted against.

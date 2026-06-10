# Evaluation Plan — How We Evaluate the Smart Layer

How we measure whether **this system** is good enough to ship and stays good over
time. It is written against our actual components and data: the **post-with-details**
input ([data_contract.md](data_contract.md)), the multimodal pipeline
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
- **Image sentiment** (visual model on the photo) and the **fused**
  `overall_sentiment`/`sentiment_score`.
- **Comment-thread analysis** — `comment_analysis.sentiment_breakdown` over the
  **stored sample** (and that `coverage` is reported honestly).
- **`post_summary`** — grounded on caption + OCR + image, in the post's own language.
- Supporting signals — language/Banglish detection, emotion, topics, intents, NER /
  brand mentions, toxicity/hate, **our OCR** text.
- **Agentic insight layer** — analyst answers and generated reports.
- **System properties** — LLM-routing rate, latency/throughput, cost-per-1k, cache
  hit rate, and **`local`⇄`groq` parity**.

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
| **Gold eval set**        | A stratified, **human-labeled** sample drawn from [posts_with_details.json](posts_with_details.json) and ongoing pulls — stratified by `postType` (TEXT/PHOTO/PHOTO_TEXT), language bucket, and `null`-caption vs not | Per-task accuracy, the ship/no-ship gate                          |
| **Banglish-heavy slice** | Comments + captions that are romanized/code-mixed                                                                                                                                                                     | The hardest, highest-priority bucket — gets first labeling effort |
| **Comment gold set**     | Human-labeled sentiment on a sample of **embedded** comments per post                                                                                                                                                 | Per-comment sentiment + thread `sentiment_breakdown`              |
| **Image gold set**       | Human-labeled visual sentiment + a transcription for OCR                                                                                                                                                              | `image_sentiment` and OCR (CER/WER)                               |
| **Summary gold set**     | Posts with reference key-points + "must-not-say" (hallucination probes)                                                                                                                                               | Summary faithfulness/coverage/grounding                           |
| **Agent QA set**         | Analyst questions with reference answers + the citing posts/comments                                                                                                                                                  | Insight-agent groundedness, citation accuracy, relevance          |
| **Regression set**       | A frozen batch replayable from Kafka                                                                                                                                                                                  | Catch regressions on every model/prompt/backend change            |

**Labeling discipline.** Written annotation guidelines per task; **≥2 annotators**
on a subset with **inter-annotator agreement** (Cohen's κ) reported — if humans
can't agree, the metric is noise. Banglish gets a transliteration-aware guideline.

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

- **LLM-routing rate** — % of posts that reach Stage-2. Target **single digits %**;
  a rising rate is a cost regression (alert on it).
- **Cost-per-1k threads** — on both backends.
- **Latency / throughput** — per-stage p50/p95; end-to-end per batch (1k / 10k).
- **Cache & dedup hit rates** — dedup on repetitive feeds, LLM-response cache.
- **Coverage honesty** — assert `comment_analysis.analyzed == storedCommentRows`
  and `coverage` reflects `commentCount` (a correctness check, not a model metric).

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

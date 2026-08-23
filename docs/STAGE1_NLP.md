# Stage 1 — Cheap NLP on Every Post and Every Comment

> **Scope.** The stage that runs unconditionally: language detection, caption
> sentiment, emotion, topics, intents, post type, toxicity, NER/keywords,
> embeddings, per-comment labelling, theme extraction, the reaction cross-check
> and sentiment fusion. Plus the two things that make its output trustworthy —
> **engine reporting** and **degradation reporting**.
>
> Code: [`workers/stage1_nlp/`](../src/defense/services/workers/stage1_nlp/worker.py)
> — `worker.py` (stream loop), `text_analyzer.py` (the analyses),
> `comment_analyzer.py` (the thread), `fusion.py` (combination),
> `vision_analyzer.py` (images/OCR), `llm_analyzer.py` (the LLM path),
> `models.py` (the registry). Position in the chain:
> [PIPELINE.md](PIPELINE.md) §1.

---

## 1. Three engines, one contract

Every Stage-1 analysis has up to three implementations, and the output always
says which one ran.

| Engine | When | What it is |
| ------ | ---- | ---------- |
| `stub` | `MODEL_STUB_MODE=true` (default) | Deterministic functions of the text — a hash, keyword lists, script ratios. Reproducible, and **not sentiment**. |
| `models` | Real HF weights installed (`uv sync --extra ml`) | fastText language ID, a language-routed sentiment head, GLiNER NER, KeyBERT keywords, a toxicity pipeline, a SentenceTransformer encoder. |
| `llm` | `STAGE1_LLM=true` (shipped **on**) | One JSON call on the `stage1` role produces sentiment, emotion, topics, intents, post type, toxicity, NER and keywords together. |

`engine` on the result says which path was *intended*.
`processing.degraded_components` says what actually **ran** — every real-mode
component that fell back because its model would not load. A run where every
component degraded still reports `engine: "models"`, so the two are deliberately
reported side by side, and the pair survives the assembler and the API by
assertion (both used to drop it silently).

Any LLM or JSON failure falls back to the stub automatically, so `STAGE1_LLM` is
a preference rather than a hard switch.

## 2. Post-level analyses

| Signal | Stub | Real / LLM | Reported as |
| ------ | ---- | ---------- | ----------- |
| **Language + Banglish** | Script-ratio heuristic | fastText | `language`, `script`, `is_banglish`, **`language_method`** (`fasttext` vs heuristic) |
| **Caption sentiment** | Hash of the text | Language-routed head — BanglaBERT (`bn`) / BanglishBERT (`banglish`) / XLM-R (else) | `sentiment`, `sentiment_score`, `engine`, resolved model |
| **Emotion (7 labels)** | Derived from sentiment + keywords | Transformer pipeline | `emotion`, **`emotion_method`** |
| **Post type** | Keyword heuristic | Zero-shot cosine against embedded prototypes, combined with the LLM verdict | `post_type`, `post_type_confidence` |
| **Topics / intents** | Seed lists | LLM, or KeyBERT-derived | `topics`, `intents` |
| **Toxicity / hate** | Keyword count | Toxicity pipeline | `toxicity_score` — feeds router gate 5 |
| **NER / brand mentions** | Seed lists + longest-token | GLiNER multilingual | `entities`, `brand_mentions` |
| **Keywords** | Longest tokens | KeyBERT | `keywords` |
| **Embedding** | `stub:sha256` unit vector | SentenceTransformer (768-dim) | `analysis_results.embedding` + `embedding_is_stub` |

**The post-type vocabulary is shared, not duplicated.** All nine labels
(`complaint`, `news`, `opinion`, `promotion`, `humor`, `personal`, `political`,
`religious`, `other`) live in [`libs/labels.py`](../src/defense/libs/labels.py),
consumed by Stage 1's classifier, the Stage-2 prompt and the router gate. They
used to be three independent copies. `other` is ordered last because it is the
explicit fallback when nothing clears the similarity floor, never a match.

**The embedding carries its own provenance.** `_embed_with_provenance` returns
`(vector, is_stub)` at the one place the truth is known, and
`EMBEDDING_ALLOW_STUB=false` — which is what `.env` and `.env.example` ship, though
**not** the `Settings` default — makes the encoder *refuse* rather than silently
degrade to a hash — because a hash vector is indistinguishable from
chance in retrieval, measured at recall@10 = 0.1875 where 10 random posts out of
50 scores 0.20 ([RAG_STATE_AND_ROADMAP.md](RAG_STATE_AND_ROADMAP.md) §0).

## 3. Per-comment labelling — full coverage, three paths

Every comment in the stored thread gets a label. The path depends on its
**kind**, classified from the text:

| Kind | Definition | Treatment |
| ---- | ---------- | --------- |
| `emoji` | No word tokens at all | Keeps a sentiment from the emoji itself; **never enters an LLM batch** — there is no text to read. Measured at **2.8%** of the corpus. |
| `link` | Predominantly a URL | Labelled `link`; textless for Stage-2 selection. |
| `short` | 1–2 word tokens | Heuristic or batched model. |
| `substantive` | 3+ word tokens | The full path; eligible for every downstream voter. |

**Provenance is per comment, not per post.** `method` is one of
`llm` / `model` / `stub` / `fast` / `emoji` / `link` / `failed`, and the post
carries a `provenance` block with `inferred_share`. Without this,
`method_breakdown` once claimed 8,513 model inferences in runs where zero models
loaded — `stub` is a hash of the text, which is reproducible and is not
sentiment.

**Stage-1 comment LLM labelling is off by default** (`STAGE1_LLM_COMMENTS=false`).
Stage 2 labels comments with a bigger model plus seven classifiers, so turning
this on re-does the work with the weaker one and is the slowest step in the
pipeline. It is *not* a coverage gap: Stage 1's heuristic covers 100% regardless,
and Stage 2's ensemble is what produces the published label. `STAGE1_LLM_COMMENT_MAX=0`
means no cap when it *is* on — the old default of 60 meant ~29% of comments got an
LLM label while the output described itself as full coverage.

**Per-comment emotion is the free heuristic even in real mode.** `emotion_method`
says so on every row; only Stage-2-relabelled comments carry a model emotion.
That distinction used to be invisible.

**Batched inference.** Comments are grouped by resolved model and run one
forward pass per group instead of one per comment — transformer inference is
10–30× faster batched. The code is complete; the **speedup is unverified**
because no real weights are installed on the measurement box.

## 4. Thread aggregation

`analyze_comments` produces the `comment_analysis` block:

- **`sentiment_breakdown`** — positive / negative / neutral over the stored
  sample, with `coverage` reported honestly (clamped to 1.0, with
  `coverage_anomaly` when stored rows exceed the upstream count — 5 posts in the
  corpus do this, up to 112 stored against 42 reported).
- **`themes`** — cross-comment keywords weighted **sub-linearly** by likes. Raw
  like-weighting made one 946-like comment outweigh 946 ordinary ones, so
  "themes" was really the top comment's keyword list.
- **Representative comments** — the highest-liked comment per sentiment class, up
  to three.
- **Target stances** — deterministic watchlist matching, which is free string
  work and therefore runs even on posts the router bypasses
  ([stance_targets.md](stance_targets.md)).

## 5. Sentiment fusion

`fusion.py` combines the available signals into `overall_sentiment` /
`sentiment_score`, on Golden rule 8's weights:

| Case | Weights |
| ---- | ------- |
| Text-only post | text × 1.0 |
| Image post with caption | text × 0.6 + image × 0.4 |
| Image post, null caption | image × 0.7 + OCR-text × 0.3 |

**Weights renormalise over the terms that carry a real model verdict**, and this
is the fix that matters most in the file. Applied unconditionally they silently
shrank real signals:

- a null-caption post scored `0.7 × image + 0.3 × 0.0` — the OCR term was always
  zero because the worker analysed the (null) caption rather than the OCR text —
  so every image-only post's score was multiplied by 0.7, enough to push a weak
  negative (−0.15 → −0.105) across the ±0.1 neutral boundary and flip its label;
- wherever the image produced no verdict, a captioned post scored
  `0.6 × text + 0.4 × 0.0`, dragging a real text signal 40% toward neutral.

A term counts only when it is a genuine model output — for images that means
`status == "ok"`, not merely "a dict was returned". When no term qualifies the
result is 0.0/neutral, which is the honest answer for a post nothing could be
measured on.

**Reaction cross-check.** `reactionBreakdown` (LIKE/LOVE/HAHA/WOW/SAD/ANGRY/CARE)
nudges a neutral-or-positive verdict negative when SAD+ANGRY exceed 40% of total
reactions. A free crowd prior, applied *after* renormalisation.

## 6. The image path — implemented, unexercised

`vision_analyzer.py` implements SigLIP/CLIP zero-shot image sentiment and
Tesseract (bn+eng) OCR. Every result carries a `status`, and only one value
licenses a claim:

| `vision_status` | Meaning |
| --------------- | ------- |
| `ok` | A model actually looked at the image — the **only** usable status |
| `stub` | Stub mode; no model loaded |
| `fetch_failed` | The bytes could not be retrieved |
| `model_unavailable` | Bytes fetched, no CLIP/SigLIP installed |
| `model_failed` | Bytes fetched, the model raised |

**It is never `ok` in any runnable configuration.** The dataset's 69 `photoUrls`
are relative object-storage keys and the objects are not in MinIO, so no image
bytes are reachable. A failed fetch now reports as a failure rather than as a
neutral verdict. OCR is additionally off by default
(`STAGE1_OCR_SENTIMENT=false`). Post sentiment is therefore a **text**
measurement today — see [evaluation.md](evaluation.md) §0 and
[data_contract.md](data_contract.md) §4.

## 7. Stage 1 also writes the summary

Since the router's gate 3 was turned off, Stage 1 writes a post summary for
**every** post and hands it to the assembler. Stage 2 re-summarises only when the
caller asks for the better model, or when Stage 1's came back empty (its LLM was
off, or the call failed). Before that, every routed post paid for a second
summary that overwrote an identical first one. `post_summary_lang` and
`post_summary_grounding` are recorded either way.

## 8. Configuration

| Env var | Default | Effect |
| ------- | ------- | ------ |
| `MODEL_STUB_MODE` | `true` | No heavy weights downloaded or loaded; drives the HF offline policy |
| `STAGE1_LLM` | `true` | Stage-1 analyses come from the `stage1` LLM role |
| `STAGE1_LLM_COMMENTS` | `false` | Whether Stage 1 *also* LLM-labels comments |
| `STAGE1_LLM_COMMENT_MAX` | `0` | Cap when it is on; `0` = every non-emoji comment |
| `STAGE1_LLM_BATCH` / `_CONCURRENCY` / `_BATCH_RETRIES` | — | Comment batching for that path |
| `SENTIMENT_MODEL` and the per-language slots | see [models.md](models.md) | Which head each language bucket routes to |
| `EMBEDDING_MODEL` / `EMBEDDING_DIM` | `paraphrase-multilingual-mpnet-base-v2` / `768` | The encoder; `EMBEDDING_DIM` must match the pgvector column |
| `EMBEDDING_STUB_MODE` | Settings default **unset (`None`)** — falls through to `MODEL_STUB_MODE`; `.env` ships `false` | Overrides `MODEL_STUB_MODE` for the sentence encoder alone, in either direction |
| `EMBEDDING_ALLOW_STUB` | Settings default **`true`**; `.env` and `.env.example` both ship **`false`** | At `false` the encoder *refuses* rather than silently degrading to `stub:sha256`. The permissive Settings default is what applies when the key is absent — so an operator who deletes it from `.env` gets silent degradation back |
| `STAGE1_OCR_SENTIMENT` | `false` | Run sentiment over OCR text |
| `COMMENT_LAUGH_SENTIMENT` | `negative` | How 🤣😂😆😹 are scored — one switch, both tables |

Full list with failure modes: [env.example.md](env.example.md).

## 9. Evidence class

| Claim | State |
| ----- | ----- |
| Language + Banglish detection runs and reports its method | ✅ Measured |
| Caption sentiment (stub + LLM engines) | ✅ Measured · real small models 📋 not yet run |
| Per-comment full coverage; comment kinds at 2.8% emoji | ✅ Measured |
| Label provenance / `inferred_share` | ✅ Measured |
| Sub-linear theme weighting | ✅ Measured |
| Fusion renormalisation + reaction nudge | ✅ Measured — `tests/test_vision_fusion.py` |
| Degradation reporting survives to the API | ✅ Measured end to end |
| Batched inference speedup | 🟡 Code complete, **unverified** — no real weights installed |
| NER / brand mentions / keywords quality | 🟡 Works, unmeasured |
| Toxicity gate | ✅ Measured **as inert in stub mode** (never exceeds 0.2) |
| Image sentiment / OCR | ⚠️ **Unexercised** — no image bytes reachable |
| Sentiment **accuracy** of any engine | 📋 **Unmeasured** — needs the gold set ([evaluation.md](evaluation.md) §8) |

Cross-references: [PIPELINE.md](PIPELINE.md) · [ROUTER.md](ROUTER.md) (what the
gates read from here) · [STAGE2_LLM.md](STAGE2_LLM.md) ·
[models.md](models.md) · [stance_targets.md](stance_targets.md) ·
[data_contract.md](data_contract.md) §4.

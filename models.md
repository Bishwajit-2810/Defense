# AI Model Selection, RAG & Fine-Tuning

Model recommendations per task, the Bangla/Banglish/English fine-tuning strategy,
and the RAG evaluation for the smart layer in [what.txt](what.txt). All choices
favor open-source, GPU-efficient models with genuine Bangla support, and are
**100% self-hosted — no external/paid LLM API is used anywhere in the design**
(the owner's hard "no api/paid models" constraint).

The guiding rule from [architecture.md](architecture.md): **small models do the
bulk work; the LLM is selective.** So the table below is mostly _small_ models,
plus **two self-hosted local LLMs** for the selective stage (see §2).

**The input is a post + its comment thread, and the content is heavily
"Banglish"** (romanized Bangla, often mixed with English in one sentence, e.g.
"Green garden e vat 25 taka baire 10 taka"). Every model choice below is judged on
how well it handles **bn + en + code-mixed Banglish**, because that — not clean
Bangla or clean English — is the real traffic (see [examples.md](examples.md)).
Small models run over the **post and each comment**; the LLM summarizes the
**thread** in the post's original language.

---

## 1. Model recommendations per task

| Task                                | Recommended model(s)                                                       | Bangla | English | Notes                                                                  |
| ----------------------------------- | -------------------------------------------------------------------------- | ------ | ------- | ---------------------------------------------------------------------- |
| **Language + Banglish detection**   | `fastText lid.176` + CLD3 + transliteration heuristic                      | ✅      | ✅       | <1 ms/item; flags `banglish` (romanized bn) → multilingual path        |
| **Sentiment** (post + per comment)  | `XLM-RoBERTa`/`mBERT` fine-tuned; BanglaBERT for bn                        | ✅      | ✅       | Run on post + every comment; aggregate into `sentiment_breakdown`      |
| **Emotion**                         | XLM-R fine-tuned (joy/anger/sadness/fear/…); GoEmotions heads for en       | ✅      | ✅       | Shares encoder with sentiment to save GPU                              |
| **Topic classification**            | XLM-R / embedding + classifier head; or zero-shot via small NLI model      | ✅      | ✅       | Use embeddings + lightweight classifier; reduces per-label models      |
| **Intent**                          | XLM-R fine-tuned (inform/promote/complain/request/…)                       | ✅      | ✅       | Per comment too (price/availability/location inquiries)                |
| **Toxicity / hate / offensive**     | `XLM-R`/`mBERT` fine-tuned; Detoxify (en) + Bangla hate datasets           | ✅      | ✅       | Bangla hate-speech corpora exist (e.g. Bengali Hate Speech); fine-tune |
| **NER (person/org/location/brand)** | `GLiNER` (multilingual, zero/few-shot), `spaCy` (en), BanglaBERT-NER (bn)  | ✅      | ✅       | GLiNER gives flexible entity types without per-type models             |
| **Embeddings**                      | `BAAI/bge-m3` (multilingual, incl. Bangla) or `intfloat/multilingual-e5`   | ✅      | ✅       | Powers dedup, comment clustering, semantic search, RAG                 |
| **Summarization** (thread)          | **Local LLM-A** (small thread) / **LLM-B** (large/clustered) — see §2      | ✅      | ✅       | Selective; summary in the post's original language                     |
| **Insight / report generation**     | **Local LLM-B** + RAG (see §2)                                             | ✅      | ✅       | Cluster summaries → corpus-level insight                               |
| **Keyword extraction**              | KeyBERT (on embeddings) / YAKE                                             | ✅      | ✅       | Cheap, no extra GPU model                                              |

### Bangla-specific resources worth using/fine-tuning on

- **BanglaBERT** (csebuetnlp) — strong monolingual Bangla encoder.
- **XLM-RoBERTa / mBERT** — multilingual encoders that handle Bangla + English
  _and_ code-mixed "Banglish" reasonably, ideal because social posts mix scripts.
- **bge-m3 / multilingual-e5** — multilingual embeddings with Bangla coverage.
- **Banglish (romanized Bangla) is the dominant comment style**, not an edge case
  (see [examples.md](examples.md): "Green garden e vat 25 taka baire 10 taka").
  Off-the-shelf models are weakest here, so it gets first priority in the labeled
  eval set and any fine-tuning. An optional **transliteration normalizer**
  (Banglish → Bangla script) can be added before classification if accuracy
  requires it. The owner plans to **fine-tune** as needed (§4); the platform's job
  is to surface the right examples to fine-tune on.

**Why a shared multilingual encoder (XLM-R family) for most classifiers:** one
encoder pass can feed multiple lightweight task heads (sentiment, emotion,
intent, topic), cutting GPU cost vs. running a separate full model per task. It
also handles code-mixed Bangla-English in a single model — critical for real FB/IG content.

---

## 2. The selective LLMs — two local models, no API

The selective Stage-2 work is split across **two self-hosted local LLMs**, both
served on **vLLM**. There is **no external/paid API**: everything runs on our own
GPUs, so post content never leaves the cluster and there is no per-token bill.

| Role                                | Model                                                                            | Serves                                                                                | Why                                                                                              |
| ----------------------------------- | -------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------ |
| **LLM-A — fast / high-throughput**  | `Qwen2.5-7B-Instruct` (or `Llama-3.1-8B-Instruct`), AWQ/GPTQ quantized           | Per-post selective refinement, hardest classification, short single-post summaries    | Strong multilingual incl. Bangla, fits one mid (24 GB) GPU, continuous batching → high post rate |
| **LLM-B — large / high-quality**    | `Qwen2.5-32B-Instruct` (or `Qwen2.5-14B-Instruct` at smaller scale), quantized   | Cluster summarization, corpus insight, grounded report generation (RAG)               | Higher reasoning/quality for the low-volume, quality-critical generation work                    |

Why two and not one: the two jobs have opposite profiles. Per-post refinement is
**high-volume, low-difficulty** (favor a small fast model); cluster/report
generation is **low-volume, high-quality** (favor a larger model). Splitting them
lets each run on right-sized GPUs and scale independently, instead of paying
32B-class cost for every per-post call or accepting 7B-class quality on reports.
At MVP scale the two can be **time-sliced on a single GPU**, or LLM-B can be
dropped and LLM-A used for both until volume justifies the second model.

Both expose an **OpenAI-compatible HTTP API** (the vLLM wire protocol — this is a
local server, not a paid service) with structured JSON output mode to minimize
tokens. See [cost_estimation.md](cost_estimation.md) for per-batch LLM economics
and [infrastructure.md](infrastructure.md) for GPU sizing.

> No external API, by design. If LLM-B is saturated the router degrades to LLM-A
> (lower quality, still local) or returns Stage-1-only results — see
> [architecture.md](architecture.md) §8. Capacity is added by scaling GPUs, never
> by sending data to a third-party API.

---

## 3. Model serving architecture

```text
   NLP fleet (Stage 1)                         LLMs (Stage 2) — both local
 ┌───────────────────────┐                 ┌────────────────────────────┐
 │ Triton / ONNX Runtime │                 │ vLLM: LLM-A (7B/8B, fast)  │
 │  + CTranslate2        │                 │ vLLM: LLM-B (14B/32B, qual)│
 │  dynamic batching     │                 │  continuous batching,      │
 │  many small models    │                 │  quantized, paged-attn KV  │
 └──────────┬────────────┘                 └───────────┬────────────────┘
            │ gRPC/HTTP                                 │ HTTP (OpenAI-compat, local)
   Stage-1 worker pulls batch                  Stage-2 worker pulls from LLM
   from queue, calls Triton                    sub-queue, calls LLM-A or LLM-B
```

- **NLP models** → **Triton Inference Server** (or ONNX Runtime / CTranslate2)
  with dynamic batching and INT8/FP16 quantization. Multiple models share GPUs;
  Triton handles concurrent model execution and batching.
- **LLMs** → **two vLLM deployments** (paged attention + continuous batching),
  **LLM-A** (fast, per-post) and **LLM-B** (large, cluster/report), each exposing
  a local OpenAI-compatible API; quantized weights (AWQ/GPTQ) to fit GPU and raise
  throughput. The Stage-2 worker picks A or B by task. No external API endpoint is
  configured.
- **Versioning:** every result records `model_versions` so re-runs after a model
  upgrade are auditable and reproducible (replay from Kafka).

---

## 4. Fine-tuning strategy (Bangla + English)

Goal: lift accuracy on _your_ domain (FB/IG, code-mixed Banglish, local
entities/brands) without training from scratch.

1. **Start zero-shot / off-the-shelf.** Ship MVP with pretrained multilingual
   models (XLM-R, GLiNER, bge-m3). Establish baselines and a labeled eval set.
2. **Collect & label.** Use the platform's own low-confidence/router-flagged
   posts as an active-learning pool — those are exactly the examples worth
   labeling. Build a held-out eval set per task and per language.
3. **Parameter-efficient fine-tuning (LoRA/QLoRA).** Fine-tune the shared XLM-R
   encoder + task heads, and optionally LLM-A (the 7B/8B local model), with
   LoRA/QLoRA. Cheap (single GPU), fast, easy to version and roll back. Both LLMs
   are local, so fine-tuned weights stay in-house — no API to depend on.
4. **Code-mixed focus.** Explicitly include Banglish (Bangla in Latin script,
   mixed sentences) in training data — this is where off-the-shelf models fail
   most on social content.
5. **Distillation (later).** Distill LLM judgments on the hardest tasks into the
   small classifiers, shrinking the slice that needs the LLM and cutting cost
   further.
6. **Evaluation gate.** No model ships without beating the current one on the
   per-language eval set; track per-task F1 and the LLM-routing rate (a good
   fine-tune should _lower_ how often Stage 2 is needed).
7. **Continuous loop.** Periodically retrain on freshly labeled router-flagged
   data; version models, replay a sample batch from Kafka to compare.

---

## 5. RAG evaluation — is it needed?

**Per-post analysis: NO.** Classifying/labeling a single post needs the post's
own text, not retrieval. Adding RAG there would only add latency and cost.

**Reporting / insight / analyst Q&A: YES, valuable.** RAG shines for:

- "What are people saying about _Brand X_ this week?" — retrieve relevant posts
  from Qdrant, feed to the LLM grounded.
- Grounded **report generation** and **insight generation** across the corpus.
- **Cluster summarization** — retrieve cluster members, summarize with citations.

**Benefits:** grounded, up-to-date, citation-able answers without stuffing the
whole corpus into context; reuses embeddings you already compute.
**Drawbacks:** retrieval-quality dependent; extra vector-store ops; can hallucinate
if retrieval is poor — mitigate with good chunking, metadata filters, and
showing source posts.

**Recommended stack:** **Qdrant** vector DB + **bge-m3 / multilingual-e5**
embeddings (Bangla-capable) + **LLM-B** (the large local model) for generation.
This is the same Qdrant + embeddings already in the core pipeline, so RAG is
nearly free to add at the reporting layer — and fully local end to end.

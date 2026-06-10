# AI Model Selection, RAG & Fine-Tuning

Model recommendations per task, the Bangla/Banglish/English fine-tuning strategy,
and the RAG evaluation for the smart layer in [what.txt](what.txt). All small-model
choices favor open-source, GPU-efficient models with genuine Bangla support and
run **self-hosted**. The Stage-2 LLM runs behind a **pluggable backend with two
interchangeable providers — `local` (self-hosted vLLM) and `groq` (Groq Cloud
API) — switchable at runtime** (see §2). Default is `local` (no per-token bill, no
data egress); `groq` is an opt-in switch for fastest inference and zero GPU ops.

The guiding rule from [architecture.md](architecture.md): **small models do the
bulk work; the LLM is selective.** So the table below is mostly _small_ models,
plus **two LLM roles** (LLM-A fast / LLM-B quality) served by the chosen backend
for the selective stage (see §2).

**The input is a post + its comment thread** — a post pulled from the upstream
**Post API** joined to comments from the **Comment API**
([data_contract.md](data_contract.md)), spanning Facebook, Telegram, X, Instagram,
… — **and the content is heavily "Banglish"** (romanized Bangla, often mixed with
English in one sentence, e.g. "Green garden e vat 25 taka baire 10 taka"). Every
model choice below is judged on how well it handles **bn + en + code-mixed
Banglish**, because that — not clean Bangla or clean English — is the real traffic
(see [examples.md](examples.md)). The input is also **multimodal**: ~80% of posts
carry an **image**, and ~half have a `null` caption ([data_contract.md](data_contract.md)
§1), so the image is not optional. Small models run **post first — text
(caption + OCR) sentiment _and_ a visual `image_sentiment` on the photo, fused —
then each comment**; a selective LLM/**VLM** summarizes the **thread** grounded on
caption + OCR + image, in the post's original language. The post's analyzable text
includes upstream `photoOcrTexts` (OCR is already done upstream). The
text+image+summary pipeline order is the **first target** — see
[data_contract.md](data_contract.md) §4.

---

## 1. Model recommendations per task

| Task                                | Recommended model(s)                                                       | Bangla | English | Notes                                                                  |
| ----------------------------------- | -------------------------------------------------------------------------- | ------ | ------- | ---------------------------------------------------------------------- |
| **Language + Banglish detection**   | `fastText lid.176` + CLD3 + transliteration heuristic                      | ✅      | ✅       | <1 ms/item; flags `banglish` (romanized bn) → multilingual path        |
| **Text sentiment** (caption + per comment) | `XLM-RoBERTa`/`mBERT` fine-tuned; BanglaBERT for bn                 | ✅      | ✅       | **Recompute** ours (caption first, then each comment) → `text_sentiment` + `sentiment_breakdown`; keep upstream `sentiment` as `baseline_sentiment` (never overwrite — [data_contract.md](data_contract.md) §4) |
| **Image sentiment** (visual)        | `SigLIP 2` / `CLIP` zero-shot (positive/negative/neutral prompts), or a fine-tuned ViT | n/a (language-agnostic) | n/a | Cheap Stage-1 model on **every image post** → `image_sentiment` (per image + aggregate). Visual, independent of caption/OCR. Fused with text sentiment |
| **Image description / caption**     | small **VLM** (`Qwen2.5-VL-3B/7B`) or `BLIP-2`                              | ✅      | ✅       | Short description of the image → grounds `post_summary` (esp. `null`-caption photo posts); feeds `image_analysis.description` |
| **OCR (image text)**                | **reuse upstream `photoOcrTexts`**; fallback `PaddleOCR`/`Tesseract` (bn+en) | ✅    | ✅       | Already produced upstream for 25/50; only run if missing. Folded into the text path |
| **Emotion**                         | XLM-R fine-tuned (joy/anger/sadness/fear/…); GoEmotions heads for en       | ✅      | ✅       | Shares encoder with sentiment to save GPU                              |
| **Topic classification**            | XLM-R / embedding + classifier head; or zero-shot via small NLI model      | ✅      | ✅       | Use embeddings + lightweight classifier; reduces per-label models      |
| **Intent**                          | XLM-R fine-tuned (inform/promote/complain/request/…)                       | ✅      | ✅       | Per comment too (price/availability/location inquiries)                |
| **Toxicity / hate / offensive**     | `XLM-R`/`mBERT` fine-tuned; Detoxify (en) + Bangla hate datasets           | ✅      | ✅       | Bangla hate-speech corpora exist (e.g. Bengali Hate Speech); fine-tune |
| **NER (person/org/location/brand)** | `GLiNER` (multilingual, zero/few-shot), `spaCy` (en), BanglaBERT-NER (bn)  | ✅      | ✅       | GLiNER gives flexible entity types without per-type models             |
| **Embeddings**                      | `BAAI/bge-m3` (multilingual, incl. Bangla) or `intfloat/multilingual-e5`   | ✅      | ✅       | Powers dedup, comment clustering, semantic search, RAG                 |
| **Summarization** (multimodal)      | text: **LLM-A**/**LLM-B**; image posts: a **VLM** (`Qwen2.5-VL` local ⇄ a Groq vision model) — §2 | ✅      | ✅       | Selective; **grounded on caption + OCR + image**; summary in the post's original language |
| **Insight / report generation**     | **LLM-B** role + RAG, on the active backend (see §2)                       | ✅      | ✅       | Cluster summaries → corpus-level insight                               |
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
also handles code-mixed Bangla-English in a single model — critical for the real
multi-platform content (Facebook, Telegram, X, Instagram, …).

---

## 2. The selective LLMs — two roles, a pluggable backend (local ⇄ Groq)

The selective Stage-2 work is split across **two logical roles** — **LLM-A**
(fast / high-throughput) and **LLM-B** (large / high-quality). Each role is
fulfilled by a **pluggable backend, switchable at runtime**:

- **`local` (default)** — self-hosted models on **vLLM**, on our own GPUs. Post
  content never leaves the cluster and there is no per-token bill; capacity grows
  by adding GPUs. Best for privacy-sensitive data, steady high volume, and
  predictable cost.
- **`groq`** — the **Groq Cloud API** (OpenAI-compatible), serving the same open
  model families (Llama 3.x, Qwen, etc.) on Groq's LPU hardware. Extremely fast
  inference, **zero GPU/model-serving ops**, elastic burst — billed per token,
  and prompt content leaves the cluster. Best for bursty load, no/low local GPU,
  or when you want the lowest latency.

Because both backends speak the **same OpenAI-compatible API**, the Stage-2 worker
is backend-agnostic — only the base URL, API key, and model name differ. Switching
is a config/flag change (env `LLM_BACKEND=local|groq`, or a per-request override —
see [api_design.md](api_design.md)), applied **at runtime without a redeploy**. You
can even run **hybrid**: e.g. `local` LLM-A for per-post volume + `groq` for LLM-B
report bursts, or fail over local→Groq under load.

### Role → model mapping per backend

| Role                               | `local` backend (vLLM, our GPUs)                                               | `groq` backend (Groq Cloud API)                                  | Serves                                                                             |
| ---------------------------------- | ------------------------------------------------------------------------------ | ---------------------------------------------------------------- | ---------------------------------------------------------------------------------- |
| **LLM-A — fast / high-throughput** | `Qwen2.5-7B-Instruct` (or `Llama-3.1-8B-Instruct`), AWQ/GPTQ quantized         | a fast Groq model (e.g. `llama-3.1-8b-instant`)                  | Per-post selective refinement, hardest classification, short single-post summaries |
| **LLM-B — large / high-quality**   | `Qwen2.5-32B-Instruct` (or `Qwen2.5-14B-Instruct` at smaller scale), quantized | a larger Groq model (e.g. `llama-3.3-70b-versatile`)             | Cluster summarization, corpus insight, grounded report generation (RAG)            |
| **VLM — vision-language**          | `Qwen2.5-VL-7B-Instruct` (or `-3B` at MVP), on vLLM                            | a Groq vision model (e.g. a Llama Vision / multimodal model id) | **Image-grounded `post_summary`** for photo posts (takes caption + OCR + image); image description |

> Groq model IDs change as their catalog evolves — treat the examples above as
> placeholders and pin the current IDs in config. The prompts and JSON output
> schema are identical across backends, so a switch needs no prompt changes.

Why two roles and not one: the two jobs have opposite profiles. Per-post refinement
is **high-volume, low-difficulty** (favor a small fast model); cluster/report
generation is **low-volume, high-quality** (favor a larger model). Splitting them
lets each be right-sized and scaled independently, instead of paying 32B-class cost
for every per-post call or accepting 7B-class quality on reports. On the `local`
backend at MVP scale the two can be **time-sliced on a single GPU**, or LLM-B
dropped and LLM-A used for both until volume justifies the second model; on the
`groq` backend the two roles are just two model IDs with no extra infrastructure.

Both backends use **structured JSON output mode** to minimize tokens. See
[cost_estimation.md](cost_estimation.md) for per-batch economics of each backend
and [infrastructure.md](infrastructure.md) for GPU sizing (local) / sizing-free
notes (Groq).

> **Switchable by design.** If a backend is saturated or unhealthy the router can
> degrade LLM-B→LLM-A, **fail over to the other backend** (when both are
> configured), or return Stage-1-only results — see
> [architecture.md](architecture.md) §8. Privacy-locked tenants can be pinned to
> `local` so a switch never routes their data to Groq.

---

## 3. Model serving architecture

```text
   NLP fleet (Stage 1)                  LLM roles (Stage 2) — pluggable backend
 ┌───────────────────────┐          ┌──────────────────────────────────────────┐
 │ Triton / ONNX Runtime │          │  Stage-2 worker (OpenAI-compatible client) │
 │  + CTranslate2        │          │      picks role LLM-A / LLM-B by task      │
 │  dynamic batching     │          └───────────────┬────────────────┬───────────┘
 │  many small models    │            LLM_BACKEND=local│        =groq │
 └──────────┬────────────┘          ┌─────────────────▼──┐   ┌───────▼──────────┐
            │ gRPC/HTTP             │ vLLM (our GPUs):    │   │ Groq Cloud API   │
   Stage-1 worker pulls batch       │  LLM-A 7B/8B fast   │   │ OpenAI-compatible│
   from queue, calls Triton         │  LLM-B 14B/32B qual │   │ Llama/Qwen on LPU│
                                     │  quantized, paged KV│   │ per-token, no GPU│
                                     └─────────────────────┘   └──────────────────┘
```

- **NLP models** → **Triton Inference Server** (or ONNX Runtime / CTranslate2)
  with dynamic batching and INT8/FP16 quantization. Multiple models share GPUs;
  Triton handles concurrent model execution and batching. (NLP is always
  self-hosted — only the Stage-2 LLM has a Groq option.)
- **LLM (Stage 2)** → one **OpenAI-compatible Stage-2 worker** that targets the
  configured backend:
  - `local`: **two vLLM deployments** (paged attention + continuous batching),
    LLM-A (fast) and LLM-B (large), quantized (AWQ/GPTQ) to fit GPU and raise
    throughput.
  - `groq`: the **Groq Cloud endpoint**, with role→model-ID mapping in config; no
    GPU, no model loading, just outbound HTTPS.
  The worker picks the **role** (A or B) by task; the **backend** is selected by
  config/flag and switchable at runtime.
- **Versioning:** every result records `processing.llm_backend` + `llm_model` +
  `model_versions` so re-runs (and a backend switch) are auditable and
  reproducible (replay from Kafka).

---

## 4. Fine-tuning strategy (Bangla + English)

Goal: lift accuracy on _your_ domain (Facebook/Telegram/X/Instagram, code-mixed Banglish, local
entities/brands) without training from scratch.

1. **Start zero-shot / off-the-shelf.** Ship MVP with pretrained multilingual
   models (XLM-R, GLiNER, bge-m3). Establish baselines and a labeled eval set.
2. **Collect & label.** Use the platform's own low-confidence/router-flagged
   posts as an active-learning pool — those are exactly the examples worth
   labeling. Build a held-out eval set per task and per language.
3. **Parameter-efficient fine-tuning (LoRA/QLoRA).** Fine-tune the shared XLM-R
   encoder + task heads, and optionally LLM-A (the 7B/8B model) on the **`local`
   backend**, with LoRA/QLoRA. Cheap (single GPU), fast, easy to version and roll
   back; fine-tuned weights stay in-house. Note this is a **local-backend
   advantage**: the `groq` backend runs Groq's hosted models, which we can steer
   only via prompting/few-shot, not custom fine-tunes. Teams that need fine-tuned
   LLM behavior should keep those tasks on `local` (the NLP fleet is fine-tuned
   regardless of which LLM backend is active).
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
embeddings (Bangla-capable) + **LLM-B** (the large role) for generation, on
whichever Stage-2 backend is active — `local` for fully in-cluster RAG, or `groq`
for faster report generation when the retrieved context may leave the cluster.
This reuses the same Qdrant + embeddings already in the core pipeline, so RAG is
nearly free to add at the reporting layer; retrieval/embeddings stay local in
both cases — only the final generation call follows the chosen backend.

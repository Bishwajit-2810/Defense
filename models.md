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

**The input is a post + its comment thread** — a **post-with-details** payload
with the comments **embedded** ([data_contract.md](data_contract.md)), Facebook in
the current sample (others by URL host)
— **and the content is heavily "Banglish"** (romanized Bangla, often mixed with
English in one sentence, e.g. "Green garden e vat 25 taka baire 10 taka"). Every
model choice below is judged on how well it handles **bn + en + code-mixed
Banglish**, because that — not clean Bangla or clean English — is the real traffic
(see [examples.md](examples.md)). The comment thread arrives **embedded**
([data_contract.md](data_contract.md)), so the comments are not optional. Small
models run **post first — caption sentiment — then each comment**; a selective
LLM summarizes the **thread**, in the post's original language. **Comment
sentiment is ours** — the upstream leaves it empty. The crowd
`reactionBreakdown` (LIKE/LOVE/HAHA/WOW/SAD/ANGRY/CARE) is a free **emotion
prior** we cross-check against.

> **The image modality is implemented but unexercised (4 August 2026).** Most
> posts do carry an image, but the corpus's 69 `photoUrls` are relative
> object-storage keys and the objects are not in MinIO, so **no image bytes are
> reachable in any runnable configuration** — the image term has never
> contributed a non-zero value ([PROJECT_ASSESSMENT.md](PROJECT_ASSESSMENT.md)
> §5.2). OCR is consequently off by default (`STAGE1_OCR_SENTIMENT=false`), the
> working corpus is `posts_text_only.json` (43 captioned posts), and post
> sentiment is a **text** measurement. The vision rows below are kept because
> the code path is retained and the models are the right ones — they are
> labelled ⚠ so nothing here reads as a measured capability.

---

## 1. Model recommendations per task

| Task                                       | Recommended model(s)                                                                              | Bangla                  | English | Notes                                                                                                                                                                                                                                                                  |
| ------------------------------------------ | ------------------------------------------------------------------------------------------------- | ----------------------- | ------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Language + Banglish detection**          | `fastText lid.176` + CLD3 + transliteration heuristic                                             | ✅                      | ✅      | <1 ms/item; flags `banglish` (romanized bn) → multilingual path                                                                                                                                                                                                        |
| **Text sentiment** (caption + per comment) | `XLM-RoBERTa`/`mBERT` fine-tuned; BanglaBERT for bn                                               | ✅                      | ✅      | **Recompute** ours (caption first, then each embedded comment) → `text_sentiment` + `sentiment_breakdown`. Post `sentiment` kept as `baseline_sentiment`; **comment sentiment is entirely ours** (upstream leaves it `null`) — [data_contract.md](data_contract.md) §4. Comments are **batched**: grouped by resolved model, one forward pass per group (transformer inference is 10–30× faster batched, and 8,513 of 10,272 comments took the unbatched path). Every label reports `method`, so a stub fallback is never counted as a model inference |
| **Image sentiment** (visual) — ⚠ **unexercised** | `SigLIP 2` / `CLIP` zero-shot (positive/negative/neutral prompts), or a fine-tuned ViT            | n/a (language-agnostic) | n/a     | Implemented, but **has never produced a signal**: the corpus's `photoUrls` are relative object-storage keys and the objects are not in MinIO, so no image bytes are reachable (PROJECT_ASSESSMENT §5.2). A failure now reports `vision_status` (`fetch_failed` / `model_unavailable` / `stub`) instead of a fake `neutral`, and fusion **excludes the absent term** rather than letting it consume its weight |
| **Image description / caption**            | small **VLM** (`Qwen2.5-VL-3B/7B`) or `BLIP-2`                                                    | ✅                      | ✅      | Short description of the image → grounds `post_summary` (esp. `null`-caption photo posts); feeds `image_analysis.description`                                                                                                                                          |
| **OCR (image text)** — ⚠ **off by default**  | **ours** — `PaddleOCR` / `Tesseract` (bn+en), or the VLM                                          | ✅                      | ✅      | The payload no longer ships OCR text, so we **run OCR ourselves** on `photoUrls`; result folded into the text path + `image_analysis.ocr_text`. Gated behind `STAGE1_OCR_SENTIMENT=false` while no image bytes are reachable — the code path is retained, not deleted |
| **Emotion**                                | XLM-R fine-tuned (joy/anger/sadness/fear/…); GoEmotions heads for en                              | ✅                      | ✅      | Shares encoder with sentiment to save GPU; **cross-checked against `reactionBreakdown`** (SAD/ANGRY/HAHA/LOVE crowd signal)                                                                                                                                            |
| **Topic classification**                   | XLM-R / embedding + classifier head; or zero-shot via small NLI model                             | ✅                      | ✅      | Use embeddings + lightweight classifier; reduces per-label models                                                                                                                                                                                                      |
| **Intent**                                 | XLM-R fine-tuned (inform/promote/complain/request/…)                                              | ✅                      | ✅      | Per comment too (price/availability/location inquiries)                                                                                                                                                                                                                |
| **Toxicity / hate / offensive**            | `XLM-R`/`mBERT` fine-tuned; Detoxify (en) + Bangla hate datasets                                  | ✅                      | ✅      | Bangla hate-speech corpora exist (e.g. Bengali Hate Speech); fine-tune                                                                                                                                                                                                 |
| **NER (person/org/location/brand)**        | `GLiNER` (multilingual, zero/few-shot), `spaCy` (en), BanglaBERT-NER (bn)                         | ✅                      | ✅      | GLiNER gives flexible entity types without per-type models                                                                                                                                                                                                             |
| **Embeddings**                             | Default `paraphrase-multilingual-mpnet-base-v2` (768-dim); `BAAI/bge-m3` (1024-dim) or `intfloat/multilingual-e5` are options — **but must match the `analysis_results.embedding vector(768)` column** (a 1024-dim model means resizing the column) | ✅                      | ✅      | Powers dedup, comment clustering, semantic search, RAG; 768-dim default keeps the pgvector column as-is                                                                                                                                                                                                                 |
| **Summarization**                          | the **`summary`** role (§2); image posts would use a **VLM** (`Qwen2.5-VL` local ⇄ a Groq vision model) when an image is actually fetchable | ✅                      | ✅      | Selective; grounded on caption (+ OCR + image when available — `post_summary_grounding` records which); summary in the post's original language. A reply that hits the token ceiling is auto-continued, then flagged `post_summary_truncated` and never cached          |
| **Insight / report generation**            | **LLM-B** role + RAG, on the active backend (see §2)                                              | ✅                      | ✅      | Cluster summaries → corpus-level insight                                                                                                                                                                                                                               |
| **Target stance** (per named entity)       | alias matcher (no model) + the Stage-2 stance prompt it rides inside; deterministic clause scorer as fallback | ✅                      | ✅      | Stance *toward a watchlist entity*, not toward the post. **No additional LLM calls** — matched entities are injected into a call already being made. The matcher is the hard part: the same entity appears in Bangla script, romanized Banglish and English ([stance_targets.md](stance_targets.md)) |
| **Keyword extraction**                     | KeyBERT (on embeddings) / YAKE                                                                    | ✅                      | ✅      | Cheap, no extra GPU model                                                                                                                                                                                                                                              |

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

| Role                               | `local` backend (vLLM, our GPUs)                                               | `groq` backend (Groq Cloud API)                                 | Serves                                                                                             |
| ---------------------------------- | ------------------------------------------------------------------------------ | --------------------------------------------------------------- | -------------------------------------------------------------------------------------------------- |
| **LLM-A — fast / high-throughput** | `Qwen2.5-7B-Instruct` (or `Llama-3.1-8B-Instruct`), AWQ/GPTQ quantized         | a fast Groq model (e.g. `llama-3.1-8b-instant`)                 | Per-post selective refinement, hardest classification, short single-post summaries                 |
| **LLM-B — large / high-quality**   | `Qwen2.5-32B-Instruct` (or `Qwen2.5-14B-Instruct` at smaller scale), quantized | a larger Groq model (e.g. `llama-3.3-70b-versatile`)            | Cluster summarization, corpus insight, grounded report generation (RAG)                            |
| **VLM — vision-language**          | `Qwen2.5-VL-7B-Instruct` (or `-3B` at MVP), on vLLM                            | a Groq vision model (e.g. a Llama Vision / multimodal model id) | **Image-grounded `post_summary`** for photo posts (takes caption + OCR + image); image description |

> Groq model IDs change as their catalog evolves — treat the examples above as
> placeholders and pin the current IDs in config. The prompts and JSON output
> schema are identical across backends, so a switch needs no prompt changes.

### Pipeline roles `stage1` / `stage2` / `summary` (implementation)

The per-post pipeline binds three concrete roles in `src/defense/libs/llm/client.py`, each
with its own model id so the stages and tasks run on **different models**:

| Role | env (`local` / `groq`) | default (Ollama / Groq) | Serves |
| --- | --- | --- | --- |
| `stage1` | `STAGE1_LOCAL_MODEL` / `STAGE1_GROQ_MODEL` | `gemma3:4b` / `llama-3.1-8b-instant` | **Stage-1 Fast NLP** — sentiment/emotion/topic/intent/toxicity/NER/keywords over caption + comments (`STAGE1_LLM=true`) |
| `stage2` | `STAGE2_LOCAL_MODEL` / `STAGE2_GROQ_MODEL` | `qwen2.5:7b` / `llama-3.3-70b-versatile` | **Stage-2 classification** — post-type, insight, context-aware comment stance |
| `summary` | `SUMMARY_LOCAL_MODEL` / `SUMMARY_GROQ_MODEL` | `qwen2.5:7b` / `llama-3.3-70b-versatile` | **Stage-2 summarization** — post summary and comment summary |

`stage1` is the LLM realization of the small-model suite in §1 (a small fast model
carries the high-volume per-post + per-comment NLP). `llm_a` / `llm_b` remain the
architectural fast / quality roles for the agents + report layer (§5), and the VLM
role is unchanged. On local Ollama all of these can be time-sliced on one GPU; on
Groq they are just distinct model IDs.

**Why `summary` is separate from `stage2`.** The two Stage-2 jobs have opposite
requirements: classification picks from a **fixed vocabulary** and wants a cheap,
constrained model; summarization writes **Bangla prose** and wants a fluent one.
Keeping them on one role forced a single compromise, and it is also the honest
version of the cost story — the expensive model is spent on the one task that
needs it, **once per post**, rather than on every classification call (which,
since comment stance runs per batch, would multiply straight through the
comment lane). `summary` defaults to the same model as `stage2`, so nothing
changes until you point it elsewhere.

**Pick the summary model by measurement, not reputation:**

```bash
python -m eval.bakeoff_summary --posts 10 --models qwen2.5:7b gemma4:26b gemma4:31b
```

It scores each candidate on real Bangla posts for latency (p50/p95), summary
length, **truncation rate**, whether the summary stayed in the post's own script,
and a crude grounding proxy — then prints the summaries so faithfulness and
fluency can be judged by eye. Record the table: it is the ablation
[PROJECT_ASSESSMENT.md](PROJECT_ASSESSMENT.md) §7.4 keeps asking for, and
"we chose it because it scored X" is defensible in a way "it is bigger" is not.

**Two implementation notes that matter when changing a model:**

- The Stage-2 response cache keys on the **resolved model id**, not the role
  label. Before that fix, switching models re-served the previous model's answers
  for 7 days — which would have silently invalidated any model comparison
  (§5.10). Set `LLM_CACHE_DISABLED=1` for evaluation runs anyway.
- `processing.role_models` in the output records which model each role resolved
  to, so a summary can always be attributed to the model that wrote it.

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
   fine-tune should _lower_ how often Stage 2 is needed). The full scoring rubric,
   gold sets, and gates are in [evaluation.md](evaluation.md).
7. **Continuous loop.** Periodically retrain on freshly labeled router-flagged
   data; version models, replay a sample batch from Kafka to compare.

---

## 5. RAG evaluation — is it needed?

**Per-post analysis: NO.** Classifying/labeling a single post needs the post's
own text, not retrieval. Adding RAG there would only add latency and cost.

**Reporting / insight / analyst Q&A: YES, valuable.** RAG shines for:

- "What are people saying about _Brand X_ this week?" — retrieve relevant posts
  via pgvector (Postgres extension), feed to the LLM grounded.
- Grounded **report generation** and **insight generation** across the corpus.
- **Cluster summarization** — retrieve cluster members, summarize with citations.

**Benefits:** grounded, up-to-date, citation-able answers without stuffing the
whole corpus into context; reuses embeddings you already compute.
**Drawbacks:** retrieval-quality dependent; extra vector-store ops; can hallucinate
if retrieval is poor — mitigate with good chunking, metadata filters, and
showing source posts.

**Recommended stack:** **Postgres + pgvector** (the `analysis_results.embedding`
`vector(768)` column, cosine distance via the `<=>` operator) + **bge-m3 /
multilingual-e5** embeddings (Bangla-capable) + **LLM-B** (the large role) for
generation, on whichever Stage-2 backend is active — `local` for fully in-cluster
RAG, or `groq` for faster report generation when the retrieved context may leave
the cluster. This reuses the same pgvector embeddings already stored in the core
pipeline, so RAG is nearly free to add at the reporting layer; retrieval/embeddings
stay local in both cases — only the final generation call follows the chosen
backend.

### Delivered as an agentic loop over MCP tools

In practice RAG here is driven by the **Insight/Analyst agent**
([architecture.md](architecture.md) §11), not a single retrieve→generate call. The
agent runs on **LLM-B** (Qwen/Llama — both support tool/function calling, which MCP
builds on) and plans over **MCP tools**: `retrieval-mcp.semantic_search` /
`get_thread` (Postgres + pgvector) for grounding, `analytics-mcp.trend_query` /
`reaction_mix` (ClickHouse) for the numbers, and a **VLM** step when an answer needs
the images. This keeps reports **grounded and cited**, lets the agent decide _how
much_ to retrieve, and stays on the chosen backend (`local` for in-cluster, `groq`
for speed) under the same tenant policy. Agents are corpus/report-tier and gated —
**never per post** — so the cost model is unchanged.

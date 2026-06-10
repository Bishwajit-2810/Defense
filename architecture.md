# Architecture — Social Media "Smart Layer" Microservice

This is the **recommended** architecture. For the alternatives that were weighed
to arrive here, see [possible_architecture.md](possible_architecture.md). For
model choices see [models.md](models.md), for cost see
[cost_estimation.md](cost_estimation.md), for real input→output examples see
[examples.md](examples.md).

## 0. What this service is

An **existing social-media monitoring platform** scrapes 1,000+ real-time
Bangla/English/Banglish posts (Facebook, Telegram, X, Instagram, …), **each with
its comment thread**, and exposes them over a **Post API** and a **Comment API**.
This service is a **smart, self-contained microservice** — a "thinking layer" —
that sits on top of that platform and:

- **pulls** posts and their comments from the upstream APIs (joined by the post's
  unique id) into its **own separate database** — read-only consumer, no
  write-back (full input contract: [data_contract.md](data_contract.md)),
- decides _per item_ how much intelligence each one needs (the cheap NLP models
  vs. an LLM — this routing is the "smart" part), and
- returns one **structured JSON** object per thread (summary in the post's own
  language, sentiment, topics, intents, entities, brand mentions, comment
  analysis — see §6) that downstream projects consume directly.

The upstream already stores a coarse `sentiment` and `viralPotential` per post; we
keep those as a **baseline** and **recompute** our own richer sentiment
([data_contract.md](data_contract.md) §4). Platform is **derived from each post's
URL host**, so the service is platform-agnostic, not Facebook/Instagram-specific.

**Posts are multimodal, and that is the first target** (in order, see
[data_contract.md](data_contract.md) §4): (1) **text sentiment** on the caption,
(2) **image sentiment** from a _visual_ model on the photo when one is present
(separate from its OCR), (3) **fuse** the two into the post's overall sentiment,
(4) a **post summary grounded on caption + image/OCR** (so a photo-only,
`null`-caption post still gets a meaningful summary), then (5) **comments**.

Hard product constraints from the owner: **fast**, **cost-effective**,
**efficient**, and **accurate on Bangla and Banglish** (with fine-tuning hooks
for when accuracy must improve). It must **scale** horizontally to handle batches
of 1k → 10k → 100k threads.

The Stage-2 LLM is a **pluggable backend with two interchangeable providers —
`local` (self-hosted vLLM) and `groq` (Groq Cloud API) — switchable at runtime**
(see [models.md](models.md) §2). The default backend is **local** (no per-token
bill, no data egress); **Groq** is an opt-in switch for fastest inference and
zero GPU ops. The choice is config- and request-level, so the operator can flip
between them at any time without redeploying.

---

## 1. Design principles

1. **Hybrid intelligence, not LLM-everywhere.** The prompt explicitly forbids
   sending every post to an LLM. Small specialized models are 100–1000× cheaper
   and 10–100× faster per post. The LLM is a _scalpel_, not a _firehose_: it runs
   only for tasks small models can't do well (summarization, cross-post insight,
   reports) or when a small model is uncertain.
2. **Queue-based and stage-decoupled.** Every heavy stage is a stateless worker
   pool fed by a durable queue. Backpressure, retries, and independent scaling
   come for free.
3. **Scale by partition, not by bigger machines.** Throughput grows by adding
   workers and queue partitions, not vertical scaling.
4. **Storage by access pattern.** No single database is good at OLTP +
   aggregation + vector search. Use the right store per job.
5. **Idempotent + observable.** Every post has a stable hash; reprocessing is
   safe. Every stage emits metrics, logs, and traces.

---

## 2. High-level architecture

```text
                          ┌─────────────────────────────┐
   Clients                │  Flutter Dashboard / API     │
 (dashboard, uploaders)   │  consumers / 3rd-party apps  │
                          └───────────────┬─────────────┘
                                          │ HTTPS
                          ┌───────────────▼─────────────┐
                          │   API Gateway + LB           │  NGINX / K8s Ingress
                          │   (TLS, routing, rate limit) │
                          └───────────────┬─────────────┘
                                          │
              ┌───────────────────────────┼───────────────────────────┐
              │                           │                           │
     ┌────────▼────────┐        ┌─────────▼─────────┐       ┌─────────▼────────┐
     │  Auth Service   │        │ Ingestion Service │       │ Reporting / Query│
     │ (JWT, API keys) │        │ (validate, dedup, │       │ Service          │
     └─────────────────┘        │  enqueue)         │       │ (read APIs)      │
                                └─────────┬─────────┘       └────────┬─────────┘
                                          │ produce                  │ read
                                ┌─────────▼─────────┐                │
                                │   Message Bus     │                │
                                │ Kafka / Redis Str │                │
                                │  (partitioned)    │                │
                                └─────────┬─────────┘                │
                                          │ consume                  │
        ┌─────────────────────────────────┼──────────────────────────────────┐
        │  STAGE 1 — Fast NLP workers (CPU + small GPU), horizontally scaled   │
        │  lang detect · sentiment · emotion · topic · toxicity · NER · embed  │
        └─────────────────────────────────┬──────────────────────────────────┘
                                          │ writes features + confidence
                                ┌─────────▼─────────┐
                                │ Router / Triage   │  decides: done? or → LLM?
                                │ (confidence gate, │
                                │  task flags)      │
                                └─────┬───────┬─────┘
                          done│             │needs LLM (selective)
                              │       ┌─────▼──────────┐
                              │       │ STAGE 2 — LLM   │  pluggable backend:
                              │       │ workers         │  local vLLM ⇄ Groq API
                              │       │ summarize·insight│  roles LLM-A / LLM-B
                              │       │ ·report·hard NER │  (switchable runtime)
                              │       └─────┬───────────┘
                              └─────────────┤
                                ┌───────────▼───────────┐
                                │  Result Assembler      │  builds final JSON
                                │  + JSON schema validate│
                                └───────────┬───────────┘
                                            │ fan-out writes
        ┌───────────────┬───────────────────┼───────────────────┬───────────────┐
   ┌────▼─────┐   ┌─────▼──────┐      ┌──────▼──────┐     ┌──────▼──────┐  ┌─────▼─────┐
   │PostgreSQL│   │ ClickHouse │      │   Qdrant    │     │   Redis     │  │  Object   │
   │ ops+jobs │   │ analytics  │      │  vectors    │     │ cache/dedup │  │  storage  │
   └──────────┘   └────────────┘      └─────────────┘     └─────────────┘  └───────────┘
```

The **Router/Triage** between Stage 1 and Stage 2 is the heart of the cost
strategy — see §5.

---

## 3. Data flow (end to end)

1. **Ingest (pull).** The Ingestion Service **pulls from the upstream Post API**
   (poll for `status: NOT_ANALYZED` / a campaign / time window) and, per post,
   **pulls its comments from the Comment API** joined by the post's unique `id`
   (see [data_contract.md](data_contract.md)). It copies the records into **our own
   database** (no write-back upstream), derives `platform` from the URL host,
   keeps the upstream `sentiment`/`viralPotential` as `baseline_*`, normalizes text
   (Unicode NFC, emoji handling, Bangla/English/Banglish script tagging) over the
   **caption + OCR text** (`photoOcrTexts`), assembles the comment thread (ordered,
   `parent_id` preserved), and computes a content hash over the post + comments.
   A pushed batch (`POST /v1/posts/upload`) is also accepted for external/replay
   sources, but the upstream APIs are the primary path.
2. **Dedup gate.** Hash is checked against Redis (recent) and Qdrant/Postgres
   (historical). Exact duplicates short-circuit to the cached result; near
   duplicates (cosine > threshold on embedding) can reuse prior analysis. This
   alone removes a large fraction of LLM/NLP work on real social feeds.
3. **Enqueue.** A `job` row is created in PostgreSQL (`status=queued`). One
   message per post is produced to the bus, partitioned by `post_id` hash so a
   post's ordering is stable and load spreads evenly.
4. **Stage 1 — Fast NLP + vision (parallel).** Worker pulls a batch
   (micro-batching) and runs the small-model suite. **Multimodal, post first, then
   comments** (see [data_contract.md](data_contract.md) §4):
   - **Text path** — the **caption + OCR text** (`photoOcrTexts`) gets
     language/Banglish detection → **our recomputed** `text_sentiment`, emotion,
     topic, intent, toxicity/hate, NER, keyword extraction, and a sentence
     embedding.
   - **Vision path (image posts)** — a cheap **visual** model scores
     `image_sentiment` per image (language-agnostic) and produces a short image
     description/OCR for grounding ([models.md](models.md) §1). Skipped for
     text-only posts.
   - **Fuse** `text_sentiment` + `image_sentiment` → post-level
     `overall_sentiment`/`sentiment_score` (text-weighted when a caption exists;
     image + OCR-weighted for `null`-caption photo posts). Upstream
     `sentiment`/`viralPotential` are retained alongside as `baseline_*`.
   The **same text models** then run over **each comment**, aggregated into the
   thread's `sentiment_breakdown`. (Until the Comment API is wired, the post pass
   runs standalone and `comment_analysis.analyzed = 0`.) Each output carries a
   **confidence** score. Results are written to a partial-result store.
5. **Router/Triage (the "smart thinking layer").** For each thread the router
   decides:
   - All required fields produced with confidence ≥ threshold, and no LLM-only
     task requested → **mark complete**, skip Stage 2.
   - A `post_summary` is requested, or confidence is below threshold, or the
     thread is ambiguous / heavily Banglish / long → **route to Stage 2** with a
     compact, token-minimized prompt (post + a representative/clustered subset of
     comments, truncated). This selectivity is what keeps the service fast and
     cheap.
6. **Stage 2 — LLM / VLM (selective).** The Stage-2 worker produces summaries,
   insights, and refined labels through two **logical roles** — **LLM-A** (fast
   7B/8B) for per-post refinement and short summaries, **LLM-B** (large 14B/32B)
   for cluster summarization, insight, and report generation. **The `post_summary`
   is grounded on caption + OCR + image:** for an image post the summary task runs
   on a **vision-language model (VLM)** that receives the caption, OCR text, and
   the image (or a Stage-1 image description) so a photo-only post is summarized
   from its picture; text-only posts use the text LLM. Each role is served by a
   **pluggable backend, switchable at runtime**: `local` (self-hosted vLLM on our
   own GPUs — no per-token bill, no data egress) or `groq` (Groq Cloud API —
   fastest inference, zero GPU ops, per-token cost), each with a text and a vision
   model id. Both speak an OpenAI-compatible API, so the worker code is
   backend-agnostic; switching is a config/flag change, not a redeploy. Responses
   are cached by `(backend, model, task, content_hash)` in Redis so repeats are free.
7. **Assemble + validate.** The Result Assembler merges Stage 1 + Stage 2 into
   the canonical JSON (see §6), validates against the JSON Schema, and sets
   `confidence` = aggregate.
8. **Persist (fan-out).**
   - PostgreSQL: job status, per-post status, the canonical result row.
   - ClickHouse: a denormalized analytics row for fast aggregations/trends.
   - Qdrant: the embedding + key metadata for semantic search, clustering, dedup.
   - Object storage: raw payload + any generated reports.
9. **Serve.** `GET /analysis/{id}`, `GET /reports`, and dashboard queries read
   from PostgreSQL (point lookups) and ClickHouse (aggregations).

---

## 4. Service breakdown (backend)

| Service                     | Responsibility                                                     | Stack                                                  | Scaling unit        |
| --------------------------- | ------------------------------------------------------------------ | ------------------------------------------------------ | ------------------- |
| **API Gateway**             | TLS termination, routing, rate limiting, request size limits, CORS | NGINX / K8s Ingress (+ optional Kong)                  | replicas behind LB  |
| **Auth Service**            | API keys, JWT issue/verify, RBAC, per-tenant quotas                | FastAPI + PostgreSQL + Redis                           | stateless replicas  |
| **Ingestion Service**       | Validate, normalize, dedup, create job, enqueue                    | FastAPI (async)                                        | stateless replicas  |
| **Stage-1 NLP + Vision Workers** | Run small-model suite (text) **and the visual model on image posts** (`image_sentiment` + image description), micro-batched, emit features + confidence | Python (Ray/Celery consumer) + Triton/ONNX/CTranslate2; SigLIP/CLIP + light VLM for vision | GPU/CPU worker pool |
| **Router/Triage**           | Apply confidence gates + task flags; decide LLM/VLM routing        | lightweight Python service or in-worker rule module    | stateless           |
| **Stage-2 LLM/VLM Workers** | Selective summarization (text **and image-grounded via a VLM**) / insight / report / hard cases | Thin worker → local vLLM (LLM-A+LLM-B + a VLM) **or** Groq API (text + vision model) | GPU pool, stateless |
| **Result Assembler**        | Merge, JSON-schema validate, compute aggregate confidence          | Python consumer                                        | stateless replicas  |
| **Reporting/Query Service** | Read APIs, report generation, exports                              | FastAPI + ClickHouse + PostgreSQL                      | stateless replicas  |
| **User Management**         | Tenants, users, roles, billing/usage metering                      | FastAPI + PostgreSQL                                   | stateless replicas  |

Workers are split into **separate pools per stage** so the expensive LLM GPUs
scale independently from the cheap NLP fleet. The Stage-2 worker itself is a thin
client that calls the configured **LLM backend**: with the `local` backend it
talks to in-cluster vLLM servers (GPU-bound); with the `groq` backend it makes
outbound HTTPS calls to Groq and needs no GPU at all (a stateless pool that
scales on CPU/queue depth). The backend is selected by config and can be flipped
at runtime per the routing rules in §5.

---

## 5. The hybrid routing strategy (core cost control)

```text
                     post features + confidences (from Stage 1)
                                      │
                          ┌───────────▼───────────┐
                          │  Router decision tree  │
                          └───────────┬───────────┘
        ┌──────────────────────────────┼──────────────────────────────┐
        │ all required fields           │ low confidence OR             │ LLM-only task
        │ confident, no LLM task        │ ambiguous mixed-lang          │ requested
        ▼                               ▼                               ▼
   COMPLETE (no LLM)            LLM verify/refine               LLM generate
   ~90–95% of posts            small slice                     (summary/insight/report)
```

Levers that keep token usage and cost low:

- **Confidence gating.** Only uncertain posts reach the LLM. Tune thresholds per
  task from a labeled validation set.
- **Exact + near-duplicate caching.** Social feeds are highly repetitive
  (reshares, copypasta, viral captions). Hash + embedding dedup avoids reanalysis.
- **Batch & cluster summarization.** Don't summarize 10,000 posts individually.
  Cluster embeddings (Qdrant + k-means/HDBSCAN), then have the LLM summarize a
  _cluster_ or representative samples → "cluster summarization" and "insight
  generation" from one LLM call per cluster, not per post.
- **Token minimization.** Send only truncated, cleaned text and only the fields
  the LLM must produce. Use structured/JSON-mode output to avoid wasted tokens.
- **Two LLM roles, pluggable backend.** LLM-A (fast) absorbs per-post work; the
  larger LLM-B is reserved for low-volume cluster/report generation. Each role is
  served by the configured backend: `local` (vLLM, continuous batching — no
  per-token bill, no data egress) or `groq` (Groq Cloud — fastest inference, no
  GPU to run, billed per token). The backend is switchable at runtime, so the
  operator can run fully local for cost/privacy, burst to Groq under load, or mix
  (e.g. local LLM-A + Groq for LLM-B reports).
- **LLM response cache** keyed by `(backend, model, task, content_hash)`.

Expected outcome: LLM touches a single-digit-to-low-double-digit percentage of
posts, and the per-batch LLM bill is dominated by _cluster-level_ generation,
not per-post calls. Quantified in [cost_estimation.md](cost_estimation.md).

---

## 6. Canonical output JSON

**The unit of analysis is a _post together with its comment thread_, not an
isolated post.** A post (upstream Post API) is joined to N comments/replies pulled
from the Comment API by the post's unique `id` (see
[data_contract.md](data_contract.md)). The smart layer analyzes the whole thread
and emits one JSON object per thread, ready for a downstream service to consume.

Field provenance: `post_id`/`campaign_id`/`platform_post_id`/`media_type`/
`baseline_sentiment`/`baseline_viral_potential` come **from the upstream Post API**
(verbatim or derived); `platform` is **derived from the URL host**; everything
else is **computed by this service**. The upstream coarse sentiment is kept as
`baseline_sentiment` while `overall_sentiment`/`sentiment_score` are our recomputed
values — a **fusion of `text_sentiment` (caption) and `image_sentiment` (the
photo)** per [data_contract.md](data_contract.md) §4. `image_sentiment` and
`image_analysis` are `null` for text-only posts; `text_sentiment` is `null` when
the `caption` is `null`.

The assembler emits and validates this schema. `post_summary` is **grounded on the
caption + OCR + image** (`post_summary_grounding` records which) and written **in
the post's own language** (Bangla post → Bangla summary; English → English; the
language is detected, never forced). Banglish (romanized Bangla) is normalized to
the dominant language.

```json
{
  "post_id": "cmq7phcmplnt00xmpl0a1b2cx",
  "campaign_id": "cmpgrn1cmplnt0xmpl0camp01",
  "platform": "facebook",
  "platform_post_id": "1402233557981288",
  "url": "https://www.facebook.com/...",
  "author": null,
  "media_type": "PHOTO_TEXT",
  "language": "bn",
  "language_mix": ["bn", "banglish", "en"],
  "language_confidence": 0.97,
  "post_type": "complaint",
  "post_summary": "গ্রিন গার্ডেন ও ট্রান্সপোর্টে খাবারের দাম বাইরের তুলনায় অনেক বেশি — একটি সিঙ্গারা ২০ টাকা; পোস্টদাতা কেনা বন্ধ করার ও বয়কটের ডাক দিয়েছেন। ছবিতে দামের তালিকা দেখা যাচ্ছে।",
  "post_summary_lang": "bn",
  "post_summary_grounding": ["caption", "ocr", "image"],
  "overall_sentiment": "negative",
  "sentiment_score": -0.64,
  "text_sentiment": { "label": "negative", "score": -0.7 },
  "image_sentiment": { "label": "neutral", "score": -0.1, "per_image": [-0.1] },
  "baseline_sentiment": -0.3,
  "baseline_viral_potential": 0.25,
  "emotion": "anger",
  "intents": ["complaint", "call_to_action"],
  "topics": ["food pricing", "campus transport", "boycott"],
  "entities": [
    { "type": "organization", "value": "Green Garden", "confidence": 0.94 },
    { "type": "product", "value": "singara", "confidence": 0.82 }
  ],
  "brand_mentions": [
    { "name": "Green Garden", "sentiment": "negative", "mentions": 9 }
  ],
  "keywords": ["দাম", "সিঙ্গারা", "boycott", "transport"],
  "toxicity_score": 0.07,
  "hate_speech_score": 0.01,
  "engagement": { "reactions": 48, "comment_count": 12 },
  "image_analysis": {
    "image_count": 1,
    "ocr_text": "মেনু: সিঙ্গারা ২০৳ ...",
    "description": "a printed café price list / menu board",
    "images": [
      { "ref": "photoUrls[0]", "sentiment": { "label": "neutral", "score": -0.1 }, "ocr_text": "মেনু: সিঙ্গারা ২০৳ ...", "description": "a printed café price list / menu board" }
    ],
    "vision_model": "SigLIP (sentiment) + Qwen2.5-VL-7B (description)"
  },
  "comment_analysis": {
    "analyzed": 12,
    "sentiment_breakdown": { "positive": 1, "negative": 9, "neutral": 2 },
    "themes": [
      "prices far above market",
      "same quality cheaper outside",
      "calls to boycott"
    ],
    "representative_comments": [
      {
        "author": "Anonymous participant 558",
        "lang": "bn",
        "sentiment": "negative",
        "text": "নুনুর গার্ডেনে এক প্লেট ভাতের দাম ২০ টাকা, বাইরে ৫ টাকা"
      }
    ]
  },
  "post_summary_source": "vlm",
  "confidence": 0.92,
  "processing": {
    "unit": "post+thread",
    "stage1_ms": 58,
    "llm_used": true,
    "llm_role": "LLM-A",
    "llm_backend": "local",
    "llm_model": "Qwen2.5-7B-Instruct",
    "vision_used": true,
    "vision_model": "Qwen2.5-VL-7B-Instruct",
    "model_versions": {}
  },
  "upstream_status": "NOT_ANALYZED",
  "created_at": "2026-06-10T05:47:00",
  "scraped_at": "2026-06-10T06:26:40.838"
}
```

Notes:

- **`sentiment_analysis` the user asked for** is **multimodal**:
  `text_sentiment` (caption), `image_sentiment` (the photo, via a visual model),
  and the **fused** post-level `overall_sentiment` + `sentiment_score` — all
  **recomputed** by us — plus `comment_analysis.sentiment_breakdown` across the
  thread. `image_sentiment`/`image_analysis` are `null` for text-only posts;
  `text_sentiment` is `null` for `null`-caption posts. The upstream's coarse score
  is preserved as `baseline_sentiment` (and `baseline_viral_potential`) — we never
  overwrite it (see [data_contract.md](data_contract.md) §4).
- **`image_analysis`** holds per-image visual `sentiment`, `ocr_text` (from upstream
  `photoOcrTexts`), and a short `description` used to ground the summary. It is the
  bridge between the image and `post_summary`.
- **`media_type`** is the upstream `postType` (TEXT/PHOTO/PHOTO_TEXT/UNKNOWN) and
  is **distinct** from the semantic `post_type` (complaint/promotion/…).
- `post_summary` is **grounded on caption + OCR + image** (`post_summary_grounding`
  lists which); `post_summary_lang` records the original language. `post_summary_source`
  is `vlm` when a vision-language model produced it, `llm` for text-only.
- `post_type`, `intents`, `brand_mentions`, and `comment_analysis.themes` are the
  "and something like that" fields — useful structured signal for downstream use.
- `post_summary_source` and `processing.llm_used`/`llm_role`/`llm_backend`/`llm_model`
  make the hybrid behavior auditable (did we call an LLM, which role, on which
  backend — `local` or `groq` — which concrete model, was it worth it).
- All fields except `post_summary`, `comment_analysis.themes`, and
  `representative_comments` come from cheap Stage-1 NLP; the LLM fills only the
  generative fields when the router asks for them.
- The flat schema the owner sketched (`post_id, platform, language, sentiment,
emotion, topics, entities, keywords, toxicity_score, summary, confidence,
created_at`) is preserved as a subset of this richer object (`sentiment` →
  `overall_sentiment`, `summary` → `post_summary`), and the **real upstream**
  fields (`id, campaignId, platformPostId, url, caption, postType, sentiment,
  viralPotential, …`) map in per [data_contract.md](data_contract.md) §5.

---

## 7. Technology stack (recommended)

| Layer          | Choice                                                       | Why                                                                      |
| -------------- | ------------------------------------------------------------ | ------------------------------------------------------------------------ |
| API services   | **Python + FastAPI** (async)                                 | Matches team skills; great for I/O-bound APIs and ML glue                |
| Workers        | **Python**, Celery or Ray for orchestration                  | Native ML ecosystem; Ray scales to multi-node cleanly                    |
| Model serving  | **Triton/ONNX/CTranslate2** (NLP); **SigLIP/CLIP + VLM** (vision); Stage-2 **vLLM**⇄**Groq** | High GPU utilization for NLP; cheap visual sentiment + a VLM for image-grounded summaries; Stage-2 backend switchable local↔Groq |
| Message bus    | **Kafka** (prod), **Redis Streams** (MVP)                    | Durable, partitioned, replayable at scale; simple to start               |
| Operational DB | **PostgreSQL**                                               | ACID jobs/state, JSONB flexibility, mature                               |
| Analytics DB   | **ClickHouse**                                               | Columnar, billions of rows, sub-second aggregations for trends           |
| Vector DB      | **Qdrant**                                                   | Fast, open-source, easy ops, good filtering; for dedup/search/clustering |
| Cache          | **Redis**                                                    | LLM/embedding/query cache, dedup set, rate limits                        |
| Object storage | **S3 / MinIO**                                               | Raw payloads, reports, model artifacts                                   |
| Orchestration  | **Kubernetes** (prod), **Docker Compose** (MVP)              | Autoscaling + HA vs simplicity                                           |
| Autoscaling    | **KEDA** (scale on queue depth) + HPA                        | Workers track backlog, not just CPU                                      |
| Observability  | **Prometheus + Grafana + Loki + OpenTelemetry + Jaeger**     | Metrics, logs, traces — see [infrastructure.md](infrastructure.md)       |
| Frontend       | **Flutter**                                                  | Matches team skills; one codebase for web/mobile dashboard               |

Rationale for each data-layer pick and the alternatives rejected is in
[possible_architecture.md](possible_architecture.md).

---

## 8. Reliability & fault tolerance

- **Retries with backoff** at every worker; transient failures (OOM, GPU hiccup,
  local vLLM 5xx, or Groq 429/5xx) retried up to N times. Groq rate-limit (429)
  responses honor `Retry-After` and back off per-key.
- **Dead-letter queue (DLQ).** Messages that exhaust retries go to a DLQ topic
  with the error context for inspection/replay.
- **Idempotency.** Content hash + job id make reprocessing safe; assembler
  upserts.
- **At-least-once delivery** from the bus + idempotent writes = no lost or
  double-counted posts.
- **Graceful degradation + backend failover.** If the active backend is saturated
  or unhealthy, the router can (a) queue, (b) degrade LLM-B work to LLM-A (lower
  quality), (c) **fail over to the other backend** — local↔Groq — when both are
  configured, or (d) return Stage-1-only results flagged
  `summary_source: "skipped"`. Failover is policy-driven: a privacy-locked tenant
  can be pinned to `local` and will never spill to Groq even under load (it
  degrades to Stage-1-only instead). Never block the whole batch.
- **Health/readiness probes** on every service; circuit breakers around each LLM
  backend (per local vLLM server and around the Groq endpoint).

---

## 9. Security considerations

- **Transport:** TLS everywhere (ingress + mTLS between services if a mesh is
  used).
- **AuthN/Z:** API keys for machine clients, JWT + RBAC for users; per-tenant
  isolation and quotas in the Auth service.
- **Rate limiting & quotas** at the gateway (per key/tenant) to prevent abuse and
  runaway cost.
- **Input hardening:** size caps, schema validation, content sanitization;
  treat post text as untrusted (prompt-injection-aware when building LLM prompts
  — never let post content alter system instructions).
- **PII handling:** social content contains personal data. Encrypt at rest
  (DB + object storage), encrypt in transit, support per-tenant data retention /
  deletion (GDPR-style), and access logging.
- **LLM backend data residency.** The `local` backend keeps all post/comment text
  inside the cluster (no egress). The `groq` backend **sends prompt content to a
  third party (Groq)** over TLS — a deliberate trade for speed/elasticity. Make it
  an explicit, audited choice: default to `local`, allow `groq` per-tenant/per-job
  only where the data classification permits it, and **pin privacy-sensitive
  tenants to `local`** so a runtime switch can never route their PII to Groq.
  Record the backend used on every result (`processing.llm_backend`) for audit.
- **Secrets:** Kubernetes Secrets / Vault; no secrets in images or env files —
  including the **Groq API key**, which is mounted only into Stage-2 workers.
- **Network:** private subnets for DBs and model servers; only the gateway is
  internet-facing. NetworkPolicies in K8s.
- **Audit logging** of admin and data-access actions.
- **Supply chain:** pinned dependencies, image scanning, signed images.

---

## 10. Performance optimization strategy

- **Micro-batching** on GPU workers (Triton dynamic batching / vLLM continuous
  batching) → high GPU utilization, low cost per post.
- **Model right-sizing & quantization** (INT8/FP16, ONNX/CTranslate2) for the
  NLP fleet; quantized (AWQ/GPTQ) LLM for serving.
- **One pass, many tasks.** Share a single transformer encoder pass across
  multiple classification heads where possible instead of N separate models.
- **Caching at every layer** (dedup, embeddings, LLM responses, query results).
- **Cluster-level LLM work** instead of per-post.
- **Queue partitioning** sized to worker concurrency; keep partitions ≥ consumers.
- **Async I/O** in all API/ingestion services.
- **Backpressure** via bounded queues; autoscale on lag, not CPU.

See [infrastructure.md](infrastructure.md) for GPU sizing and the monitoring
that drives these decisions, and [plan.md](plan.md) for the rollout order.

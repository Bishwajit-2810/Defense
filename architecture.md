# Architecture — Social Media "Smart Layer" Microservice

This is the **recommended** architecture. For the alternatives that were weighed
to arrive here, see [possible_architecture.md](possible_architecture.md). For
model choices see [models.md](models.md), for cost see
[cost_estimation.md](cost_estimation.md), for real input→output examples see
[examples.md](examples.md).

## 0. What this service is

A **scraper** feeds 1,000+ real-time Bangla/English/Banglish posts (Facebook,
Instagram, …), **each with its comment thread**, into this system. The service is
a **smart, self-contained microservice** — a "thinking layer" — that:

- takes a post **and its comments** as input,
- decides _per item_ how much intelligence each one needs (the cheap NLP models
  vs. an LLM — this routing is the "smart" part), and
- returns one **structured JSON** object per thread (summary in the post's own
  language, sentiment, topics, intents, entities, brand mentions, comment
  analysis — see §6) that downstream projects consume directly.

Hard product constraints from the owner: **fast**, **cost-effective with no
external/paid API** (two local open-source LLMs only — see [models.md](models.md)
§2), **efficient**, and **accurate on Bangla and Banglish** (with fine-tuning
hooks for when accuracy must improve). It must **scale** horizontally to handle
batches of 1k → 10k → 100k threads.

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
                              │       │ STAGE 2 — LLM   │  2 local vLLM models
                              │       │ workers         │  LLM-A fast / LLM-B qual
                              │       │ summarize·insight│  (no external API)
                              │       │ ·report·hard NER │
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

1. **Upload.** The scraper calls `POST /posts/upload` with a batch of **threads**
   (each = a parent post + its nested comments/replies), or a presigned link to a
   large JSONL file in object storage. Ingestion validates schema, normalizes text
   (Unicode NFC, emoji handling, Bangla/English/Banglish script tagging),
   flattens the comment tree to an ordered list, and computes a content hash over
   the post + comments.
2. **Dedup gate.** Hash is checked against Redis (recent) and Qdrant/Postgres
   (historical). Exact duplicates short-circuit to the cached result; near
   duplicates (cosine > threshold on embedding) can reuse prior analysis. This
   alone removes a large fraction of LLM/NLP work on real social feeds.
3. **Enqueue.** A `job` row is created in PostgreSQL (`status=queued`). One
   message per post is produced to the bus, partitioned by `post_id` hash so a
   post's ordering is stable and load spreads evenly.
4. **Stage 1 — Fast NLP (parallel).** Worker pulls a batch (micro-batching) and
   runs the small-model suite in one GPU/CPU pass over the **post and each
   comment**: language/Banglish detection → sentiment, emotion, topic, intent,
   toxicity/hate, NER, keyword extraction, and a sentence embedding. Per-comment
   sentiment is aggregated into the thread's `sentiment_breakdown`. Each output
   carries a **confidence** score. Results are written to a partial-result store.
5. **Router/Triage (the "smart thinking layer").** For each thread the router
   decides:
   - All required fields produced with confidence ≥ threshold, and no LLM-only
     task requested → **mark complete**, skip Stage 2.
   - A `post_summary` is requested, or confidence is below threshold, or the
     thread is ambiguous / heavily Banglish / long → **route to Stage 2** with a
     compact, token-minimized prompt (post + a representative/clustered subset of
     comments, truncated). This selectivity is what keeps the service fast and
     cheap.
6. **Stage 2 — LLM (selective).** Two self-hosted local models on vLLM produce
   summaries, insights, refined labels — **LLM-A** (fast 7B/8B) for per-post
   refinement and short summaries, **LLM-B** (large 14B/32B) for cluster
   summarization, insight, and report generation. No external/paid API is used.
   Responses are cached by `(model, task, content_hash)` in Redis so repeats are
   free.
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
| **Stage-1 NLP Workers**     | Run small-model suite, micro-batched, emit features + confidence   | Python (Ray/Celery consumer) + Triton/ONNX/CTranslate2 | GPU/CPU worker pool |
| **Router/Triage**           | Apply confidence gates + task flags; decide LLM routing            | lightweight Python service or in-worker rule module    | stateless           |
| **Stage-2 LLM Workers**     | Selective summarization / insight / report / hard cases            | 2 local vLLM servers (LLM-A + LLM-B) + thin worker     | GPU worker pool     |
| **Result Assembler**        | Merge, JSON-schema validate, compute aggregate confidence          | Python consumer                                        | stateless replicas  |
| **Reporting/Query Service** | Read APIs, report generation, exports                              | FastAPI + ClickHouse + PostgreSQL                      | stateless replicas  |
| **User Management**         | Tenants, users, roles, billing/usage metering                      | FastAPI + PostgreSQL                                   | stateless replicas  |

Workers are split into **separate pools per stage** so the expensive LLM GPUs
scale independently from the cheap NLP fleet.

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
- **Two local LLMs, no API.** Both Stage-2 models run on owned/cloud GPUs via
  vLLM with continuous batching — there is no per-token API bill and no data
  egress. The cheap LLM-A absorbs per-post work; the larger LLM-B is reserved for
  low-volume cluster/report generation.
- **LLM response cache** keyed by `(model, task, content_hash)`.

Expected outcome: LLM touches a single-digit-to-low-double-digit percentage of
posts, and the per-batch LLM bill is dominated by _cluster-level_ generation,
not per-post calls. Quantified in [cost_estimation.md](cost_estimation.md).

---

## 6. Canonical output JSON

**The unit of analysis is a _post together with its comment thread_, not an
isolated post.** A scraped item is a parent post plus N nested comments/replies
(author, text, reactions, timestamp). The smart layer analyzes the whole thread
and emits one JSON object per thread, ready for a downstream service to consume.

The assembler emits and validates this schema. `post_summary` is written **in the
post's own language** (Bangla post → Bangla summary; English post → English
summary; the language is detected, never forced). Banglish (romanized Bangla) is
normalized to the dominant language for the summary.

```json
{
  "post_id": "fb_12345",
  "platform": "facebook",
  "url": "https://facebook.com/...",
  "author": "TalentedOstrich6332",
  "language": "bn",
  "language_mix": ["bn", "banglish", "en"],
  "language_confidence": 0.97,
  "post_type": "complaint",
  "post_summary": "গ্রিন গার্ডেন ও ট্রান্সপোর্টে খাবারের দাম বাইরের তুলনায় অনেক বেশি — একটি সিঙ্গারা ২০ টাকা; পোস্টদাতা কেনা বন্ধ করার ও বয়কটের ডাক দিয়েছেন।",
  "post_summary_lang": "bn",
  "overall_sentiment": "negative",
  "sentiment_score": -0.61,
  "emotion": "anger",
  "intents": ["complaint", "call_to_action"],
  "topics": ["food pricing", "campus transport", "boycott"],
  "entities": [
    { "type": "organization", "value": "Green Garden", "confidence": 0.93 },
    { "type": "product", "value": "singara", "confidence": 0.81 }
  ],
  "brand_mentions": [
    { "name": "Green Garden", "sentiment": "negative", "mentions": 9 }
  ],
  "keywords": ["দাম", "সিঙ্গারা", "boycott", "transport"],
  "toxicity_score": 0.07,
  "hate_speech_score": 0.01,
  "engagement": { "reactions": 48, "comment_count": 12 },
  "comment_analysis": {
    "analyzed": 12,
    "sentiment_breakdown": { "positive": 1, "negative": 9, "neutral": 2 },
    "themes": ["prices far above market", "same quality cheaper outside", "calls to boycott"],
    "representative_comments": [
      { "author": "Anonymous participant 558", "lang": "bn", "sentiment": "negative",
        "text": "নুনুর গার্ডেনে এক প্লেট ভাতের দাম ২০ টাকা, বাইরে ৫ টাকা" }
    ]
  },
  "post_summary_source": "llm",
  "confidence": 0.92,
  "processing": {
    "unit": "post+thread",
    "stage1_ms": 58,
    "llm_used": true,
    "llm_model": "LLM-B",
    "model_versions": {}
  },
  "created_at": "2026-06-01T10:00:00Z"
}
```

Notes:

- **`sentiment_analysis` the user asked for** maps to `overall_sentiment` +
  `sentiment_score` (post-level) and `comment_analysis.sentiment_breakdown`
  (positive/negative/neutral counts across the thread).
- `post_summary` + `post_summary_lang` capture "summary in the original language."
- `post_type`, `intents`, `brand_mentions`, and `comment_analysis.themes` are the
  "and something like that" fields — useful structured signal for downstream use.
- `post_summary_source` and `processing.llm_used`/`llm_model` make the hybrid
  behavior auditable (did we call an LLM, which one, was it worth it).
- All fields except `post_summary`, `comment_analysis.themes`, and
  `representative_comments` come from cheap Stage-1 NLP; the LLM fills only the
  generative fields when the router asks for them.

---

## 7. Technology stack (recommended)

| Layer          | Choice                                                        | Why                                                                      |
| -------------- | ------------------------------------------------------------- | ------------------------------------------------------------------------ |
| API services   | **Python + FastAPI** (async)                                  | Matches team skills; great for I/O-bound APIs and ML glue                |
| Workers        | **Python**, Celery or Ray for orchestration                   | Native ML ecosystem; Ray scales to multi-node cleanly                    |
| Model serving  | **Triton / ONNX / CTranslate2** (NLP) + **vLLM x2 local**     | High GPU utilization, dynamic batching; all local, no API                |
| Message bus    | **Kafka** (prod), **Redis Streams** (MVP)                     | Durable, partitioned, replayable at scale; simple to start               |
| Operational DB | **PostgreSQL**                                                | ACID jobs/state, JSONB flexibility, mature                               |
| Analytics DB   | **ClickHouse**                                                | Columnar, billions of rows, sub-second aggregations for trends           |
| Vector DB      | **Qdrant**                                                    | Fast, open-source, easy ops, good filtering; for dedup/search/clustering |
| Cache          | **Redis**                                                     | LLM/embedding/query cache, dedup set, rate limits                        |
| Object storage | **S3 / MinIO**                                                | Raw payloads, reports, model artifacts                                   |
| Orchestration  | **Kubernetes** (prod), **Docker Compose** (MVP)               | Autoscaling + HA vs simplicity                                           |
| Autoscaling    | **KEDA** (scale on queue depth) + HPA                         | Workers track backlog, not just CPU                                      |
| Observability  | **Prometheus + Grafana + Loki + OpenTelemetry + Jaeger**      | Metrics, logs, traces — see [infrastructure.md](infrastructure.md)       |
| Frontend       | **Flutter**                                                   | Matches team skills; one codebase for web/mobile dashboard               |

Rationale for each data-layer pick and the alternatives rejected is in
[possible_architecture.md](possible_architecture.md).

---

## 8. Reliability & fault tolerance

- **Retries with backoff** at every worker; transient failures (OOM, GPU hiccup,
  API 5xx) retried up to N times.
- **Dead-letter queue (DLQ).** Messages that exhaust retries go to a DLQ topic
  with the error context for inspection/replay.
- **Idempotency.** Content hash + job id make reprocessing safe; assembler
  upserts.
- **At-least-once delivery** from the bus + idempotent writes = no lost or
  double-counted posts.
- **Graceful degradation (all local).** If the LLMs are saturated, the router can
  (a) queue, (b) degrade LLM-B work to LLM-A (lower quality, still local), or
  (c) return Stage-1-only results flagged `summary_source: "skipped"` — never
  block the whole batch and never fall out to a third-party API.
- **Health/readiness probes** on every service; circuit breakers around each LLM.

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
- **Secrets:** Kubernetes Secrets / Vault; no secrets in images or env files.
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

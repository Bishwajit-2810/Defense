# Master Plan — Social Media "Smart Layer" Microservice

A single, self-contained master document for the social-media analysis smart
layer. It consolidates the whole design — overview, architecture, alternatives,
models, infrastructure, cost, API, worked examples, deployment, and the phased
roadmap — into one source of truth.

---

## Table of contents

1. [What this service is](#1-what-this-service-is)
2. [TL;DR recommendation](#2-tldr-recommendation)
3. [Design principles](#3-design-principles)
4. [High-level architecture](#4-high-level-architecture)
5. [Data flow (end to end)](#5-data-flow-end-to-end)
6. [Service breakdown](#6-service-breakdown)
7. [The hybrid routing strategy (core cost control)](#7-the-hybrid-routing-strategy-core-cost-control)
8. [Canonical output JSON](#8-canonical-output-json)
9. [Technology stack](#9-technology-stack)
10. [Reliability & fault tolerance](#10-reliability--fault-tolerance)
11. [Security](#11-security)
12. [Performance optimization](#12-performance-optimization)
13. [Possible architectures & tradeoffs](#13-possible-architectures--tradeoffs)
14. [AI model selection, RAG & fine-tuning](#14-ai-model-selection-rag--fine-tuning)
15. [Infrastructure — GPU, monitoring, caching, scaling](#15-infrastructure--gpu-monitoring-caching-scaling)
16. [Cost estimation](#16-cost-estimation)
17. [API design](#17-api-design)
18. [Worked examples](#18-worked-examples)
19. [Deployment](#19-deployment)
20. [Implementation plan & roadmap](#20-implementation-plan--roadmap)
21. [Best practices for 10,000+ posts](#21-best-practices-for-10000-posts)
22. [Risk register](#22-risk-register)

---

## 1. What this service is

A **scraper** feeds 1,000+ real-time Bangla/English/Banglish posts (Facebook,
Instagram, …), **each with its comment thread**, into this system. The service is
a **smart, self-contained microservice** — a "thinking layer" — that:

- takes a post **and its comments** as input,
- decides _per item_ how much intelligence each one needs (cheap NLP models vs. an
  LLM — this routing is the "smart" part), and
- returns one **structured JSON** object per thread (summary in the post's own
  language, sentiment, topics, intents, entities, brand mentions, comment
  analysis — see §8) that downstream projects consume directly.

Hard product constraints from the owner: **fast**, **cost-effective with no
external/paid API** (two local open-source LLMs only — see §14), **efficient**,
and **accurate on Bangla and Banglish** (with fine-tuning hooks for when accuracy
must improve). It must **scale** horizontally to handle batches of 1k → 10k →
100k threads.

**The unit of analysis is a _post together with its comment thread_, not an
isolated post.** A scraped item is a parent post plus N nested comments/replies
(author, text, reactions, timestamp). The smart layer analyzes the whole thread
and emits one JSON object per thread.

---

## 2. TL;DR recommendation

- **Unit of analysis = post + its comment thread.** The service analyzes the whole
  thread and emits one JSON object per thread (see §8).
- **Hybrid analysis pipeline.** Cheap, fast NLP models (fastText, transformer
  classifiers, spaCy/GLiNER) run over the post and every comment and handle
  ~90–95% of the work. An LLM is invoked **selectively** only for the
  original-language summary, insight, and low-confidence/ambiguous cases. This is
  the central cost-control idea.
- **Two local LLMs, no external/paid API.** The selective stage runs entirely on
  self-hosted models: **LLM-A** (fast 7B/8B) for per-post refinement and **LLM-B**
  (larger 14B/32B) for cluster summarization, insight, and grounded reports.
  Everything stays on our own GPUs — no per-token bill, no data egress.
- **Queue-based, horizontally scalable.** API → ingestion → message bus →
  stateless GPU/CPU workers → result store. Workers scale independently per stage.
- **Queue: Kafka for Production/Enterprise, Redis Streams for the MVP.** Start
  simple, migrate when throughput and replay/retention demand it.
- **Storage split by access pattern:** PostgreSQL (operational + jobs), ClickHouse
  (analytics/aggregations), Qdrant (vectors/semantic search/dedup), Redis (cache +
  dedup + rate limits), object storage (raw payloads + reports).
- **Deployment:** Docker Compose for MVP, Kubernetes (with KEDA autoscaling on
  queue depth) for Production and beyond.

---

## 3. Design principles

1. **Hybrid intelligence, not LLM-everywhere.** The requirement explicitly forbids
   sending every post to an LLM. Small specialized models are 100–1000× cheaper and
   10–100× faster per post. The LLM is a _scalpel_, not a _firehose_: it runs only
   for tasks small models can't do well (summarization, cross-post insight,
   reports) or when a small model is uncertain.
2. **Queue-based and stage-decoupled.** Every heavy stage is a stateless worker
   pool fed by a durable queue. Backpressure, retries, and independent scaling come
   for free.
3. **Scale by partition, not by bigger machines.** Throughput grows by adding
   workers and queue partitions, not vertical scaling.
4. **Storage by access pattern.** No single database is good at OLTP + aggregation
   and vector search. Use the right store per job.
5. **Idempotent + observable.** Every post has a stable hash; reprocessing is safe.
   Every stage emits metrics, logs, and traces.

---

## 4. High-level architecture

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
strategy — see §7.

---

## 5. Data flow (end to end)

1. **Upload.** The scraper calls `POST /v1/posts/upload` with a batch of
   **threads** (each = a parent post + its nested comments/replies), or a presigned
   link to a large JSONL file in object storage. Ingestion validates schema,
   normalizes text (Unicode NFC, emoji handling, Bangla/English/Banglish script
   tagging), flattens the comment tree to an ordered list, and computes a content
   hash over the post + comments.
2. **Dedup gate.** Hash is checked against Redis (recent) and Qdrant/Postgres
   (historical). Exact duplicates short-circuit to the cached result; near
   duplicates (cosine > threshold on embedding) can reuse prior analysis. This
   alone removes a large fraction of LLM/NLP work on real social feeds.
3. **Enqueue.** A `job` row is created in PostgreSQL (`status=queued`). One message
   per post is produced to the bus, partitioned by `post_id` hash so a post's
   ordering is stable and load spreads evenly.
4. **Stage 1 — Fast NLP (parallel).** Worker pulls a batch (micro-batching) and
   runs the small-model suite in one GPU/CPU pass over the **post and each
   comment**: language/Banglish detection → sentiment, emotion, topic, intent,
   toxicity/hate, NER, keyword extraction, and a sentence embedding. Per-comment
   sentiment is aggregated into the thread's `sentiment_breakdown`. Each output
   carries a **confidence** score. Results are written to a partial-result store.
5. **Router/Triage (the "smart thinking layer").** For each thread the router
   decides:
   - All required fields produced with confidence ≥ threshold, and no LLM-only task
     requested → **mark complete**, skip Stage 2.
   - A `post_summary` is requested, or confidence is below threshold, or the thread
     is ambiguous / heavily Banglish / long → **route to Stage 2** with a compact,
     token-minimized prompt (post + a representative/clustered subset of comments,
     truncated). This selectivity is what keeps the service fast and cheap.
6. **Stage 2 — LLM (selective).** Two self-hosted local models on vLLM produce
   summaries, insights, refined labels — **LLM-A** (fast 7B/8B) for per-post
   refinement and short summaries, **LLM-B** (large 14B/32B) for cluster
   summarization, insight, and report generation. No external/paid API is used.
   Responses are cached by `(model, task, content_hash)` in Redis so repeats are
   free.
7. **Assemble + validate.** The Result Assembler merges Stage 1 + Stage 2 into the
   canonical JSON (see §8), validates against the JSON Schema, and sets
   `confidence` = aggregate.
8. **Persist (fan-out).**
   - PostgreSQL: job status, per-post status, the canonical result row.
   - ClickHouse: a denormalized analytics row for fast aggregations/trends.
   - Qdrant: the embedding + key metadata for semantic search, clustering, dedup.
   - Object storage: raw payload + any generated reports.
9. **Serve.** `GET /analysis/{id}`, `GET /reports`, and dashboard queries read from
   PostgreSQL (point lookups) and ClickHouse (aggregations).

---

## 6. Service breakdown

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

## 7. The hybrid routing strategy (core cost control)

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
  _cluster_ or representative samples → one LLM call per cluster, not per post.
- **Token minimization.** Send only truncated, cleaned text and only the fields the
  LLM must produce. Use structured/JSON-mode output to avoid wasted tokens.
- **Two local LLMs, no API.** Both Stage-2 models run on owned/cloud GPUs via vLLM
  with continuous batching — no per-token API bill and no data egress. The cheap
  LLM-A absorbs per-post work; the larger LLM-B is reserved for low-volume
  cluster/report generation.
- **LLM response cache** keyed by `(model, task, content_hash)`.

Expected outcome: LLM touches a single-digit-to-low-double-digit percentage of
posts, and the per-batch LLM bill is dominated by _cluster-level_ generation, not
per-post calls.

---

## 8. Canonical output JSON

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
  "sentiment_score": -0.64,
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
  "post_summary_source": "llm",
  "confidence": 0.92,
  "processing": {
    "unit": "post+thread",
    "stage1_ms": 58,
    "llm_used": true,
    "llm_model": "LLM-A",
    "model_versions": {}
  },
  "created_at": "2026-06-01T10:00:00Z"
}
```

Notes:

- **`sentiment_analysis` the owner asked for** maps to `overall_sentiment` +
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

The flat schema the owner sketched (`post_id, platform, language, sentiment,
emotion, topics, entities, keywords, toxicity_score, summary, confidence,
created_at`) is preserved as a subset of the above richer object.

---

## 9. Technology stack

| Layer          | Choice                                                    | Why                                                                      |
| -------------- | --------------------------------------------------------- | ------------------------------------------------------------------------ |
| API services   | **Python + FastAPI** (async)                              | Matches team skills; great for I/O-bound APIs and ML glue                |
| Workers        | **Python**, Celery or Ray for orchestration               | Native ML ecosystem; Ray scales to multi-node cleanly                    |
| Model serving  | **Triton / ONNX / CTranslate2** (NLP) + **vLLM x2 local** | High GPU utilization, dynamic batching; all local, no API                |
| Message bus    | **Kafka** (prod), **Redis Streams** (MVP)                 | Durable, partitioned, replayable at scale; simple to start               |
| Operational DB | **PostgreSQL**                                            | ACID jobs/state, JSONB flexibility, mature                               |
| Analytics DB   | **ClickHouse**                                            | Columnar, billions of rows, sub-second aggregations for trends           |
| Vector DB      | **Qdrant**                                                | Fast, open-source, easy ops, good filtering; for dedup/search/clustering |
| Cache          | **Redis**                                                 | LLM/embedding/query cache, dedup set, rate limits                        |
| Object storage | **S3 / MinIO**                                            | Raw payloads, reports, model artifacts                                   |
| Orchestration  | **Kubernetes** (prod), **Docker Compose** (MVP)           | Autoscaling + HA vs simplicity                                           |
| Autoscaling    | **KEDA** (scale on queue depth) + HPA                     | Workers track backlog, not just CPU                                      |
| Observability  | **Prometheus + Grafana + Loki + OpenTelemetry + Jaeger**  | Metrics, logs, traces                                                    |
| Frontend       | **Flutter**                                               | Matches team skills; one codebase for web/mobile dashboard               |

---

## 10. Reliability & fault tolerance

- **Retries with backoff** at every worker; transient failures (OOM, GPU hiccup)
  retried up to N times.
- **Dead-letter queue (DLQ).** Messages that exhaust retries go to a DLQ topic with
  the error context for inspection/replay.
- **Idempotency.** Content hash + job id make reprocessing safe; assembler upserts.
- **At-least-once delivery** from the bus + idempotent writes = no lost or
  double-counted posts.
- **Graceful degradation (all local).** If the LLMs are saturated, the router can
  (a) queue, (b) degrade LLM-B work to LLM-A (lower quality, still local), or
  (c) return Stage-1-only results flagged `summary_source: "skipped"` — never block
  the whole batch and never fall out to a third-party API.
- **Health/readiness probes** on every service; circuit breakers around each LLM.

---

## 11. Security

- **Transport:** TLS everywhere (ingress + mTLS between services if a mesh is used).
- **AuthN/Z:** API keys for machine clients, JWT + RBAC for users; per-tenant
  isolation and quotas in the Auth service.
- **Rate limiting & quotas** at the gateway (per key/tenant) to prevent abuse and
  runaway cost.
- **Input hardening:** size caps, schema validation, content sanitization; treat
  post text as untrusted (prompt-injection-aware when building LLM prompts — never
  let post content alter system instructions).
- **PII handling:** social content contains personal data. Encrypt at rest (DB +
  object storage), encrypt in transit, support per-tenant data retention / deletion
  (GDPR-style), and access logging.
- **Secrets:** Kubernetes Secrets / Vault; no secrets in images or env files.
- **Network:** private subnets for DBs and model servers; only the gateway is
  internet-facing. NetworkPolicies in K8s.
- **Audit logging** of admin and data-access actions.
- **Supply chain:** pinned dependencies, image scanning, signed images.

---

## 12. Performance optimization

- **Micro-batching** on GPU workers (Triton dynamic batching / vLLM continuous
  batching) → high GPU utilization, low cost per post.
- **Model right-sizing & quantization** (INT8/FP16, ONNX/CTranslate2) for the NLP
  fleet; quantized (AWQ/GPTQ) LLM for serving.
- **One pass, many tasks.** Share a single transformer encoder pass across multiple
  classification heads where possible instead of N separate models.
- **Caching at every layer** (dedup, embeddings, LLM responses, query results).
- **Cluster-level LLM work** instead of per-post.
- **Queue partitioning** sized to worker concurrency; keep partitions ≥ consumers.
- **Async I/O** in all API/ingestion services.
- **Backpressure** via bounded queues; autoscale on lag, not CPU.

---

## 13. Possible architectures & tradeoffs

Records the major options considered for each decision point and why the
recommended choice won. Use it to revisit a decision if constraints change.
Context for every choice: a **self-hosted microservice** ingesting
post+comment threads (Bangla/English/Banglish), under hard constraints — **no
external/paid API**, fast, cheap, Bangla-accurate.

### 13.1 Overall topology

- **Option A — Monolith + background workers (rejected for prod).** Single FastAPI
  app + Celery/Redis worker pool. Fastest to build, fine for MVP's 1k posts, but
  stages can't scale independently, LLM GPUs idle while NLP runs, one crash takes
  everything. Good _only_ as the MVP shape (Docker Compose).
- **Option B — Stage-decoupled, queue-based microservices (RECOMMENDED).**
  Ingestion → bus → Stage-1 NLP pool → router → Stage-2 LLM pool → assembler →
  stores. Independent scaling, backpressure, fault isolation, replay, clean
  autoscaling on queue depth. More moving parts; needs K8s + observability. Chosen
  for Production/Enterprise.
- **Option C — Serverless / FaaS pipeline (rejected).** Scales to zero, but GPU
  support is poor/expensive on FaaS, cold starts on big models are killers,
  per-invocation model loading wastes money. Only attractive for sparse/bursty
  volume — not this workload.

### 13.2 Message bus

| Criterion              | **Kafka**                    | **RabbitMQ**              | **Redis Streams**               | **NATS (JetStream)**  |
| ---------------------- | ---------------------------- | ------------------------- | ------------------------------- | --------------------- |
| Model                  | Distributed log, partitioned | Broker + exchanges/queues | Log on Redis                    | Lightweight log/queue |
| Throughput             | Very high (millions/s)       | High                      | High (single-node bound)        | Very high             |
| Replay / retention     | Excellent (native, long)     | Limited (consume-once)    | Good (capped streams)           | Good (JetStream)      |
| Ordering               | Per-partition                | Per-queue                 | Per-stream                      | Per-subject           |
| Scalability            | Horizontal, mature           | Cluster (more fiddly)     | Limited by Redis node           | Horizontal, simple    |
| Operational complexity | High (ZK/KRaft, tuning)      | Medium                    | **Low**                         | Low–medium            |
| Cost                   | Higher (infra + ops)         | Medium                    | **Low** (often already present) | Low                   |
| Reliability            | Excellent                    | Excellent                 | Good (needs persistence config) | Good                  |
| Ecosystem              | Huge (Connect, streams)      | Mature                    | Minimal                         | Growing               |

**Decision:** **MVP → Redis Streams** (already running Redis; consumer groups give
at-least-once + DLQ with near-zero extra ops). **Production/Enterprise → Kafka**
(partitioning maps to worker parallelism; retention/replay for reprocessing after a
model upgrade; lag metrics drive KEDA). RabbitMQ if you needed complex routing
topologies; NATS as a lighter Kafka alternative — both kept as documented
fallbacks.

### 13.3 Operational database: PostgreSQL vs MongoDB

| Criterion               | **PostgreSQL**                            | **MongoDB**                         |
| ----------------------- | ----------------------------------------- | ----------------------------------- |
| Schema                  | Relational + JSONB (flexible when needed) | Document, schema-flexible           |
| Transactions            | Strong ACID                               | Multi-doc since 4.x, weaker culture |
| Job/state tracking      | Excellent (constraints, FKs)              | Workable                            |
| Flexible nested results | JSONB columns                             | Native                              |
| Aggregations            | OK (not for heavy analytics)              | Aggregation pipeline (OK)           |
| Ops maturity            | Very high                                 | High                                |

**Decision: PostgreSQL.** Job/tenant/user state wants ACID and constraints, and
JSONB covers the flexible-result need without giving up relational integrity.
MongoDB is fine for flexible result documents but we already get that via JSONB +
the analytics store, so we avoid running two philosophies. Heavy aggregation goes
to ClickHouse, not the operational DB.

### 13.4 Analytics store: ClickHouse vs Elasticsearch

| Criterion               | **ClickHouse**                    | **Elasticsearch**            |
| ----------------------- | --------------------------------- | ---------------------------- |
| Primary strength        | Columnar OLAP aggregations        | Full-text search + analytics |
| Trend/aggregate queries | Excellent, sub-second on billions | Good, heavier                |
| Full-text search        | Basic                             | Excellent                    |
| Storage efficiency      | Very high (compression)           | Lower                        |
| Cost at scale           | Lower                             | Higher (JVM, shards)         |
| Ops                     | Medium                            | Medium–high                  |

**Decision: ClickHouse** for trend analysis, brand-mention counts, time-series
aggregations, and dashboard charts — the dominant analytics need (columnar,
sub-second on billions, high compression). **Optional Elasticsearch/OpenSearch**
later _only if_ rich full-text search over post text becomes first-class; Qdrant +
ClickHouse cover semantic search and aggregation meanwhile.

### 13.5 Vector database: Qdrant vs Weaviate vs Milvus

| Criterion        | **Qdrant**                    | **Weaviate**            | **Milvus**               |
| ---------------- | ----------------------------- | ----------------------- | ------------------------ |
| Language/perf    | Rust, fast, low memory        | Go, feature-rich        | C++, very scalable       |
| Filtering        | Excellent payload filters     | Good                    | Good                     |
| Ops simplicity   | **High**                      | Medium                  | Lower (more components)  |
| Built-in modules | Lean (BYO embeddings)         | Many (modules, hybrid)  | Lean                     |
| Scale ceiling    | High                          | High                    | **Very high** (billions) |
| Best fit         | Pragmatic mid-scale, easy ops | Hybrid search + modules | Massive enterprise scale |

**Decision: Qdrant** for MVP→Production: easiest to operate, fast, excellent
metadata filtering (scope vectors by tenant/platform/time). Reassess **Milvus** at
Enterprise/100k scale if vector count reaches billions and you need distributed
sharding. Weaviate only if you want its built-in hybrid-search/module ecosystem
over BYO simplicity.

### 13.6 Caching layer composition

All complementary, not alternatives: **Redis** (LLM/embedding/query cache, dedup
set, rate-limit counters), **embedding cache** (keyed by `content_hash`), **LLM
response cache** (keyed by `(model, task, content_hash)`), **query cache**
(short-TTL ClickHouse aggregations), **CDN** (Flutter web assets + static report
exports; not dynamic per-tenant data). See §15.4.

### 13.7 Load balancing & traffic management

- **API LB:** K8s Ingress (NGINX controller) in prod; plain NGINX in the MVP.
  **HAProxy** if you need advanced LB algorithms / very high connection counts
  outside K8s.
- **Worker LB:** not HTTP — workers self-balance by pulling from partitioned queues
  (competing consumers). "Load balancing" for workers = partition count + KEDA
  replica scaling.
- **Queue partitioning:** partition by `hash(post_id)`; keep partitions ≥ max
  consumers so every worker can be busy.
- **Service mesh:** start without one (mesh adds latency + ops); introduce
  **Linkerd** at Production for mTLS + retries if security/observability needs it
  (**Istio** if you need its full feature set).

### 13.8 LLM serving: self-host vs API, and why two local models

**Hard constraint: no external/paid LLM API.** Every model runs on our own GPUs.

| Option                            | Pros                                                                         | Cons                                                                          | Verdict                                 |
| --------------------------------- | ---------------------------------------------------------------------------- | ----------------------------------------------------------------------------- | --------------------------------------- |
| **External API**                  | Zero ops; frontier quality; elastic                                          | Per-token cost; data leaves the cluster; rate limits                          | **Rejected** — violates the no-API rule |
| **Single self-hosted LLM**        | One model to run; simplest                                                   | Either overpay (big model per post) or under-deliver (small model on reports) | Workable for the MVP only               |
| **Two self-hosted LLMs (CHOSEN)** | Right-sized per job; data in-house; no per-token bill; scale each separately | Two model deployments to run                                                  | **Chosen** for Production/Enterprise    |

**LLM-A** — fast 7B/8B (AWQ/GPTQ) for **high-volume, low-difficulty** per-post
refinement and short summaries. **LLM-B** — larger 14B/32B for **low-volume,
high-quality** cluster summarization, corpus insight, and grounded report
generation (RAG). At MVP scale the two can time-slice one GPU, or run LLM-A alone
until volume justifies LLM-B.

### 13.9 Deployment: Docker Compose vs Kubernetes

|                | **Docker Compose**  | **Kubernetes**              |
| -------------- | ------------------- | --------------------------- |
| Setup          | Minutes             | Days                        |
| Scaling        | Manual, single host | Auto (HPA/KEDA), multi-node |
| HA             | None                | Yes                         |
| GPU scheduling | Manual              | Native (device plugin)      |
| Cost           | Low                 | Higher baseline             |
| Best for       | **MVP / 1k posts**  | **Production / 10k+**       |

**Decision:** Compose for MVP, Kubernetes + KEDA for Production and Enterprise.

### 13.10 RAG: needed or not?

**Not for per-post analysis; yes for the reporting/insight layer.** Per-post
classification needs no retrieval. RAG becomes valuable for analyst Q&A over the
corpus, grounded report generation, and "what are people saying about X" queries —
backed by Qdrant + the embeddings you already compute. Full reasoning in §14.5.

---

## 14. AI model selection, RAG & fine-tuning

All choices favor open-source, GPU-efficient models with genuine Bangla support,
and are **100% self-hosted — no external/paid LLM API anywhere**. **Small models do
the bulk work; the LLM is selective.** The input is a post + its comment thread,
heavily **Banglish** (romanized Bangla mixed with English, e.g. "Green garden e vat
25 taka baire 10 taka"). Every choice is judged on **bn + en + code-mixed
Banglish**.

### 14.1 Model recommendations per task

| Task                                | Recommended model(s)                                                      | Bangla | English | Notes                                                                  |
| ----------------------------------- | ------------------------------------------------------------------------- | ------ | ------- | ---------------------------------------------------------------------- |
| **Language + Banglish detection**   | `fastText lid.176` + CLD3 + transliteration heuristic                     | ✅     | ✅      | <1 ms/item; flags `banglish` (romanized bn) → multilingual path        |
| **Sentiment** (post + per comment)  | `XLM-RoBERTa`/`mBERT` fine-tuned; BanglaBERT for bn                       | ✅     | ✅      | Run on post + every comment; aggregate into `sentiment_breakdown`      |
| **Emotion**                         | XLM-R fine-tuned (joy/anger/sadness/fear/…); GoEmotions heads for en      | ✅     | ✅      | Shares encoder with sentiment to save GPU                              |
| **Topic classification**            | XLM-R / embedding + classifier head; or zero-shot via small NLI model     | ✅     | ✅      | Use embeddings + lightweight classifier; reduces per-label models      |
| **Intent**                          | XLM-R fine-tuned (inform/promote/complain/request/…)                      | ✅     | ✅      | Per comment too (price/availability/location inquiries)                |
| **Toxicity / hate / offensive**     | `XLM-R`/`mBERT` fine-tuned; Detoxify (en) + Bangla hate datasets          | ✅     | ✅      | Bangla hate-speech corpora exist (e.g. Bengali Hate Speech); fine-tune |
| **NER (person/org/location/brand)** | `GLiNER` (multilingual, zero/few-shot), `spaCy` (en), BanglaBERT-NER (bn) | ✅     | ✅      | GLiNER gives flexible entity types without per-type models             |
| **Embeddings**                      | `BAAI/bge-m3` (multilingual, incl. Bangla) or `intfloat/multilingual-e5`  | ✅     | ✅      | Powers dedup, comment clustering, semantic search, RAG                 |
| **Summarization** (thread)          | **Local LLM-A** (small thread) / **LLM-B** (large/clustered) — see §14.2  | ✅     | ✅      | Selective; summary in the post's original language                     |
| **Insight / report generation**     | **Local LLM-B** + RAG (see §14.5)                                         | ✅     | ✅      | Cluster summaries → corpus-level insight                               |
| **Keyword extraction**              | KeyBERT (on embeddings) / YAKE                                            | ✅     | ✅      | Cheap, no extra GPU model                                              |

**Bangla-specific resources:** BanglaBERT (csebuetnlp), XLM-RoBERTa / mBERT (handle
code-mixed Banglish reasonably), bge-m3 / multilingual-e5 embeddings. **Banglish is
the dominant comment style**, not an edge case — it gets first priority in the
labeled eval set and any fine-tuning; an optional transliteration normalizer
(Banglish → Bangla script) can be added before classification if accuracy requires.

**Why a shared multilingual encoder (XLM-R family):** one encoder pass feeds
multiple lightweight task heads (sentiment, emotion, intent, topic), cutting GPU
cost vs. a separate full model per task, and handles code-mixed Bangla-English in a
single model.

### 14.2 The selective LLMs — two local models, no API

Both served on **vLLM**, on our own GPUs — no external/paid API, no per-token bill.

| Role                               | Model                                                                          | Serves                                                                             | Why                                                                                              |
| ---------------------------------- | ------------------------------------------------------------------------------ | ---------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------ |
| **LLM-A — fast / high-throughput** | `Qwen2.5-7B-Instruct` (or `Llama-3.1-8B-Instruct`), AWQ/GPTQ quantized         | Per-post selective refinement, hardest classification, short single-post summaries | Strong multilingual incl. Bangla, fits one mid (24 GB) GPU, continuous batching → high post rate |
| **LLM-B — large / high-quality**   | `Qwen2.5-32B-Instruct` (or `Qwen2.5-14B-Instruct` at smaller scale), quantized | Cluster summarization, corpus insight, grounded report generation (RAG)            | Higher reasoning/quality for the low-volume, quality-critical generation work                    |

Why two and not one: per-post refinement is high-volume/low-difficulty (favor a
small fast model); cluster/report generation is low-volume/high-quality (favor a
larger model). Splitting lets each run on right-sized GPUs and scale independently.
At MVP scale they can time-slice one GPU, or LLM-B can be dropped and LLM-A used for
both until volume justifies the second model. Both expose an **OpenAI-compatible
HTTP API** (vLLM wire protocol — a local server, not a paid service) with structured
JSON output mode. If LLM-B is saturated the router degrades to LLM-A or returns
Stage-1-only results; capacity is added by scaling GPUs, never by calling a
third-party API.

### 14.3 Model serving architecture

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

- **NLP models** → **Triton Inference Server** (or ONNX Runtime / CTranslate2) with
  dynamic batching and INT8/FP16 quantization; multiple models share GPUs.
- **LLMs** → **two vLLM deployments** (paged attention + continuous batching), each
  exposing a local OpenAI-compatible API; quantized weights (AWQ/GPTQ). The Stage-2
  worker picks A or B by task. No external API endpoint is configured.
- **Versioning:** every result records `model_versions` so re-runs after a model
  upgrade are auditable and reproducible (replay from Kafka).

### 14.4 Fine-tuning strategy (Bangla + English)

1. **Start zero-shot / off-the-shelf.** Ship MVP with pretrained multilingual
   models (XLM-R, GLiNER, bge-m3). Establish baselines and a labeled eval set.
2. **Collect & label.** Use the platform's own low-confidence/router-flagged posts
   as an active-learning pool. Build a held-out eval set per task and per language.
3. **Parameter-efficient fine-tuning (LoRA/QLoRA).** Fine-tune the shared XLM-R
   encoder + task heads, and optionally LLM-A (the 7B/8B local model). Cheap, fast,
   versionable. Fine-tuned weights stay in-house.
4. **Code-mixed focus.** Explicitly include Banglish (Bangla in Latin script, mixed
   sentences) in training data — where off-the-shelf models fail most.
5. **Distillation (later).** Distill LLM judgments on the hardest tasks into the
   small classifiers, shrinking the LLM slice.
6. **Evaluation gate.** No model ships without beating the current one on the
   per-language eval set; track per-task F1 and the LLM-routing rate.
7. **Continuous loop.** Periodically retrain on freshly labeled router-flagged data;
   version models, replay a sample batch from Kafka to compare.

### 14.5 RAG evaluation — is it needed?

**Per-post analysis: NO.** Classifying a single post needs the post's own text, not
retrieval. **Reporting / insight / analyst Q&A: YES.** RAG shines for "what are
people saying about Brand X this week?" (retrieve relevant posts from Qdrant, feed
to the LLM grounded), grounded report/insight generation across the corpus, and
cluster summarization with citations. **Benefits:** grounded, citation-able answers
without stuffing the whole corpus into context; reuses embeddings you already
compute. **Drawbacks:** retrieval-quality dependent; mitigate with good chunking,
metadata filters, showing source posts. **Stack:** Qdrant + bge-m3/multilingual-e5
embeddings + the local LLM-B for generation — fully local end to end.

---

## 15. Infrastructure — GPU, monitoring, caching, scaling

### 15.1 Throughput model

A unit is a post + its comment thread, so a single "item" can be a handful to
hundreds of short texts — Stage-1 throughput is better measured in **texts
(post + comments) per second** rather than threads/sec.

- **Stage-1 NLP** (shared XLM-R encoder + heads + embedding), batched on GPU: order
  of **hundreds–low-thousands of texts/sec** on a single modern GPU. A thread with
  50 comments = ~51 texts.
- **Stage-2 LLMs** (two local models on vLLM, continuous batching): order of
  **thousands of output tokens/sec** aggregate; but they only see the **selective
  slice** (single-digit % of posts) and mostly **cluster-level** calls.

The NLP fleet dominates raw text throughput; the LLM dominates _quality_ work on a
small slice. Size them independently.

### 15.2 GPU requirements by stage

- **MVP — 1,000 posts/batch:** 1 × consumer GPU (RTX 4090/3090, 24 GB) runs the
  whole show: NLP suite (quantized) + local LLMs via vLLM, time-sliced. Run just
  **LLM-A** (quantized 7B) for all Stage-2 work; add **LLM-B** when cluster/report
  quality demands it. CPU-only possible for the smallest NLP models. Docker Compose.
- **Production — 10,000 posts/batch:** NLP fleet 1–2 GPUs (24 GB consumer or L4/A10)
  with Triton dynamic batching, autoscaled by queue depth. **LLM-A** (quantized 7B)
  on an L4/A10 + **LLM-B** (quantized 14B/32B) on a data-center GPU (**L40S / A100
  40GB**); at lower volume LLM-A can share the NLP GPU. Small K8s cluster, 2–4 GPUs,
  KEDA.
- **Enterprise — 100,000 posts/batch:** NLP fleet several data-center GPUs (A10 /
  L4 / L40S), autoscaled. **LLMs:** multiple **A100/H100** (or several L40S) behind
  vLLM serving both local models, multi-replica with tensor parallelism for LLM-B;
  scale out by adding GPU replicas (never an external API). Multi-node K8s, GPU node
  pools per stage, aggressive caching + clustering.

| Class                              | Examples                                                  | Best for                                 | Tradeoff                                                          |
| ---------------------------------- | --------------------------------------------------------- | ---------------------------------------- | ----------------------------------------------------------------- |
| **Consumer**                       | RTX 3090 / 4090 (24 GB)                                   | MVP, NLP fleet, dev                      | Cheapest per FLOP; no NVLink/ECC, licensing limits in some clouds |
| **Data-center inference**          | L4 (24 GB), A10 (24 GB), L40S (48 GB)                     | Production NLP + small/mid LLM           | Best perf/$ for inference; power-efficient                        |
| **Data-center training/large LLM** | A100 (40/80 GB), H100 (80 GB)                             | Enterprise LLM, fine-tuning large models | Expensive; reserve for where they pay off                         |
| **Cloud alternatives**             | AWS g5/g6, GCP G2, Azure NC/ND, Lambda, RunPod, CoreWeave | Any stage, elastic                       | Spot/preemptible for batch saves a lot; on-demand for steady base |

**Cost lever:** run the steady base load on owned/reserved GPUs and burst to
**spot/preemptible cloud GPUs** for large batches — batch work tolerates
interruptions (re-queue from Kafka).

### 15.3 Monitoring stack

```text
            ┌──────────────────────────────────────────────────────┐
   services │ OpenTelemetry SDK in every service/worker             │
   & workers│  emits: metrics, logs, traces                         │
            └───────┬───────────────┬───────────────┬──────────────┘
                    │ metrics       │ logs          │ traces
              ┌─────▼─────┐   ┌─────▼─────┐   ┌──────▼──────┐
              │ Prometheus│   │   Loki    │   │   Jaeger    │
              └─────┬─────┘   └─────┬─────┘   └──────┬──────┘
                    └───────────────┼────────────────┘
                              ┌─────▼─────┐
                              │  Grafana  │  dashboards + alerts
                              └───────────┘
```

- **Prometheus** — queue depth/lag, posts/sec per stage, GPU utilization (DCGM
  exporter), LLM-routing rate, cache hit rate, error rate, p50/p95/p99 latency, DLQ
  size.
- **Grafana** — dashboards (throughput, cost-per-batch, LLM slice %) + alerting
  (Alertmanager): queue lag, DLQ growth, GPU saturation, error-rate spikes.
- **Loki** — centralized structured logs, correlated to traces by trace_id.
- **OpenTelemetry** — single instrumentation standard; exports metrics→Prometheus,
  logs→Loki, traces→Jaeger.
- **Jaeger** — distributed traces follow a post from ingestion → NLP → router →
  (LLM) → assembler → store.

**Key product metrics:** % of posts that hit the LLM, cache hit rates
(dedup/embedding/LLM), cost per 1k posts, per-language accuracy drift.

### 15.4 Caching strategy

| Cache                          | Key                           | Purpose                                   | TTL                  |
| ------------------------------ | ----------------------------- | ----------------------------------------- | -------------------- |
| **Dedup set** (Redis)          | `content_hash`                | Skip re-analysis of exact dupes/reshares  | long / per-retention |
| **Embedding cache** (Redis)    | `content_hash`                | Avoid recomputing vectors                 | long                 |
| **LLM response cache** (Redis) | `(model, task, content_hash)` | Free repeats of LLM calls                 | medium–long          |
| **Query cache** (Redis)        | normalized query              | Fast dashboard aggregations               | short (secs–mins)    |
| **CDN**                        | URL                           | Flutter web assets, static report exports | long, versioned      |

On real social feeds these caches remove a large fraction of total work — a primary
cost lever, not an afterthought.

### 15.5 Scaling strategy

- **Horizontal first.** Add worker replicas + queue partitions before bigger GPUs.
- **Autoscale on backlog.** **KEDA** scales each worker pool on its queue lag/depth
  (not CPU). HPA covers the stateless API tier on CPU/RPS.
- **Independent pools.** NLP and LLM pools scale separately; the cheap fleet can
  grow 10× without touching the LLM GPUs.
- **Partitioning.** Partition the bus by `hash(post_id)`; keep partitions ≥ max
  consumers so workers never starve.
- **Stateless workers.** All state in queue + stores → scale to zero between
  batches, scale out instantly for a 10k/100k burst.
- **Spot for batch.** Use preemptible/spot GPU nodes for batch surges; Kafka replay
  makes interruptions safe.
- **Backpressure.** Bounded queues + DLQ prevent overload cascades.
- **DB scaling.** PostgreSQL read replicas; ClickHouse shards/replicas; Qdrant
  collections sharded by tenant at large scale.

---

## 16. Cost estimation

> **Planning-grade order-of-magnitude estimates, not quotes.** Cloud GPU prices
> change frequently and vary by region, commitment, and provider. Treat the
> _ratios and the dominant cost drivers_ as the durable insight. **There is no LLM
> API line at all** — both LLMs are self-hosted, so cost is GPU + storage +
> networking only, with no per-token charges. The headline number is **cost per
> 1,000 threads analyzed**, which the hybrid pipeline drives down by keeping the LLM
> to one cluster-level summary per thread.

### 16.1 What drives cost

| Driver      | Without hybrid (LLM-per-post)                 | With hybrid (this design)                                             |
| ----------- | --------------------------------------------- | --------------------------------------------------------------------- |
| LLM tokens  | **Dominant, runaway** — every post = LLM call | Small — only the selective slice + cluster-level calls                |
| GPU compute | Moderate                                      | **Now the main line**, but cheap & predictable (batched small models) |
| Storage     | Small                                         | Small                                                                 |
| Networking  | Small–moderate                                | Small–moderate                                                        |

The hybrid architecture + self-hosting both LLMs converts an unbounded per-token
bill into a **bounded, mostly-fixed GPU bill**.

### 16.2 MVP — ~1,000 posts/batch

Single host, one 24 GB consumer GPU, Docker Compose. Self-hosted everything.

| Line                                                                 | Estimate (USD/mo)   | Notes                                                                           |
| -------------------------------------------------------------------- | ------------------- | ------------------------------------------------------------------------------- |
| Compute (1 GPU host, on-prem amortized **or** 1 cloud GPU part-time) | $150 – $700         | On-prem 4090 amortized at low end; cloud L4/A10 on-demand part-time at high end |
| Storage (Postgres + ClickHouse + Qdrant + object, modest)            | $10 – $40           | Tens of GB                                                                      |
| Networking                                                           | $5 – $30            | Mostly egress for dashboard/API                                                 |
| LLM (both local models, time-sliced on the same GPU)                 | ~$0 incremental     | LLM-A + LLM-B share the GPU; selective + cached                                 |
| Monitoring (self-hosted Prometheus/Grafana/Loki)                     | ~$0 – $20           | Runs on the same box                                                            |
| **Total**                                                            | **~$170 – $800/mo** | Dominated by the single GPU                                                     |

### 16.3 Production — ~10,000 posts/batch

Small K8s cluster, 2–4 GPUs, KEDA autoscaling, spot for batch surges.

| Line                                                       | Estimate (USD/mo)       | Notes                           |
| ---------------------------------------------------------- | ----------------------- | ------------------------------- |
| Compute — NLP fleet (1–2 GPUs, autoscaled, partly spot)    | $400 – $1,500           | Scales with daily volume        |
| Compute — LLM GPUs (LLM-A on L4/A10 + LLM-B on L40S/A100)  | $900 – $3,200           | Both local; reserved/spot lower |
| Compute — CPU services (API, ingestion, assembler, DBs)    | $200 – $600             | Several small nodes             |
| Storage (Postgres + ClickHouse + Qdrant + object, growing) | $50 – $250              | Hundreds of GB → TB             |
| Networking / egress                                        | $50 – $300              | Dashboard, exports, inter-AZ    |
| Monitoring                                                 | $30 – $150              | Self-hosted or small managed    |
| **Total**                                                  | **~$1,600 – $6,000/mo** | LLM + NLP GPUs dominate; no API |

### 16.4 Enterprise — ~100,000 posts/batch

Multi-node K8s, GPU node pools per stage, several A100/H100 or many L40S.

| Line                                                                   | Estimate (USD/mo)         | Notes                                                   |
| ---------------------------------------------------------------------- | ------------------------- | ------------------------------------------------------- |
| Compute — NLP fleet (several data-center GPUs, autoscaled, spot-heavy) | $3,000 – $12,000          | Horizontal scale; spot saves 50–70%                     |
| Compute — LLM-A + LLM-B (multiple A100/H100 or many L40S, vLLM)        | $5,000 – $25,000          | Biggest line; both local; reserved/spot critical        |
| Compute — CPU services + DB nodes                                      | $1,000 – $4,000           | HA Postgres, ClickHouse cluster, Qdrant shards          |
| Storage (TBs across stores + object + backups)                         | $300 – $2,000             | Grows with retention                                    |
| Networking / egress                                                    | $300 – $2,000             | Significant at this scale                               |
| Monitoring / observability                                             | $150 – $600               |                                                         |
| **Total**                                                              | **~$10,000 – $48,000/mo** | LLM GPUs dominate; caching/clustering decide the spread |

### 16.5 Levers ranked by impact

1. **Hybrid routing** — keep the LLM slice in the single digits %. Biggest lever.
2. **Caching + dedup** — social feeds are repetitive; cache hits are free results.
3. **Cluster-level LLM** — summarize clusters, not individual posts.
4. **Quantization + batching** — maximize GPU utilization (vLLM/Triton).
5. **Spot/reserved GPUs** — batch tolerates preemption (Kafka replay); reserve the
   steady base.
6. **Both LLMs local, no API** — zero per-token pricing and no data egress; scale by
   adding GPUs. Right-size: cheap LLM-A for per-post, larger LLM-B only for
   low-volume cluster/report work.

---

## 17. API design

REST over HTTPS. Auth via `Authorization: Bearer <JWT>` or `X-API-Key: <key>`. All
endpoints versioned under `/v1`. Conventions: `202 Accepted` for async work;
idempotency via `Idempotency-Key`; pagination via `?limit=&cursor=`; consistent
error envelope.

### 17.1 Ingestion — `POST /v1/posts/upload`

Upload one or many **threads** inline (a parent post plus its comments/replies), or
register a large JSONL file already in object storage. The comment tree may be
nested; the service flattens it but preserves `parent_id`.

**Request (inline batch):**

```json
{
  "source": "inline",
  "posts": [
    {
      "post_id": "fb_12345",
      "platform": "facebook",
      "url": "https://facebook.com/...",
      "author": "TalentedOstrich6332",
      "text": "গ্রিন গার্ডেন এ খাবারের দাম অনেক বেশি... একটা সিংগারা ২০ টাকা চাইল।",
      "created_at": "2026-06-01T10:00:00Z",
      "engagement": { "reactions": 48, "comment_count": 12 },
      "comments": [
        {
          "comment_id": "c1",
          "parent_id": null,
          "author": "GenuineJackfruit1970",
          "text": "Green garden e sudhu polao 100 taka baire 30-40 takai e paua jay",
          "created_at": "2026-06-01T13:00:00Z"
        },
        {
          "comment_id": "c2",
          "parent_id": "c1",
          "author": "TalentedOstrich6332",
          "text": "Ami agee breakfast kortam green garden e. Ekhn oitao baad disi.",
          "created_at": "2026-06-01T13:30:00Z"
        }
      ],
      "meta": { "page_id": "p_99", "lang_hint": "bn" }
    }
  ],
  "options": { "tasks": ["all"], "want_summary": true, "summary_lang": "auto" }
}
```

`comments` is optional (a bare post is valid). `summary_lang: "auto"` keeps the
summary in the post's detected language; pass `"bn"`/`"en"` to force it. Banglish
comments are handled natively.

**Request (large file):**

```json
{
  "source": "object",
  "object_uri": "s3://uploads/tenant42/batch-2026-06-01.jsonl",
  "format": "jsonl",
  "options": { "tasks": ["all"], "want_summary": true }
}
```

**Response — `202 Accepted`:**

```json
{
  "job_id": "job_01HZX...",
  "accepted": 2,
  "duplicates_skipped": 0,
  "status": "queued",
  "status_url": "/v1/analysis/job_01HZX...",
  "created_at": "2026-06-01T12:00:00Z"
}
```

`options.tasks` selects analyses (e.g. `["sentiment","ner","toxicity"]` or
`["all"]`). `want_summary`/`want_insight` opt into LLM tasks; otherwise the router
keeps work on the cheap path unless confidence is low.

### 17.2 Batch processing — `POST /v1/analysis/run`

Trigger (or re-trigger) analysis for already-ingested posts — useful for
reprocessing after a model upgrade.

```json
{
  "selector": { "job_id": "job_01HZX..." },
  "options": {
    "tasks": ["sentiment", "emotion", "topics", "ner", "toxicity"],
    "want_summary": true,
    "want_cluster_summary": true,
    "model_profile": "default"
  }
}
```

`selector` may instead be `{ "post_ids": [...] }` or
`{ "filter": { "platform": "instagram", "from": "...", "to": "..." } }`.

**Response — `202 Accepted`:**

```json
{
  "analysis_id": "an_01J0A...",
  "post_count": 10000,
  "status": "queued",
  "estimated_llm_share": 0.07,
  "status_url": "/v1/analysis/an_01J0A..."
}
```

`estimated_llm_share` previews the expected fraction routed to the LLM — a
transparency feature tied to the hybrid design.

### 17.3 Results — `GET /v1/analysis/{id}`

`{id}` is a `job_id` or `analysis_id`.

**In progress:**

```json
{
  "id": "an_01J0A...",
  "status": "processing",
  "progress": {
    "total": 10000,
    "completed": 6400,
    "llm_used": 420,
    "failed": 3
  },
  "updated_at": "2026-06-01T12:05:00Z"
}
```

**Complete (with `?include=results&limit=2`):**

```json
{
  "id": "an_01J0A...",
  "status": "completed",
  "progress": {
    "total": 10000,
    "completed": 9997,
    "llm_used": 680,
    "failed": 3
  },
  "results": [
    {
      "post_id": "fb_12345",
      "platform": "facebook",
      "author": "TalentedOstrich6332",
      "language": "bn",
      "language_mix": ["bn", "banglish", "en"],
      "language_confidence": 0.97,
      "post_type": "complaint",
      "post_summary": "গ্রিন গার্ডেন ও ট্রান্সপোর্টে খাবারের দাম বাইরের তুলনায় অনেক বেশি; পোস্টদাতা কেনা বন্ধ ও বয়কটের ডাক দিয়েছেন।",
      "post_summary_lang": "bn",
      "overall_sentiment": "negative",
      "sentiment_score": -0.64,
      "emotion": "anger",
      "intents": ["complaint", "call_to_action"],
      "topics": ["food pricing", "campus transport", "boycott"],
      "entities": [
        { "type": "organization", "value": "Green Garden", "confidence": 0.94 }
      ],
      "brand_mentions": [
        { "name": "Green Garden", "sentiment": "negative", "mentions": 9 }
      ],
      "keywords": ["দাম", "সিঙ্গারা", "boycott"],
      "toxicity_score": 0.07,
      "hate_speech_score": 0.01,
      "engagement": { "reactions": 48, "comment_count": 12 },
      "comment_analysis": {
        "analyzed": 12,
        "sentiment_breakdown": { "positive": 1, "negative": 9, "neutral": 2 },
        "themes": [
          "prices above market",
          "same quality cheaper outside",
          "boycott calls"
        ]
      },
      "post_summary_source": "llm",
      "confidence": 0.92,
      "processing": {
        "unit": "post+thread",
        "stage1_ms": 58,
        "llm_used": true,
        "llm_model": "LLM-A"
      },
      "created_at": "2026-06-01T10:00:00Z"
    }
  ],
  "next_cursor": "eyJvZmZzZXQiOjJ9"
}
```

Real-time alternative: `GET /v1/analysis/{id}/stream` (SSE) pushes per-post results
as they complete, for live dashboards.

### 17.4 Reporting — `GET /v1/reports`

List and fetch generated reports (trends, brand mentions, political analysis,
cluster insights). Reports are LLM-generated at the _cluster/corpus_ level.

**List — `GET /v1/reports?type=trend&from=2026-06-01&to=2026-06-07`:**

```json
{
  "reports": [
    {
      "report_id": "rep_88",
      "type": "trend",
      "title": "Weekly trend digest",
      "period": "2026-06-01..2026-06-07",
      "created_at": "2026-06-07T00:10:00Z"
    }
  ],
  "next_cursor": null
}
```

**Fetch — `GET /v1/reports/rep_88`:**

```json
{
  "report_id": "rep_88",
  "type": "trend",
  "period": "2026-06-01..2026-06-07",
  "summary": "Technology and education topics dominated; positive sentiment rose 12%...",
  "clusters": [
    {
      "cluster_id": "c1",
      "label": "AI in education",
      "post_count": 1840,
      "top_sentiment": "positive",
      "summary": "...",
      "sample_post_ids": ["fb_12345"]
    }
  ],
  "metrics": { "total_posts": 10000, "languages": { "bn": 6200, "en": 3800 } },
  "generated_by": "llm",
  "created_at": "2026-06-07T00:10:00Z"
}
```

**Generate — `POST /v1/reports`:**

```json
{
  "type": "brand_mentions",
  "filter": { "brand": "BrandX", "from": "...", "to": "..." },
  "options": { "grounded": true }
}
```

→ `202 Accepted` with `report_id` and `status_url`. `grounded: true` uses RAG
(Qdrant retrieval + LLM) for citation-backed output.

### 17.5 Supporting endpoints

| Endpoint                           | Purpose                                                           |
| ---------------------------------- | ----------------------------------------------------------------- |
| `POST /v1/auth/token`              | Exchange credentials/API key for a JWT                            |
| `GET /v1/health` / `GET /v1/ready` | Liveness / readiness probes                                       |
| `GET /v1/usage`                    | Per-tenant usage + cost metering (posts, LLM calls)               |
| `GET /v1/search?q=&semantic=true`  | Semantic/keyword search over analyzed posts (Qdrant + ClickHouse) |
| `DELETE /v1/posts/{id}`            | Data deletion (retention / GDPR-style)                            |

### 17.6 Error envelope

```json
{
  "error": {
    "code": "validation_error",
    "message": "post[1].text exceeds max length",
    "request_id": "req_01J0...",
    "details": [{ "field": "posts[1].text", "issue": "too_long" }]
  }
}
```

Standard codes: `unauthorized`, `forbidden`, `validation_error`, `rate_limited`
(with `Retry-After`), `not_found`, `conflict` (idempotency), `internal`.

---

## 18. Worked examples

Two real scraped threads run through the smart layer. The unit of analysis is
always a **post + its comment thread**; `post_summary` is in the post's own
language; Banglish is detected and folded into the dominant language.

### 18.1 Example 1 — Bangla complaint thread (campus food prices)

A Bangla post complaining about Green Garden / campus-transport food prices, with a
thread of mostly Banglish comments agreeing and calling for a boycott.

**Input (abridged):**

```json
{
  "post_id": "fb_greengarden_001",
  "platform": "facebook",
  "author": "TalentedOstrich6332",
  "text": "সাধারণত দেখা যায় যে গ্রিণ গার্ডেন এ খাবারের দাম অনেক বেশি... আজকে ট্রান্সপোর্টে একটা সিংগারা ২০ টাকা চাইল। এটা তো জুলুম। এই বিষয়ে কথা বলা প্রয়োজন মনে হয়েছে।",
  "created_at": "2026-06-01T09:00:00Z",
  "engagement": { "reactions": 48, "comment_count": 12 },
  "comments": [
    {
      "comment_id": "c1",
      "parent_id": null,
      "author": "GenuineJackfruit1970",
      "text": "Green garden e sudhu polao 100 taka baire 30-40 takai e paua jay. Quality same. Mone hoy gold dhuya pani diye ranna kore"
    },
    {
      "comment_id": "c2",
      "parent_id": null,
      "author": "Anonymous participant 558",
      "text": "নুনুর গার্ডেনে এক প্লেট ভাতের দাম ২০ টাকা 🤣 বাইরে ৫ টাকা একই চাল"
    },
    {
      "comment_id": "c3",
      "parent_id": null,
      "author": "AuthenticDragon4378",
      "text": "Eder boycott koray uttom karon era kokokhnoi apnake value korbe na"
    },
    {
      "comment_id": "c4",
      "parent_id": null,
      "author": "Munam Mira",
      "text": "Even 50 taka lekha Ice-cream gula naki 150! Ajob kahini."
    },
    {
      "comment_id": "c5",
      "parent_id": null,
      "author": "StunningDolphin9938",
      "text": "ট্রান্সপোর্টের ওই ভাইয়ার দোকান ভাড়াও নাই... ১০০% প্রফিট করছে উনি।"
    }
  ]
}
```

**Output JSON:**

```json
{
  "post_id": "fb_greengarden_001",
  "platform": "facebook",
  "author": "TalentedOstrich6332",
  "language": "bn",
  "language_mix": ["bn", "banglish", "en"],
  "language_confidence": 0.97,
  "post_type": "complaint",
  "post_summary": "পোস্টদাতা অভিযোগ করছেন গ্রিন গার্ডেন ও ক্যাম্পাস ট্রান্সপোর্টে খাবারের দাম বাইরের তুলনায় অনেক বেশি (একটি সিঙ্গারা ২০ টাকা), যা তিনি অন্যায্য মনে করছেন এবং কেনা বন্ধ করার ডাক দিয়েছেন। মন্তব্যকারীরা একমত — একই মানের খাবার বাইরে অনেক সস্তা — এবং অনেকে বয়কটের প্রস্তাব দিয়েছেন।",
  "post_summary_lang": "bn",
  "overall_sentiment": "negative",
  "sentiment_score": -0.64,
  "emotion": "anger",
  "intents": ["complaint", "call_to_action"],
  "topics": ["food pricing", "campus transport", "boycott"],
  "entities": [
    { "type": "organization", "value": "Green Garden", "confidence": 0.94 },
    { "type": "product", "value": "singara", "confidence": 0.82 },
    { "type": "product", "value": "polao", "confidence": 0.79 }
  ],
  "brand_mentions": [
    { "name": "Green Garden", "sentiment": "negative", "mentions": 9 }
  ],
  "keywords": ["দাম", "সিঙ্গারা", "polao", "boycott", "transport"],
  "toxicity_score": 0.08,
  "hate_speech_score": 0.01,
  "engagement": { "reactions": 48, "comment_count": 12 },
  "comment_analysis": {
    "analyzed": 12,
    "sentiment_breakdown": { "positive": 1, "negative": 9, "neutral": 2 },
    "themes": [
      "prices far above outside market",
      "same quality cheaper elsewhere",
      "calls to boycott",
      "transport vendor over-charging"
    ],
    "representative_comments": [
      {
        "author": "GenuineJackfruit1970",
        "lang": "banglish",
        "sentiment": "negative",
        "text": "Green garden e sudhu polao 100 taka baire 30-40 takai e paua jay"
      },
      {
        "author": "AuthenticDragon4378",
        "lang": "banglish",
        "sentiment": "negative",
        "text": "Eder boycott koray uttom"
      }
    ]
  },
  "post_summary_source": "llm",
  "confidence": 0.92,
  "processing": {
    "unit": "post+thread",
    "stage1_ms": 61,
    "llm_used": true,
    "llm_model": "LLM-A"
  },
  "created_at": "2026-06-01T09:00:00Z"
}
```

**What did the work:** Stage-1 NLP detected language/Banglish, per-comment
sentiment, entities (Green Garden), and toxicity cheaply. The router sent the thread
to **LLM-A** (the fast per-post model) only for the Bangla `post_summary` and the
comment `themes` (generative fields) — everything else is small-model output.

### 18.2 Example 2 — English brand-page thread (Fabrilife jerseys)

An English promotional post from a clothing brand, with a large thread of
product-availability and price inquiries (many Banglish), plus the brand's own
replies. Summary requested in English.

**Input (abridged — the real thread has 100+ comments):**

```json
{
  "post_id": "fb_fabrilife_001",
  "platform": "facebook",
  "author": "Fabrilife",
  "text": "Unlock Your Confidence! Fabrilife, Bangladesh's fastest growing clothing brand, brings you premium quality comfort. Shop Now and discover your new favorite piece of confidence!",
  "created_at": "2026-05-28T08:00:00Z",
  "engagement": { "reactions": 76000, "comment_count": 2000 },
  "comments": [
    {
      "comment_id": "c1",
      "parent_id": null,
      "author": "Kamrul Islam",
      "text": "ইরান, তুরস্কের জার্সি আনেন!"
    },
    {
      "comment_id": "c2",
      "parent_id": null,
      "author": "Sayeed Islam",
      "text": "আর্জেন্টিনা জার্সি নিতে চাচ্ছি।"
    },
    {
      "comment_id": "c3",
      "parent_id": "c2",
      "author": "Fabrilife",
      "text": "The offer price of Argentina 2026 World Cup Home Jersey is 1290 taka..."
    },
    {
      "comment_id": "c4",
      "parent_id": null,
      "author": "Monir Zaman",
      "text": "৪ বছরের বাচ্চাদের jersey হবে।"
    },
    {
      "comment_id": "c5",
      "parent_id": "c4",
      "author": "Fabrilife",
      "text": "We are sorry, Kids Jersey is not available."
    },
    {
      "comment_id": "c6",
      "parent_id": null,
      "author": "Mohammad Zahirul Haque",
      "text": "Eta copy naki original?"
    },
    {
      "comment_id": "c7",
      "parent_id": "c6",
      "author": "Fabrilife",
      "text": "All of our jerseys are imported from Thailand and are high-quality 1:1 replicas."
    }
  ]
}
```

**Output JSON:**

```json
{
  "post_id": "fb_fabrilife_001",
  "platform": "facebook",
  "author": "Fabrilife",
  "language": "en",
  "language_mix": ["en", "bn", "banglish"],
  "language_confidence": 0.96,
  "post_type": "promotion",
  "post_summary": "A promotional post from Fabrilife (a Bangladeshi clothing brand) marketing premium, comfortable clothing. The comment thread is dominated by customer inquiries about World Cup football jerseys — availability of specific national teams (Argentina, Portugal, Germany, Brazil, England), prices (~1270–1290 taka), kids' sizes, outlet locations, and whether the jerseys are original. The brand actively replies with prices, order links, and outlet addresses; kids' jerseys and several teams are out of stock.",
  "post_summary_lang": "en",
  "overall_sentiment": "positive",
  "sentiment_score": 0.34,
  "emotion": "interest",
  "intents": [
    "promotion",
    "product_inquiry",
    "price_inquiry",
    "availability_inquiry"
  ],
  "topics": [
    "football jerseys",
    "world cup 2026",
    "pricing",
    "product availability",
    "outlet locations"
  ],
  "entities": [
    { "type": "organization", "value": "Fabrilife", "confidence": 0.98 },
    { "type": "product", "value": "World Cup jersey", "confidence": 0.9 },
    { "type": "location", "value": "Argentina", "confidence": 0.7 },
    { "type": "location", "value": "Portugal", "confidence": 0.7 }
  ],
  "brand_mentions": [
    { "name": "Fabrilife", "sentiment": "positive", "mentions": 60 }
  ],
  "keywords": [
    "jersey",
    "argentina",
    "portugal",
    "price",
    "available",
    "outlet"
  ],
  "toxicity_score": 0.01,
  "hate_speech_score": 0.0,
  "engagement": { "reactions": 76000, "comment_count": 2000 },
  "comment_analysis": {
    "analyzed": 2000,
    "sentiment_breakdown": {
      "positive": 420,
      "negative": 110,
      "neutral": 1470
    },
    "themes": [
      "which national-team jerseys are available",
      "price requests (mostly answered: ~1290 taka)",
      "kids' jersey availability (out of stock)",
      "original vs replica questions",
      "outlet / showroom locations across cities"
    ],
    "top_intents": [
      { "intent": "price_inquiry", "count": 540 },
      { "intent": "availability_inquiry", "count": 430 },
      { "intent": "location_inquiry", "count": 180 }
    ]
  },
  "post_summary_source": "llm",
  "confidence": 0.9,
  "processing": {
    "unit": "post+thread",
    "stage1_ms": 240,
    "llm_used": true,
    "llm_model": "LLM-B"
  },
  "created_at": "2026-05-28T08:00:00Z"
}
```

**What did the work:** with 2,000 comments, the router does **not** send every
comment to the LLM. Stage-1 NLP classifies each comment's language, sentiment, and
intent cheaply; comments are **clustered by embedding**, and only cluster
representatives + the post go to **LLM-B** for the English `post_summary` and
`themes`. This keeps a 2,000-comment thread to a single cluster-level LLM call
instead of thousands.

### 18.3 How these map to the owner's request

The owner asked for `{ post_summary, sentiment_analysis, "and something like
that" }`. The schema delivers:

- **`post_summary`** — in the original language (`post_summary_lang` records it).
- **`sentiment_analysis`** — split into post-level (`overall_sentiment` +
  `sentiment_score`) and thread-level (`comment_analysis.sentiment_breakdown` =
  positive / negative / neutral counts).
- **"something like that"** — `post_type`, `intents`, `topics`, `entities`,
  `brand_mentions`, `comment_analysis.themes`/`top_intents`, toxicity, and
  engagement — rich structured signal, not just two fields.

---

## 19. Deployment

### 19.1 Docker Compose vs Kubernetes

| Dimension                | **Docker Compose**    | **Kubernetes**                 |
| ------------------------ | --------------------- | ------------------------------ |
| Setup time               | Minutes               | Days                           |
| Scaling                  | Manual, single host   | Auto (HPA + KEDA), multi-node  |
| High availability        | None (single host)    | Yes (multi-node, self-healing) |
| GPU scheduling           | Manual pinning        | Native (NVIDIA device plugin)  |
| Autoscale on queue depth | No                    | Yes (KEDA)                     |
| Rolling/canary deploys   | Limited               | Native                         |
| Operational cost         | Low                   | Higher baseline + skill        |
| Best fit                 | **MVP / 1,000 posts** | **Production / 10k–100k+**     |

**Recommendation:** Docker Compose for the MVP, Kubernetes + KEDA for Production and
Enterprise.

### 19.2 MVP architecture (Docker Compose)

Single host with one GPU. One `docker-compose.yml` brings up:

```text
services:
  gateway        (NGINX)            → TLS, routing, rate limit
  api            (FastAPI)          → auth + ingestion + reporting (combined for MVP)
  worker-nlp     (Python)           → Stage-1 small-model suite (GPU)
  worker-llm     (Python + vLLM)    → Stage-2 local LLMs A+B (share GPU, no API)
  redis          (cache/queue)      → Redis Streams = bus + cache + dedup
  postgres       (ops + jobs)
  clickhouse     (analytics)
  qdrant         (vectors)
  minio          (object storage)
  prometheus + grafana + loki       → monitoring
```

- Queue = **Redis Streams** (no separate Kafka yet).
- API services can be one process for the MVP; split later.
- GPU shared between NLP and the local LLM(s) via time-slicing (run LLM-A only at
  MVP scale; both LLM-A and LLM-B are self-hosted — no external API).
- Goal: prove the hybrid pipeline end-to-end on 1k post+comment-thread batches.

### 19.3 Production architecture (Kubernetes)

```text
            Internet
               │ TLS
        ┌──────▼───────┐
        │  Ingress     │  NGINX ingress controller (+ optional Linkerd mesh)
        │  Controller  │
        └──────┬───────┘
   ┌───────────┼───────────────────────────┐
   │           │                           │
┌──▼───┐  ┌────▼─────┐  ┌──────────┐  ┌────▼──────┐
│auth  │  │ingestion │  │reporting │  │user-mgmt  │   Deployments (HPA on CPU/RPS)
│(pods)│  │ (pods)   │  │ (pods)   │  │ (pods)    │
└──────┘  └────┬─────┘  └────┬─────┘  └───────────┘
               │ produce      │ read
          ┌────▼──────────────▼────┐
          │   Kafka (StatefulSet/   │  partitioned topics: ingest, llm, dlq
          │   operator, 3 brokers)  │
          └────┬───────────────┬────┘
        consume│               │consume
   ┌───────────▼──┐      ┌─────▼─────────┐
   │ nlp-workers  │      │ llm-workers   │   Deployments on GPU node pools
   │ (GPU pool A) │      │ (GPU pool B)  │   KEDA scales on Kafka lag
   │ KEDA-scaled  │      │ KEDA-scaled   │
   └──────┬───────┘      └─────┬─────────┘
          │ Triton svc          │ vLLM svc
   ┌──────▼───────┐      ┌──────▼────────┐
   │ triton (GPU) │      │ vllm A + B    │   model servers (2 local LLMs, no API)
   └──────────────┘      └───────────────┘
          │ writes (assembler Deployment) │
   ┌──────▼───────┬──────────┬────────────▼──────┬───────────┐
   │ postgres     │clickhouse│ qdrant            │ minio/S3  │  StatefulSets / managed
   │ (HA, replica)│(cluster) │ (sharded)         │           │
   └──────────────┴──────────┴───────────────────┴───────────┘
        observability namespace: prometheus, grafana, loki, jaeger, otel-collector
```

### 19.4 Kubernetes deployment plan

**Namespaces:** `platform` (services + workers), `data` (DBs, queue), `models`
(Triton, vLLM), `observability` (monitoring), `ingress`.

**Workload mapping:**

- **Stateless services** (auth, ingestion, reporting, user-mgmt, assembler, router)
  → `Deployment` + `Service`, HPA on CPU/RPS, `PodDisruptionBudget`,
  liveness/readiness probes.
- **Workers** (nlp, llm) → `Deployment` on **GPU node pools** (nodeSelector +
  tolerations + `nvidia.com/gpu` resource requests), scaled by **KEDA** on Kafka
  consumer lag.
- **Model servers** (Triton + two vLLM deployments, LLM-A and LLM-B) →
  `Deployment`/`StatefulSet` on GPU pool, `Service` each for in-cluster gRPC/HTTP.
  Both LLMs are local; no egress to an external API.
- **Stateful infra** (Kafka, PostgreSQL, ClickHouse, Qdrant) → operators or
  `StatefulSet` + `PersistentVolumeClaim`; or managed equivalents.
- **Object storage** → MinIO operator or cloud S3.

**Autoscaling:**

- **KEDA** `ScaledObject` per worker pool with a Kafka-lag trigger (scale 0→N when
  topic lag > threshold) → workers track backlog, scale to zero between batches,
  surge for bursts.
- **HPA** for the API tier (CPU / requests-per-second).
- **Cluster Autoscaler / Karpenter** to add GPU nodes (incl. **spot**) for batch
  surges; batch tolerates preemption thanks to Kafka replay.

**Networking & security:** Ingress controller (NGINX) terminates TLS; cert-manager
for certs. NetworkPolicies (only ingress reaches services; only services reach
data; model servers reachable only by workers). Optional **Linkerd** mesh for mTLS +
retries + canary. Secrets via Kubernetes Secrets + Vault/External Secrets. Private
node pools for `data` and `models`; only ingress is internet-facing.

**Reliability:** Multi-AZ node pools; PodDisruptionBudgets; replicas ≥ 2 for
stateless. DLQ topic in Kafka; alert on DLQ growth. Rolling updates with readiness
gates; canary via mesh or two Deployments. Backups: Postgres PITR, ClickHouse +
Qdrant snapshots to object storage.

**CI/CD:** Build → scan images → push to registry → deploy via Helm/Kustomize (+
ArgoCD for GitOps). Model artifacts versioned in object storage; `model_versions`
recorded in every result for reproducible replay.

---

## 20. Implementation plan & roadmap

Phased build from MVP (1k) → Production (10k) → Enterprise (100k).

### Phase 0 — Foundations (week 0–1)

- Repo + monorepo layout (services, workers, infra, models, dashboard).
- Define the **input schema** (post + nested comment thread) and the **canonical
  output JSON schema** (§8), plus a JSON Schema validator shared by all services.
  These two contracts are the heart of the microservice; lock them early.
- Stand up local Docker Compose skeleton: Postgres, Redis, Qdrant, ClickHouse,
  MinIO, a stub API, Prometheus/Grafana.
- Pick and pin model versions (§14); download weights to object storage.
- Build a small **labeled eval set** per task and per language (bn / en / Banglish).

**Exit criterion:** a single post flows API → Redis Stream → stub worker → Postgres
→ `GET /analysis/{id}`, with valid JSON.

### Phase 1 — MVP (week 1–4) — 1,000 posts/batch

Goal: prove the hybrid pipeline and output quality end-to-end, cheaply.

- **Ingestion service:** validate the post+comment-thread payload, flatten the
  comment tree (keep `parent_id`), Unicode normalization, Bangla/English/Banglish
  script tagging, content-hash **dedup** (Redis), job creation, enqueue to Redis
  Streams.
- **Stage-1 NLP worker:** runs over the **post and each comment** —
  language/Banglish detection (fastText) → shared XLM-R encoder with
  sentiment/emotion/topic/intent heads → toxicity/hate → NER (GLiNER/spaCy) →
  embedding (bge-m3) → keywords. Aggregate per-comment sentiment into the thread
  `sentiment_breakdown`. Micro-batched. Emit confidence per field.
- **Router/Triage:** confidence gates + task flags; decide LLM routing.
- **Stage-2 LLM worker:** vLLM serving **LLM-A** (quantized 7B) for selective
  summarization/insight — local only, no API; **LLM response cache** in Redis.
  (LLM-B is added in Phase 2 for cluster/report quality.)
- **Result assembler:** merge + JSON-schema validate + write to Postgres, ClickHouse,
  Qdrant, MinIO.
- **APIs:** `/posts/upload`, `/analysis/run`, `/analysis/{id}`, `/reports` (basic),
  auth (API key + JWT).
- **Flutter dashboard (v1):** upload, job status, results table, basic charts.
- **Monitoring:** Prometheus + Grafana + Loki; track LLM-routing rate + cache hits.

**Exit criteria:** process 1,000-post batches reliably; measured LLM slice in single
digits %; per-task accuracy baselined on the eval set; cost-per-1k recorded.

### Phase 2 — Production hardening (week 4–10) — 10,000 posts/batch

Goal: scale, reliability, and the move to Kubernetes.

- **Migrate bus Redis Streams → Kafka** (partitioned by `hash(post_id)`); add **DLQ**
  topic + replay tooling.
- **Split services** (auth, ingestion, reporting, user-mgmt) into separate
  Deployments; introduce the Router as its own concern if not inlined.
- **Kubernetes** (§19): namespaces, GPU node pools, **KEDA** autoscaling on Kafka
  lag, HPA on API tier, NetworkPolicies, secrets, ingress + cert-manager.
- **Model serving upgrade:** Triton for the NLP fleet (dynamic batching), and add
  **LLM-B** — a quantized 14B/32B on a data-center GPU (L40S/A100) via vLLM —
  alongside LLM-A. Both local.
- **Reliability:** retries + backoff everywhere, idempotent assembler, circuit
  breakers around each LLM; if LLM-B is saturated, degrade to LLM-A or Stage-1-only
  (no external API fallback), PDBs, multi-replica.
- **Cluster summarization:** k-means/HDBSCAN over embeddings → LLM summarizes
  clusters, not posts (key cost lever at 10k).
- **Reporting:** trend analysis, brand-mention tracking, political analysis on
  ClickHouse; grounded report generation via RAG (Qdrant + LLM).
- **Observability:** OpenTelemetry traces → Jaeger; dashboards for throughput,
  cost-per-batch, LLM slice, cache hit rates, queue lag, DLQ size.
- **First fine-tune:** LoRA/QLoRA on router-flagged + labeled data; ship only if it
  beats baseline on the eval set (§14.4).

**Exit criteria:** 10,000-post batches within target latency; autoscaling proven
under burst; DLQ < threshold; LLM slice held in single digits %; per-language
accuracy improved over MVP baseline.

### Phase 3 — Enterprise scale (week 10+) — 100,000 posts/batch

Goal: horizontal scale, cost efficiency, resilience at volume.

- **GPU node pools per stage**, spot/preemptible for batch surges (Kafka replay
  makes preemption safe); reserved capacity for steady base.
- **Data layer scale-out:** Postgres read replicas; ClickHouse cluster
  (shards+replicas); Qdrant sharded by tenant; possibly Milvus if vectors reach
  billions (§13.5).
- **LLM scale:** multiple vLLM replicas for both local models (tensor parallelism
  for LLM-B); absorb spikes by adding GPU replicas, not by calling an external API.
- **Aggressive caching + dedup** tuning — the dominant cost lever at this scale.
- **Service mesh** (Linkerd) for mTLS + canary; GitOps (ArgoCD).
- **Continuous fine-tuning loop** + distillation to shrink the LLM slice further.
- **Multi-tenancy & quotas** fully enforced; per-tenant cost metering (`/usage`).

**Exit criteria:** 100k batches at target SLA; cost-per-1k flat or falling vs.
Production; graceful degradation under failure proven (chaos test).

---

## 21. Best practices for 10,000+ posts

1. **Never LLM-per-post.** Route by confidence; LLM only for what small models can't
   do or are unsure about (§7).
2. **Dedup hard.** Hash + near-duplicate (embedding) detection — social feeds are
   highly repetitive; cache hits are free results.
3. **Micro-batch on GPU.** Triton dynamic batching + vLLM continuous batching = high
   utilization, low cost per post.
4. **Cluster, then summarize.** One LLM call per cluster, not per post.
5. **Share one encoder across tasks.** Multiple classification heads on a single
   XLM-R pass instead of N independent models.
6. **Partition = parallelism.** Partition the bus by `hash(post_id)`; keep partitions
   ≥ consumers; scale workers on **lag**, not CPU.
7. **Stateless workers, durable queue.** Scale to zero between batches, surge for
   bursts; at-least-once + idempotent writes = no loss/double-count.
8. **Quantize everything servable.** INT8/FP16 NLP (ONNX/CTranslate2), AWQ/GPTQ LLM.
9. **Spot for batch.** Cheap GPUs for surges; Kafka replay covers preemption.
10. **Cache every layer.** Dedup, embeddings, LLM responses, query results.
11. **Backpressure + DLQ.** Bounded queues prevent cascades; DLQ captures poison
    messages for replay.
12. **Measure the right things.** % posts hitting LLM, cache hit rates, cost-per-1k,
    per-language accuracy, queue lag.
13. **Token minimization.** Send only needed fields + truncated text; structured
    JSON output mode.
14. **Reproducible replay.** Record `model_versions` per result; replay batches from
    Kafka after model upgrades.

---

## 22. Risk register

| Risk                                 | Mitigation                                                             |
| ------------------------------------ | ---------------------------------------------------------------------- |
| Bangla / Banglish accuracy below bar | Multilingual encoder + Bangla fine-tune + Banglish-heavy eval set      |
| LLM slice creeps up → cost spikes    | Confidence-gate tuning, caching, clustering, alert on LLM-share metric |
| GPU cost overrun                     | Spot for batch, reserved base, quantization, right-sizing              |
| Queue/worker overload                | KEDA on lag, bounded queues, backpressure, DLQ                         |
| Data privacy / PII                   | Encryption at rest/in transit, retention/deletion APIs, access audit   |
| Prompt injection via post text       | Treat post text as untrusted; never let it alter system instructions   |
| Model regression on upgrade          | Eval-set gate before ship; Kafka replay to compare                     |

```

```

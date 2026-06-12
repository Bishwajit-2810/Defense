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

An **existing social-media monitoring platform** scrapes 1,000+ real-time
Bangla/English/Banglish posts (Facebook in the current sample; others by URL host)
**with their comments embedded**, and exposes them as a single **post-with-details**
payload (post + `comments[]` + `engagement` + `reactionBreakdown` + `sampleShares`).
This service is a **smart, self-contained microservice** — a "thinking layer" —
that sits on top of that platform and:

- **pulls** the post-with-details payload into its **own separate database** —
  read-only consumer, no write-back (full input contract:
  [data_contract.md](data_contract.md), real sample
  [posts_with_details.json](posts_with_details.json)). The **comment thread is
  embedded**, so no separate fetch/join is needed.
- decides _per item_ how much intelligence each one needs (cheap NLP models vs. an
  LLM — this routing is the "smart" part), and
- returns one **structured JSON** object per thread (summary in the post's own
  language, sentiment, topics, intents, entities, brand mentions, comment
  analysis — see §8) that downstream projects consume directly.

Platform is **derived from each post's URL host**, so the service is
platform-agnostic. The upstream stores a coarse post `sentiment` and
`viralPotential` (kept as a **baseline**); **comment sentiment is empty and OCR is
no longer shipped — both are our job**, and the free `reactionBreakdown`
(LIKE/LOVE/HAHA/WOW/SAD/ANGRY/CARE) is a crowd emotion prior
(see [data_contract.md](data_contract.md) §4).

**Posts are multimodal, and that is the first target** (in order): (1) **text
sentiment** on the caption, (2) **image sentiment** from a _visual_ model on the
photo when present (we also OCR it), (3) **fuse** into the post's overall sentiment
(cross-checked against `reactionBreakdown`), (4) a **post summary grounded on
caption + image/OCR** (so a photo-only, `null`-caption post is still summarized),
then (5) **per-comment sentiment** over the embedded comments.

Hard product constraints from the owner: **fast**, **cost-effective**,
**efficient**, and **accurate on Bangla and Banglish** (with fine-tuning hooks for
when accuracy must improve). It must **scale** horizontally to handle batches of
1k → 10k → 100k threads.

The Stage-2 LLM is a **pluggable backend with two interchangeable providers —
`local` (self-hosted vLLM) and `groq` (Groq Cloud API) — switchable at runtime**
(see §14). The default backend is **local** (no per-token bill, no data egress);
**Groq** is an opt-in switch for fastest inference and zero GPU ops. The choice is
config- and request-level, so the operator can flip between them at any time
without redeploying.

**The unit of analysis is a _post together with its comment thread_, not an
isolated post.** The post arrives **with its `comments[]` embedded** (a stored
sample of `engagement.commentCount`; [data_contract.md](data_contract.md)) — no
separate fetch/join. The smart layer analyzes the whole thread and emits one JSON
object per thread, reporting comment **coverage** since only a sample is shipped.

---

## 2. TL;DR recommendation

- **Unit of analysis = post + its comment thread.** The service analyzes the whole
  thread and emits one JSON object per thread (see §8).
- **Hybrid analysis pipeline.** Cheap, fast NLP models (fastText, transformer
  classifiers, spaCy/GLiNER) run over the post and every comment and handle
  ~90–95% of the work. An LLM is invoked **selectively** only for the
  original-language summary, insight, and low-confidence/ambiguous cases. This is
  the central cost-control idea.
- **Two LLM roles, a pluggable backend (local ⇄ Groq), switchable at runtime.**
  The selective stage uses **LLM-A** (fast 7B/8B) for per-post refinement and
  **LLM-B** (larger 14B/32B) for cluster summarization, insight, and grounded
  reports. Each role is served by the chosen backend: **`local`** (self-hosted
  vLLM — no per-token bill, no data egress; the default) or **`groq`** (Groq Cloud
  API — fastest inference, zero GPU ops, per-token cost). Both speak an
  OpenAI-compatible API, so switching is a config/flag change, and you can run
  hybrid/failover. Pin privacy-sensitive tenants to `local`.
- **Queue-based, horizontally scalable.** API → ingestion → message bus →
  stateless GPU/CPU workers → result store. Workers scale independently per stage.
- **Queue: Kafka for Production/Enterprise, Redis Streams for the MVP.** Start
  simple, migrate when throughput and replay/retention demand it.
- **Storage split by access pattern:** PostgreSQL + pgvector (operational + jobs +
  vectors/semantic search/dedup, all in one store), ClickHouse
  (analytics/aggregations), Redis (cache +
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
   Clients                │  Web Dashboard (HTML/CSS/JS) │
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
     │ (JWT, FastAPI)  │        │ (FastAPI, dedup,  │       │ + Agent Orchestr.│  FastAPI
     └─────────────────┘        │  enqueue)         │       │ (read + agents)  │
                                └─────────┬─────────┘       └────────┬─────────┘
                                          │ produce                  │ read / agent tools
                                ┌─────────▼─────────┐       ┌─────────▼─────────────────┐
                                │   Message Bus     │       │ Agentic layer: AI agents  │
                                │ Kafka / Redis Str │       │ (LLM-B/VLM) + MCP servers │
                                │  (partitioned)    │       │ analytics·retrieval·ingest│
                                └─────────┬─────────┘       └─────────┬─────────────────┘
                                          │ consume                  │ read
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
        ┌──────────────────┬────────────────┼────────────────┬───────────────┐
   ┌────▼───────────┐   ┌──▼─────────┐      ┌▼────────────┐  ┌▼──────────┐
   │PostgreSQL      │   │ ClickHouse │      │   Redis     │  │  Object   │
   │ ops+jobs+vector│   │ analytics  │      │ cache/dedup │  │  storage  │
   │ (pgvector)     │   └────────────┘      └─────────────┘  └───────────┘
   └────────────────┘
```

The **Router/Triage** between Stage 1 and Stage 2 is the heart of the cost
strategy — see §7.

---

## 5. Data flow (end to end)

1. **Ingest (pull).** The Ingestion Service **pulls the post-with-details payload**
   (a campaign / time window / id) — post **with its `comments[]` embedded**, plus
   `engagement`, `reactionBreakdown`, `sampleShares`
   ([data_contract.md](data_contract.md)). It copies records into **our own
   database** (no write-back), derives `platform` from the URL host, keeps upstream
   `sentiment`/`viralPotential` as `baseline_*`, **runs OCR on `photoUrls`** (no OCR
   is shipped), normalizes the caption + OCR (Unicode NFC, emoji,
   Bangla/English/Banglish script tagging), takes the embedded comment thread (tracks
   `storedCommentRows` of `commentCount` — coverage), and computes a content hash
   over the post + comments. A pushed batch (`POST /v1/posts/upload`) is also
   accepted for external/replay sources.
2. **Dedup gate.** Hash is checked against Redis (recent) and Postgres
   (historical, including a pgvector similarity lookup over
   `analysis_results.embedding`). Exact duplicates short-circuit to the cached
   result; near duplicates (cosine `<=>` distance below threshold on embedding) can
   reuse prior analysis. This
   alone removes a large fraction of LLM/NLP work on real social feeds.
3. **Enqueue.** A `job` row is created in PostgreSQL (`status=queued`). One message
   per post is produced to the bus, partitioned by `post_id` hash so a post's
   ordering is stable and load spreads evenly.
4. **Stage 1 — Fast NLP + vision (parallel).** Worker pulls a batch
   (micro-batching). **Multimodal, post first, then comments:** the **text path**
   (caption + OCR) gets language/Banglish detection → **recomputed** `text_sentiment`,
   emotion, topic, intent, toxicity/hate, NER, keywords, embedding; the **vision
   path** runs a cheap **visual** model on each image → `image_sentiment` + a short
   image description (skipped for text-only posts); the two are **fused** into
   post-level `overall_sentiment`/`sentiment_score` (text-weighted when a caption
   exists; image + OCR-weighted for `null`-caption photo posts), **cross-checked
   against `reactionBreakdown`**. Upstream `sentiment`/`viralPotential` are retained
   as `baseline_*`. The **same text models** then run over **each embedded comment**
   (the upstream ships no comment sentiment), aggregated into the thread's
   `sentiment_breakdown` + themes (weighted by `likes`), reporting **coverage**
   (`analyzed` of `commentCount`). Each output carries a **confidence** score.
   Results are written to a partial-result store.
5. **Router/Triage (the "smart thinking layer").** For each thread the router
   decides:
   - All required fields produced with confidence ≥ threshold, and no LLM-only task
     requested → **mark complete**, skip Stage 2.
   - A `post_summary` is requested, or confidence is below threshold, or the thread
     is ambiguous / heavily Banglish / long → **route to Stage 2** with a compact,
     token-minimized prompt (post + a representative/clustered subset of comments,
     truncated). This selectivity is what keeps the service fast and cheap.
6. **Stage 2 — LLM / VLM (selective).** The Stage-2 worker produces summaries,
   insights, and refined labels through two **logical roles** — **LLM-A** (fast
   7B/8B) for per-post refinement and short summaries, **LLM-B** (large 14B/32B) for
   cluster summarization, insight, and report generation. **The `post_summary` is
   grounded on caption + OCR + image:** image posts run the summary on a
   **vision-language model (VLM)** that receives the caption, OCR, and the image (or
   the Stage-1 image description); text-only posts use the text LLM. Each role is
   served by a **pluggable backend, switchable at runtime**: `local` (self-hosted
   vLLM on our own GPUs — no per-token bill, no data egress) or `groq` (Groq Cloud
   API — fastest inference, zero GPU ops, per-token cost), each exposing a text and a
   vision model id. Both speak an OpenAI-compatible API, so the worker code is
   backend-agnostic; switching is a config/flag change, not a redeploy. Responses are
   cached by `(backend, model, task, content_hash)` in Redis so repeats are free.
7. **Assemble + validate.** The Result Assembler merges Stage 1 + Stage 2 into the
   canonical JSON (see §8), validates against the JSON Schema, and sets
   `confidence` = aggregate.
8. **Persist (fan-out to 3 backends).**
   - PostgreSQL + pgvector: job status, per-post status, the canonical result row,
     and the `analysis_results.embedding` `vector(768)` column (cosine `<=>`) for
     semantic search, clustering, and dedup.
   - ClickHouse: a denormalized analytics row for fast aggregations/trends.
   - Object storage: raw payload + any generated reports.
9. **Serve.** `GET /analysis/{id}`, `GET /reports`, and dashboard queries read from
   PostgreSQL (point lookups) and ClickHouse (aggregations).

---

## 6. Service breakdown

| Service                          | Responsibility                                                                                                                                                                        | Stack                                                                        | Scaling unit        |
| -------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------- | ------------------- |
| **API Gateway**                  | TLS termination, routing, rate limiting, request size limits, CORS                                                                                                                    | NGINX / K8s Ingress (+ optional Kong)                                        | replicas behind LB  |
| **Auth Service**                 | API keys, JWT issue/verify, RBAC, per-tenant quotas                                                                                                                                   | FastAPI + PostgreSQL + Redis                                                 | stateless replicas  |
| **Ingestion Service**            | Validate, normalize, dedup, create job, enqueue                                                                                                                                       | FastAPI (async)                                                              | stateless replicas  |
| **Stage-1 NLP + Vision Workers** | Text small-model suite **and the visual model + OCR on image posts** (`image_sentiment`, our OCR, description), and **per-comment sentiment** over the embedded thread, micro-batched | Python + Triton/ONNX/CTranslate2; SigLIP/CLIP + PaddleOCR + light VLM        | GPU/CPU worker pool |
| **Router/Triage**                | Apply confidence gates + task flags; decide LLM/VLM routing                                                                                                                           | lightweight Python service or in-worker rule module                          | stateless           |
| **Stage-2 LLM/VLM Workers**      | Selective summarization (text **and image-grounded via a VLM**) / insight / report / hard cases                                                                                       | Thin worker → local vLLM (LLM-A+LLM-B + VLM) **or** Groq API (text + vision) | GPU pool, stateless |
| **Result Assembler**             | Merge, JSON-schema validate, compute aggregate confidence                                                                                                                             | Python consumer                                                              | stateless replicas  |
| **Reporting/Query Service**      | Read APIs, report generation, exports                                                                                                                                                 | FastAPI + ClickHouse + PostgreSQL                                            | stateless replicas  |
| **Agent Orchestrator**           | Selective **AI agents** (insight/analyst, coverage deep-dive, alerting) — corpus/report tier only, never per-post (§14.6)                                                             | FastAPI + agent loop → LLM-B/VLM backend + MCP tools                         | stateless replicas  |
| **MCP Servers**                  | Standardized tools for the agents: `analytics-mcp` (ClickHouse/Postgres), `retrieval-mcp` (pgvector), `ingest-mcp` (upstream pull / more comments)                                    | FastAPI + MCP SDK (internal)                                                 | stateless replicas  |
| **User Management**              | Tenants, users, roles, billing/usage metering                                                                                                                                         | FastAPI + PostgreSQL                                                         | stateless replicas  |

Workers are split into **separate pools per stage** so the expensive LLM GPUs
scale independently from the cheap NLP fleet. The Stage-2 worker itself is a thin
client that calls the configured **LLM backend**: with the `local` backend it
talks to in-cluster vLLM servers (GPU-bound); with the `groq` backend it makes
outbound HTTPS calls to Groq and needs no GPU at all (a stateless pool that scales
on CPU/queue depth). The backend is selected by config and can be flipped at
runtime per the routing rules in §7.

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
  Cluster embeddings (pgvector + k-means/HDBSCAN), then have the LLM summarize a
  _cluster_ or representative samples → one LLM call per cluster, not per post.
- **Token minimization.** Send only truncated, cleaned text and only the fields the
  LLM must produce. Use structured/JSON-mode output to avoid wasted tokens.
- **Two LLM roles, pluggable backend.** LLM-A (fast) absorbs per-post work; the
  larger LLM-B is reserved for low-volume cluster/report generation. Each role is
  served by the configured backend: `local` (vLLM, continuous batching — no
  per-token bill, no data egress) or `groq` (Groq Cloud — fastest inference, no GPU
  to run, billed per token). The backend is switchable at runtime, so the operator
  can run fully local for cost/privacy, burst to Groq under load, or mix (e.g.
  local LLM-A + Groq for LLM-B reports).
- **LLM response cache** keyed by `(backend, model, task, content_hash)`.

Expected outcome: LLM touches a single-digit-to-low-double-digit percentage of
posts, and the per-batch LLM bill is dominated by _cluster-level_ generation, not
per-post calls.

---

## 8. Canonical output JSON

The assembler emits and validates this schema. `overall_sentiment`/`sentiment_score`
are a **fusion of `text_sentiment` (caption) and `image_sentiment` (the photo)**,
cross-checked against `reaction_breakdown` ([data_contract.md](data_contract.md) §4);
`image_sentiment`/`image_analysis` are `null` for text-only posts and
`text_sentiment` is `null` for `null`-caption posts. `post_summary` is **grounded on
caption + OCR + image** and written **in the post's own language**.
`comment_analysis.coverage` reports `analyzed / commentCount`. Provenance:
`post_id`/`campaign_id`/`platform_post_id`/`media_type`/`baseline_*`/
`reaction_breakdown`/`shares` come from the upstream payload; `platform` is derived;
everything else — including **all comment sentiment** and **OCR** — is computed by us.
(Real example below: the Bangla photo+text Shapla post.)

```json
{
  "post_id": "cmosjpp9305n0u9tskgmd1c4k",
  "campaign_id": "cmoldmxzr02d8fu22vhvrg23c",
  "platform": "facebook",
  "platform_post_id": "4460219584209360",
  "url": "https://www.facebook.com/4460219584209360",
  "media_type": "PHOTO_TEXT",
  "language": "bn",
  "language_mix": ["bn"],
  "language_confidence": 0.98,
  "post_type": "commemoration",
  "post_summary": "শাপলা চত্বরের ঘটনার স্মরণে একটি আবেগঘন বাংলা পোস্ট; ছবিতে সেই রাতের দৃশ্য। পোস্ট ও মন্তব্যে শোক এবং আওয়ামী লীগের প্রতি ক্ষোভ প্রবল।",
  "post_summary_lang": "bn",
  "post_summary_grounding": ["caption", "image"],
  "overall_sentiment": "negative",
  "sentiment_score": -0.82,
  "text_sentiment": { "label": "negative", "score": -0.85 },
  "image_sentiment": {
    "label": "negative",
    "score": -0.7,
    "per_image": [-0.7]
  },
  "baseline_sentiment": -0.85,
  "baseline_viral_potential": 0.78,
  "emotion": "sadness",
  "intents": ["commemorate", "express_grievance"],
  "topics": ["shapla chattar", "2013", "politics", "grief"],
  "entities": [
    { "type": "event", "value": "Shapla Chattar", "confidence": 0.9 },
    { "type": "organization", "value": "Awami League", "confidence": 0.86 }
  ],
  "brand_mentions": [],
  "keywords": ["শাপলা", "শোক", "আওয়ামী লীগ"],
  "toxicity_score": 0.18,
  "hate_speech_score": 0.12,
  "engagement": {
    "reactions": 84979,
    "comment_count": 1562,
    "share_count": 3189,
    "stored_comments": 112
  },
  "reaction_breakdown": {
    "SAD": 65289,
    "LIKE": 18235,
    "LOVE": 682,
    "HAHA": 566,
    "CARE": 125,
    "WOW": 56,
    "ANGRY": 26
  },
  "shares": { "sample_count": 0, "sample": [] },
  "image_analysis": {
    "image_count": 1,
    "ocr_text": "",
    "description": "a dark night-time scene of a crowd / security forces",
    "images": [
      {
        "ref": "photoUrls[0]",
        "sentiment": { "label": "negative", "score": -0.7 },
        "ocr_text": "",
        "description": "a dark night-time scene of a crowd / security forces"
      }
    ],
    "vision_model": "SigLIP (sentiment) + Qwen2.5-VL-7B (description+OCR)"
  },
  "comment_analysis": {
    "analyzed": 112,
    "coverage": "112/1562 stored",
    "sentiment_breakdown": { "positive": 6, "negative": 89, "neutral": 17 },
    "themes": [
      "grief and remembrance",
      "anger at Awami League",
      "calls for justice"
    ],
    "representative_comments": [
      {
        "author": "Abdur Rahman Wisdom's",
        "lang": "bn",
        "sentiment": "negative",
        "likes": 574,
        "text": "এই ছবিগুলো প্রমাণ করে যে পুলিশ আমাদের বন্ধু ছিল না কখনো।"
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
  "created_at": "2026-05-04T18:19:14",
  "scraped_at": "2026-05-05T17:39:44.464"
}
```

Notes:

- **`sentiment_analysis` the owner asked for** is **multimodal**: `text_sentiment`
  (caption), `image_sentiment` (the photo, via a visual model), and the **fused**
  post-level `overall_sentiment` + `sentiment_score` — all **recomputed** — plus
  **our** per-comment `comment_analysis.sentiment_breakdown` across the embedded
  thread (the upstream ships none). `image_*` are `null` for text-only posts;
  `text_sentiment` is `null` for `null`-caption posts. The upstream coarse **post**
  score is kept as `baseline_sentiment` (and `baseline_viral_potential`), never
  overwritten ([data_contract.md](data_contract.md) §4).
- **`reaction_breakdown`** is a free crowd **emotion signal** (SAD/ANGRY vs
  HAHA/LOVE) used to cross-check sentiment; **`shares`** carries `sampleShares`;
  `engagement.stored_comments` + `comment_analysis.coverage` make comment
  **sampling** explicit.
- **`image_analysis`** holds per-image visual `sentiment`, `ocr_text` (**our** OCR —
  the payload no longer ships it), and a `description` that grounds the summary.
- **`media_type`** (upstream `postType`: TEXT/PHOTO/PHOTO_TEXT) is distinct from the
  semantic `post_type`.
- `post_summary` is **grounded on caption + OCR + image** (`post_summary_grounding`);
  `post_summary_source` is `vlm` when a vision-language model produced it, else `llm`.
- `post_type`, `intents`, `brand_mentions`, and `comment_analysis.themes` are the
  "and something like that" fields — useful structured signal for downstream use.
- `post_summary_source` and
  `processing.llm_used`/`llm_role`/`llm_backend`/`llm_model` make the hybrid
  behavior auditable (did we call an LLM, which role, on which backend — `local`
  or `groq` — which concrete model, was it worth it).
- All fields except `post_summary`, `comment_analysis.themes`, and
  `representative_comments` come from cheap Stage-1 NLP; the LLM fills only the
  generative fields when the router asks for them.

The flat schema the owner sketched (`post_id, platform, language, sentiment,
emotion, topics, entities, keywords, toxicity_score, summary, confidence,
created_at`) is preserved as a subset of the above richer object.

---

## 9. Technology stack

| Layer          | Choice                                                                                         | Why                                                                                                                                                     |
| -------------- | ---------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------- |
| API services   | **Python + FastAPI** (async)                                                                   | Matches team skills; great for I/O-bound APIs and ML glue                                                                                               |
| Workers        | **Python**, Celery or Ray for orchestration                                                    | Native ML ecosystem; Ray scales to multi-node cleanly                                                                                                   |
| Model serving  | **Triton/ONNX/CTranslate2** (NLP); **SigLIP/CLIP + VLM** (vision); Stage-2 **vLLM**⇄**Groq**   | High GPU util for NLP; cheap visual sentiment + a VLM for image-grounded summaries; Stage-2 backend switchable local↔Groq                               |
| Agents + tools | **Agent orchestrator** (FastAPI) on the LLM-B/VLM backend; **MCP servers** (FastAPI + MCP SDK) | Tool-using agents for corpus-level insight; MCP standardizes tools over our stores + upstream (§14.6); backend models (Qwen/Llama) support tool calling |
| Message bus    | **Kafka** (prod), **Redis Streams** (MVP)                                                      | Durable, partitioned, replayable at scale; simple to start                                                                                              |
| Operational DB | **PostgreSQL**                                                                                 | ACID jobs/state, JSONB flexibility, mature                                                                                                              |
| Analytics DB   | **ClickHouse**                                                                                 | Columnar, billions of rows, sub-second aggregations for trends                                                                                          |
| Vector store   | **pgvector (Postgres extension)**                                                              | `analysis_results.embedding vector(768)`, cosine `<=>` — dedup/search/clustering with no extra service; one DB to operate                               |
| Cache          | **Redis**                                                                                      | LLM/embedding/query cache, dedup set, rate limits                                                                                                       |
| Object storage | **S3 / MinIO**                                                                                 | Raw payloads, reports, model artifacts                                                                                                                  |
| Orchestration  | **Kubernetes** (prod), **Docker Compose** (MVP)                                                | Autoscaling + HA vs simplicity                                                                                                                          |
| Autoscaling    | **KEDA** (scale on queue depth) + HPA                                                          | Workers track backlog, not just CPU                                                                                                                     |
| Observability  | **Prometheus + Grafana + Loki + OpenTelemetry + Jaeger**                                       | Metrics, logs, traces                                                                                                                                   |
| Frontend       | **Plain HTML + CSS + JavaScript** (vanilla, no framework)                                      | Simple static dashboard served from a CDN/static host; calls the read APIs directly; no build step or framework runtime                                 |

---

## 10. Reliability & fault tolerance

- **Retries with backoff** at every worker; transient failures (OOM, GPU hiccup,
  local vLLM 5xx, or Groq 429/5xx) retried up to N times. Groq rate-limit (429)
  responses honor `Retry-After` and back off per-key.
- **Dead-letter queue (DLQ).** Messages that exhaust retries go to a DLQ topic with
  the error context for inspection/replay.
- **Idempotency.** Content hash + job id make reprocessing safe; assembler upserts.
- **At-least-once delivery** from the bus + idempotent writes = no lost or
  double-counted posts.
- **Graceful degradation + backend failover.** If the active backend is saturated
  or unhealthy, the router can (a) queue, (b) degrade LLM-B work to LLM-A (lower
  quality), (c) **fail over to the other backend** — local↔Groq — when both are
  configured, or (d) return Stage-1-only results flagged `summary_source:
"skipped"`. Failover is policy-driven: a privacy-locked tenant can be pinned to
  `local` and will never spill to Groq even under load (it degrades to
  Stage-1-only instead). Never block the whole batch.
- **Health/readiness probes** on every service; circuit breakers around each LLM
  backend (per local vLLM server and around the Groq endpoint).

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
Context for every choice: a **self-hosted microservice** ingesting post+comment
threads (Bangla/English/Banglish), under hard constraints — fast, cheap,
Bangla-accurate. The data stores and NLP fleet are self-hosted (no data egress
there). The **Stage-2 LLM is the one pluggable piece**: a switchable backend,
`local` (self-hosted vLLM, default — no egress, no per-token bill) ⇄ `groq` (Groq
Cloud API — fastest, no GPU, per-token). See §13.8.

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
later _only if_ rich full-text search over post text becomes first-class; pgvector +
ClickHouse cover semantic search and aggregation meanwhile.

### 13.5 Vector store: pgvector (in Postgres) vs a standalone vector DB

| Criterion        | **pgvector (in Postgres)**         | **Weaviate**            | **Milvus**               |
| ---------------- | ---------------------------------- | ----------------------- | ------------------------ |
| Language/perf    | C, in-Postgres; HNSW/IVFFlat       | Go, feature-rich        | C++, very scalable       |
| Filtering        | Full SQL `WHERE` + joins on rows   | Good                    | Good                     |
| Ops simplicity   | **Highest** (no extra service)     | Medium                  | Lower (more components)  |
| Built-in modules | Lean (BYO embeddings)              | Many (modules, hybrid)  | Lean                     |
| Scale ceiling    | High (millions–low tens of M)      | High                    | **Very high** (billions) |
| Best fit         | One-DB simplicity, easy ops        | Hybrid search + modules | Massive enterprise scale |

**Decision: pgvector** for MVP→Production: the embedding lives as the
`analysis_results.embedding vector(768)` column right alongside the operational
rows, queried with the cosine `<=>` operator. No separate vector service to run,
back up, or secure; metadata filtering is just SQL `WHERE`/joins (scope vectors by
tenant/platform/time), and dedup/search/clustering reuse the same Postgres
connection pool. Reassess **Milvus** at
Enterprise/100k scale if vector count reaches billions and you need distributed
sharding. Weaviate only if you want its built-in hybrid-search/module ecosystem
over BYO simplicity.

### 13.6 Caching layer composition

All complementary, not alternatives: **Redis** (LLM/embedding/query cache, dedup
set, rate-limit counters), **embedding cache** (keyed by `content_hash`), **LLM
response cache** (keyed by `(backend, model, task, content_hash)`), **query cache**
(short-TTL ClickHouse aggregations), **CDN** (static HTML/CSS/JS dashboard assets +
static report exports; not dynamic per-tenant data). See §15.4.

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

### 13.8 LLM serving: a pluggable backend (local ⇄ Groq), and two LLM roles

**The Stage-2 LLM runs behind a pluggable backend, switchable at runtime.** Two
interchangeable providers fulfill the same two LLM roles; the operator picks one
(or mixes them) by config/flag without redeploying. The NLP fleet and all data
stores remain self-hosted regardless.

| Backend option                 | Pros                                                                  | Cons                                                               | Verdict                                             |
| ------------------------------ | --------------------------------------------------------------------- | ------------------------------------------------------------------ | --------------------------------------------------- |
| **`local` — self-hosted vLLM** | No per-token bill; data stays in-cluster; fine-tunable; no rate limit | Needs GPUs + serving ops; capacity = GPUs you run                  | **Default.** Privacy/cost-sensitive, steady volume  |
| **`groq` — Groq Cloud API**    | Fastest inference (LPU); zero GPU/model ops; elastic burst; tiny MVP  | Per-token cost; prompt data leaves the cluster; rate limits/quotas | **Opt-in switch.** Bursty load, no GPU, low latency |
| **Both (hybrid / failover)**   | Local for steady/private work, Groq for burst or report spikes        | Two integrations to keep configured; per-tenant policy needed      | **Recommended** where data policy allows            |

Rejected: a **single LLM** (either overpay running a big model per post, or
under-deliver running a small model on reports) — so we keep **two roles** instead.
**LLM-A** — fast 7B/8B for **high-volume, low-difficulty** per-post refinement and
short summaries. **LLM-B** — larger 14B/32B for **low-volume, high-quality** cluster
summarization, corpus insight, and grounded report generation (RAG). Each role maps
to a `local` model (vLLM) or a `groq` model ID — same prompts, same JSON schema, so
a backend switch needs no code or prompt changes. On `local` at MVP scale the two
roles time-slice one GPU (or run LLM-A alone until volume justifies LLM-B); on
`groq` they are just two model IDs with no infrastructure. **Privacy note:** pin
sensitive tenants to `local` so a runtime switch can never send their data to Groq.

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
backed by pgvector + the embeddings you already compute. Full reasoning in §14.5.

---

## 14. AI model selection, RAG & fine-tuning

All small-model choices favor open-source, GPU-efficient models with genuine
Bangla support and run **self-hosted**. The Stage-2 LLM runs behind a **pluggable
backend with two interchangeable providers — `local` (self-hosted vLLM) and `groq`
(Groq Cloud API) — switchable at runtime** (see §14.2); default is `local` (no
per-token bill, no data egress), `groq` is an opt-in switch for fastest inference
and zero GPU ops. **Small models do the bulk work; the LLM is selective.** The
input is a post + its comment thread, heavily **Banglish** (romanized Bangla mixed
with English, e.g. "Green garden e vat 25 taka baire 10 taka"). Every choice is
judged on **bn + en + code-mixed Banglish**.

### 14.1 Model recommendations per task

| Task                                       | Recommended model(s)                                                                 | Bangla | English | Notes                                                                                                                                                                   |
| ------------------------------------------ | ------------------------------------------------------------------------------------ | ------ | ------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Language + Banglish detection**          | `fastText lid.176` + CLD3 + transliteration heuristic                                | ✅     | ✅      | <1 ms/item; flags `banglish` (romanized bn) → multilingual path                                                                                                         |
| **Text sentiment** (caption + per comment) | `XLM-RoBERTa`/`mBERT` fine-tuned; BanglaBERT for bn                                  | ✅     | ✅      | **Recompute** → `text_sentiment` + `sentiment_breakdown`. Post `sentiment` kept as `baseline_sentiment`; **comment sentiment is entirely ours** (upstream ships `null`) |
| **Image sentiment** (visual)               | `SigLIP 2` / `CLIP` zero-shot, or fine-tuned ViT                                     | n/a    | n/a     | Cheap Stage-1 model on **every image post** → `image_sentiment`; visual, fused with text sentiment                                                                      |
| **Image description / caption**            | small **VLM** (`Qwen2.5-VL-3B/7B`) or `BLIP-2`                                       | ✅     | ✅      | Short image description → grounds `post_summary` (esp. `null`-caption posts)                                                                                            |
| **OCR (image text)**                       | **ours** — `PaddleOCR` / `Tesseract` (bn+en), or the VLM                             | ✅     | ✅      | Payload no longer ships OCR, so we **run it ourselves** on `photoUrls`; folded into the text path                                                                       |
| **Emotion**                                | XLM-R fine-tuned (joy/anger/sadness/fear/…); GoEmotions heads for en                 | ✅     | ✅      | Shares encoder with sentiment; **cross-checked against `reactionBreakdown`**                                                                                            |
| **Topic classification**                   | XLM-R / embedding + classifier head; or zero-shot via small NLI model                | ✅     | ✅      | Use embeddings + lightweight classifier; reduces per-label models                                                                                                       |
| **Intent**                                 | XLM-R fine-tuned (inform/promote/complain/request/…)                                 | ✅     | ✅      | Per comment too (price/availability/location inquiries)                                                                                                                 |
| **Toxicity / hate / offensive**            | `XLM-R`/`mBERT` fine-tuned; Detoxify (en) + Bangla hate datasets                     | ✅     | ✅      | Bangla hate-speech corpora exist (e.g. Bengali Hate Speech); fine-tune                                                                                                  |
| **NER (person/org/location/brand)**        | `GLiNER` (multilingual, zero/few-shot), `spaCy` (en), BanglaBERT-NER (bn)            | ✅     | ✅      | GLiNER gives flexible entity types without per-type models                                                                                                              |
| **Embeddings**                             | `BAAI/bge-m3` (multilingual, incl. Bangla) or `intfloat/multilingual-e5`             | ✅     | ✅      | Powers dedup, comment clustering, semantic search, RAG                                                                                                                  |
| **Summarization** (multimodal)             | text: **LLM-A**/**LLM-B**; image posts: a **VLM** (`Qwen2.5-VL` ⇄ Groq vision) §14.2 | ✅     | ✅      | Selective; **grounded on caption + OCR + image**; original language                                                                                                     |
| **Insight / report generation**            | **LLM-B** role + RAG, on the active backend (see §14.5)                              | ✅     | ✅      | Cluster summaries → corpus-level insight                                                                                                                                |
| **Keyword extraction**                     | KeyBERT (on embeddings) / YAKE                                                       | ✅     | ✅      | Cheap, no extra GPU model                                                                                                                                               |

**Bangla-specific resources:** BanglaBERT (csebuetnlp), XLM-RoBERTa / mBERT (handle
code-mixed Banglish reasonably), bge-m3 / multilingual-e5 embeddings. **Banglish is
the dominant comment style**, not an edge case — it gets first priority in the
labeled eval set and any fine-tuning; an optional transliteration normalizer
(Banglish → Bangla script) can be added before classification if accuracy requires.

**Why a shared multilingual encoder (XLM-R family):** one encoder pass feeds
multiple lightweight task heads (sentiment, emotion, intent, topic), cutting GPU
cost vs. a separate full model per task, and handles code-mixed Bangla-English in a
single model.

### 14.2 The selective LLMs — two roles, a pluggable backend (local ⇄ Groq)

The selective Stage-2 work is split across **two logical roles** — **LLM-A** (fast
/ high-throughput) and **LLM-B** (large / high-quality). Each role is fulfilled by
a **pluggable backend, switchable at runtime**:

- **`local` (default)** — self-hosted models on **vLLM**, on our own GPUs. Post
  content never leaves the cluster and there is no per-token bill; capacity grows
  by adding GPUs. Best for privacy-sensitive data, steady high volume, and
  predictable cost.
- **`groq`** — the **Groq Cloud API** (OpenAI-compatible), serving the same open
  model families (Llama 3.x, Qwen, etc.) on Groq's LPU hardware. Extremely fast
  inference, **zero GPU/model-serving ops**, elastic burst — billed per token, and
  prompt content leaves the cluster. Best for bursty load, no/low local GPU, or
  when you want the lowest latency.

Because both backends speak the **same OpenAI-compatible API**, the Stage-2 worker
is backend-agnostic — only the base URL, API key, and model name differ. Switching
is a config/flag change (env `LLM_BACKEND=local|groq`, or a per-request override —
see §17), applied **at runtime without a redeploy**. You can even run **hybrid**:
e.g. `local` LLM-A for per-post volume + `groq` for LLM-B report bursts, or fail
over local→Groq under load.

| Role                               | `local` backend (vLLM, our GPUs)                                               | `groq` backend (Groq Cloud API)                      | Serves                                                                             |
| ---------------------------------- | ------------------------------------------------------------------------------ | ---------------------------------------------------- | ---------------------------------------------------------------------------------- |
| **LLM-A — fast / high-throughput** | `Qwen2.5-7B-Instruct` (or `Llama-3.1-8B-Instruct`), AWQ/GPTQ quantized         | a fast Groq model (e.g. `llama-3.1-8b-instant`)      | Per-post selective refinement, hardest classification, short single-post summaries |
| **LLM-B — large / high-quality**   | `Qwen2.5-32B-Instruct` (or `Qwen2.5-14B-Instruct` at smaller scale), quantized | a larger Groq model (e.g. `llama-3.3-70b-versatile`) | Cluster summarization, corpus insight, grounded report generation (RAG)            |
| **VLM — vision-language**          | `Qwen2.5-VL-7B-Instruct` (or `-3B` at MVP), on vLLM                            | a Groq vision/multimodal model id                    | **Image-grounded `post_summary`** (caption + OCR + image); image description       |

Groq model IDs change as their catalog evolves — treat the examples above as
placeholders and pin the current IDs in config. The prompts and JSON output schema
are identical across backends, so a switch needs no prompt changes.

Why two roles and not one: per-post refinement is **high-volume, low-difficulty**
(favor a small fast model); cluster/report generation is **low-volume,
high-quality** (favor a larger model). Splitting lets each be right-sized and
scaled independently. On the `local` backend at MVP scale the two can time-slice
one GPU, or LLM-B can be dropped and LLM-A used for both until volume justifies the
second model; on the `groq` backend the two roles are just two model IDs with no
extra infrastructure. Both backends use **structured JSON output mode** to minimize
tokens. If a backend is saturated or unhealthy the router degrades LLM-B→LLM-A,
**fails over to the other backend** (when both are configured), or returns
Stage-1-only results (see §10). Privacy-locked tenants can be pinned to `local` so
a switch never routes their data to Groq.

### 14.3 Model serving architecture

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

- **NLP models** → **Triton Inference Server** (or ONNX Runtime / CTranslate2) with
  dynamic batching and INT8/FP16 quantization; multiple models share GPUs. (NLP is
  always self-hosted — only the Stage-2 LLM has a Groq option.)
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
  `model_versions` so re-runs (and a backend switch) are auditable and reproducible
  (replay from Kafka).

### 14.4 Fine-tuning strategy (Bangla + English)

1. **Start zero-shot / off-the-shelf.** Ship MVP with pretrained multilingual
   models (XLM-R, GLiNER, bge-m3). Establish baselines and a labeled eval set.
2. **Collect & label.** Use the platform's own low-confidence/router-flagged posts
   as an active-learning pool. Build a held-out eval set per task and per language.
3. **Parameter-efficient fine-tuning (LoRA/QLoRA).** Fine-tune the shared XLM-R
   encoder + task heads, and optionally LLM-A (the 7B/8B model) on the **`local`
   backend**. Cheap, fast, versionable; fine-tuned weights stay in-house. This is a
   **local-backend advantage**: the `groq` backend runs Groq's hosted models, which
   we can steer only via prompting/few-shot, not custom fine-tunes — so tasks that
   need fine-tuned LLM behavior should stay on `local` (the NLP fleet is fine-tuned
   regardless of which LLM backend is active).
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
people saying about Brand X this week?" (retrieve relevant posts from pgvector, feed
to the LLM grounded), grounded report/insight generation across the corpus, and
cluster summarization with citations. **Benefits:** grounded, citation-able answers
without stuffing the whole corpus into context; reuses embeddings you already
compute. **Drawbacks:** retrieval-quality dependent; mitigate with good chunking,
metadata filters, showing source posts. **Stack:** pgvector + bge-m3/multilingual-e5
embeddings + the **LLM-B role** for generation on whichever Stage-2 backend is
active — `local` for fully in-cluster RAG, or `groq` for faster report generation
when the retrieved context may leave the cluster. Retrieval and embeddings stay
local in both cases; only the final generation call follows the chosen backend.

### 14.6 Agentic insight layer — MCP servers + AI agents

The per-post / per-comment pipeline stays **deterministic NLP + single-shot
LLM/VLM** (that's the cost model). **Above** it sits a small, **selective agent
layer** for corpus-level work that needs planning + multiple tool calls — analyst
Q&A, grounded reports, targeted deep-dives. **Agents never run per post.**

- **MCP servers** (Model Context Protocol — standardized tools, small FastAPI
  services): **`analytics-mcp`** (ClickHouse trends/aggregations + Postgres lookups),
  **`retrieval-mcp`** (pgvector semantic search + post/thread fetch), **`ingest-mcp`**
  (trigger an upstream post-with-details pull / fetch more comments to raise coverage).
  One consistent tool interface, same auth/tenant scoping; read-mostly (`ingest-mcp`
  writes only into our own DB, never upstream).
- **AI agents** (LLM-B on the pluggable `local`⇄`groq` backend — Qwen/Llama both do
  tool calling; a **VLM** step when images matter):
  - **Insight/Analyst agent** — `trend_query → semantic_search → get_thread →
synthesize → cite`; generates reports and answers `POST /v1/agents/query`,
    replacing single-shot RAG with a grounded tool-using loop.
  - **Coverage deep-dive agent** — when `comment_analysis.coverage` is low or a post
    is flagged viral, calls `ingest-mcp.fetch_more_comments`, re-runs the comment
    pass, escalates.
  - **Alerting agent** (scheduled) — watches `reaction_breakdown` spikes / sentiment
    shifts / viral signals and raises alerts.
- **Guardrails:** invoked by request/schedule/router escalation (not per post);
  per-run tool-call + token budgets; cached by `(agent, inputs, backend, model)`;
  grounded + cited + auditable (records backend/model/tools/tokens → `/v1/usage`);
  privacy-locked tenants keep agent calls on `local`. Not required for the MVP —
  lands with the reporting/insight phase (§20).

---

## 15. Infrastructure — GPU, monitoring, caching, scaling

### 15.1 Throughput model

A unit is a post + its comment thread, so a single "item" can be a handful to
hundreds of short texts — Stage-1 throughput is better measured in **texts
(post + comments) per second** rather than threads/sec.

- **Stage-1 NLP** (shared XLM-R encoder + heads + embedding), batched on GPU: order
  of **hundreds–low-thousands of texts/sec** on a single modern GPU. A thread with
  50 comments = ~51 texts.
- **Stage-2 LLMs** (two roles — **LLM-A** 7B/8B for per-post refinement, **LLM-B**
  14B/32B for cluster/report generation): order of **thousands of output tokens/sec**
  aggregate; but they only see the **selective slice** (single-digit % of posts) and
  mostly **cluster-level** calls. This throughput is delivered by the configured
  backend: `local` (vLLM continuous batching on our GPUs — sized below) or `groq`
  (Groq LPU — throughput is Groq's to scale, bounded by your rate-limit/quota rather
  than your GPUs).

The NLP fleet dominates raw text throughput; the LLM dominates _quality_ work on a
small slice. Size them independently — and note the `groq` backend removes LLM GPU
sizing from the equation entirely (you size only the NLP fleet).

### 15.2 GPU requirements by stage

**Backend choice changes the GPU need.** On the **`local`** backend you size a GPU
for the Stage-2 LLM (below). On the **`groq`** backend the LLM runs on Groq — **no
LLM GPU at all** — so the Stage-2 worker pool is CPU-only and you size only the NLP
fleet (mind Groq rate-limits/quotas as the throughput ceiling instead of GPU count).

- **MVP — 1,000 posts/batch:** on `local`, 1 × consumer GPU (RTX 4090/3090, 24 GB)
  runs the whole show: NLP suite (quantized) + local LLMs via vLLM, time-sliced —
  run just **LLM-A** (quantized 7B) for all Stage-2 work; add **LLM-B** when
  cluster/report quality demands it. On `groq`, the MVP can run NLP on a small GPU
  or even CPU-only and call Groq for summaries (cheapest way to stand up an MVP
  without owning a GPU). Docker Compose.
- **Production — 10,000 posts/batch:** NLP fleet 1–2 GPUs (24 GB consumer or L4/A10)
  with Triton dynamic batching, autoscaled by queue depth. **LLMs (`local`):**
  **LLM-A** (quantized 7B) on an L4/A10 + **LLM-B** (quantized 14B/32B) on a
  data-center GPU (**L40S / A100 40GB**); at lower volume LLM-A can share the NLP
  GPU. **LLMs (`groq`):** none of the above — the Stage-2 worker pool is CPU-only.
  Small K8s cluster, 2–4 GPUs on `local` (just the NLP GPUs on `groq`), KEDA.
- **Enterprise — 100,000 posts/batch:** NLP fleet several data-center GPUs (A10 /
  L4 / L40S), autoscaled. **LLMs (`local`):** multiple **A100/H100** (or several
  L40S) behind vLLM serving both models, multi-replica with tensor parallelism for
  LLM-B; scale out by adding GPU replicas. **LLMs (`groq`):** no LLM GPU pool —
  scale the stateless Stage-2 workers and negotiate a Groq quota that covers peak. A
  **hybrid** is common at this scale: own GPUs for the steady base, burst overflow
  to Groq during spikes instead of over-provisioning GPUs. Multi-node K8s, GPU node
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
  exporter, `local` backend), LLM-routing rate, cache hit rate, error rate,
  p50/p95/p99 latency, DLQ size. **Per-backend LLM metrics:** calls and latency
  split by `backend` (local/groq); on `groq` also **tokens in/out + estimated cost**,
  HTTP 429 rate-limit hits, and Groq API error rate; on `local` also vLLM
  queue/KV-cache utilization. A **backend label** on every LLM metric makes a
  runtime switch visible on the dashboards.
- **Grafana** — dashboards (throughput, cost-per-batch, LLM slice %) + alerting
  (Alertmanager): queue lag, DLQ growth, GPU saturation, error-rate spikes.
- **Loki** — centralized structured logs, correlated to traces by trace_id.
- **OpenTelemetry** — single instrumentation standard; exports metrics→Prometheus,
  logs→Loki, traces→Jaeger.
- **Jaeger** — distributed traces follow a post from ingestion → NLP → router →
  (LLM) → assembler → store.

**Key product metrics:** % of posts that hit the LLM, cache hit rates
(dedup/embedding/LLM), cost per 1k posts (GPU-amortized on `local`, token-billed on
`groq`), the active **LLM backend mix** (local vs groq share), and per-language
accuracy drift.

### 15.4 Caching strategy

| Cache                          | Key                                    | Purpose                                                        | TTL                  |
| ------------------------------ | -------------------------------------- | -------------------------------------------------------------- | -------------------- |
| **Dedup set** (Redis)          | `content_hash`                         | Skip re-analysis of exact dupes/reshares                       | long / per-retention |
| **Embedding cache** (Redis)    | `content_hash`                         | Avoid recomputing vectors                                      | long                 |
| **LLM response cache** (Redis) | `(backend, model, task, content_hash)` | Free repeats of LLM calls                                      | medium–long          |
| **Query cache** (Redis)        | normalized query                       | Fast dashboard aggregations                                    | short (secs–mins)    |
| **CDN**                        | URL                                    | Static **HTML/CSS/JS** dashboard assets, static report exports | long, versioned      |

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
- **DB scaling.** PostgreSQL read replicas (pgvector queries fan out to replicas);
  ClickHouse shards/replicas; partition the `analysis_results` vector index by
  tenant at large scale.

---

## 16. Cost estimation

> **Planning-grade order-of-magnitude estimates, not quotes.** Cloud GPU prices
> change frequently and vary by region, commitment, and provider. Treat the
> _ratios and the dominant cost drivers_ as the durable insight. **The LLM cost
> shape depends on the selected backend** (see §14.2): on the **`local`** backend
> there is no API line — cost is GPU + storage + networking, no per-token charge;
> on the **`groq`** backend the LLM becomes a **per-token API line** but the LLM
> GPU line disappears. Either way the hybrid design's whole point is to keep the
> LLM slice small — that caps both the GPU bill and the token bill. The headline
> number is **cost per 1,000 threads analyzed**, which the hybrid pipeline drives
> down by keeping the LLM to one cluster-level summary per thread.

### 16.1 What drives cost

| Driver          | Without hybrid (LLM-per-post)                 | With hybrid (this design)                                                                                                                                                                                 |
| --------------- | --------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| LLM tokens      | **Dominant, runaway** — every post = LLM call | Small — only the selective slice + cluster-level calls                                                                                                                                                    |
| LLM GPU (local) | Moderate                                      | Fixed GPU line (only on the `local` backend)                                                                                                                                                              |
| LLM API (groq)  | **Dominant, runaway**                         | Small per-token line (only on the `groq` backend)                                                                                                                                                         |
| NLP GPU compute | Moderate                                      | **The main fixed line**, cheap & predictable (batched small models)                                                                                                                                       |
| Vision compute  | n/a                                           | Small fixed line — cheap **image-sentiment** (SigLIP/CLIP) **+ our OCR** (PaddleOCR/Tesseract) on image posts; the **VLM** summary runs only on the selective slice (GPU on `local`, per-token on `groq`) |
| Agents + MCP    | n/a                                           | Tiny — stateless FastAPI services (a few CPU replicas); only real cost is **low-volume LLM-B calls** for reports/analyst Q&A, gated + budget-capped, billed under the LLM line (§14.6)                    |
| Storage         | Small                                         | Small                                                                                                                                                                                                     |
| Networking      | Small–moderate                                | Small–moderate (+ egress to Groq on the `groq` backend)                                                                                                                                                   |

The hybrid architecture keeps the LLM slice tiny, which caps cost under either
backend: on `local` it converts an unbounded per-token bill into a **bounded,
mostly-fixed GPU bill**; on `groq` it keeps the **per-token bill small and
proportional to the few calls that actually reach the LLM**, with no LLM GPU to
own. The operator can switch backends as volume, GPU availability, and data policy
change.

### 16.2 MVP — ~1,000 posts/batch

Single host, one 24 GB consumer GPU, Docker Compose. Self-hosted everything.

| Line                                                                 | Estimate (USD/mo)   | Notes                                                                           |
| -------------------------------------------------------------------- | ------------------- | ------------------------------------------------------------------------------- |
| Compute (1 GPU host, on-prem amortized **or** 1 cloud GPU part-time) | $150 – $700         | On-prem 4090 amortized at low end; cloud L4/A10 on-demand part-time at high end |
| Storage (Postgres + pgvector + ClickHouse + object, modest)          | $10 – $40           | Tens of GB (vectors live in Postgres)                                           |
| Networking                                                           | $5 – $30            | Mostly egress for dashboard/API                                                 |
| LLM — **`local`** (both roles time-sliced on the same GPU)           | ~$0 incremental     | LLM-A + LLM-B share the GPU; selective + cached                                 |
| LLM — **`groq`** alternative (per-token, ~1k batches)                | ~$5 – $50           | Replaces the LLM GPU; only the selective slice is billed; tiny at MVP volume    |
| Monitoring (self-hosted Prometheus/Grafana/Loki)                     | ~$0 – $20           | Runs on the same box                                                            |
| **Total**                                                            | **~$170 – $800/mo** | Dominated by the single GPU                                                     |

Two ways to run Stage-2 at MVP scale: **`local`** (default) time-slices the one GPU
across both roles (no API line; capacity grows by adding GPUs), or **`groq`** drops
the LLM off the GPU and calls Groq per token — at ~1k batches the selective slice
is tiny, so the token bill is a few dollars and you may not need a GPU at all if NLP
runs CPU-only (trade: prompt data leaves the box). Switchable at runtime, so you can
start on Groq and move to `local` as volume grows, or vice-versa.

### 16.3 Production — ~10,000 posts/batch

Small K8s cluster, 2–4 GPUs, KEDA autoscaling, spot for batch surges.

| Line                                                                | Estimate (USD/mo)       | Notes                                                      |
| ------------------------------------------------------------------- | ----------------------- | ---------------------------------------------------------- |
| Compute — NLP fleet (1–2 GPUs, autoscaled, partly spot)             | $400 – $1,500           | Scales with daily volume                                   |
| Compute — Stage-2 LLM, **`local`** (LLM-A L4/A10 + LLM-B L40S/A100) | $900 – $3,200           | GPU line; reserved/spot lower                              |
| — **or** Stage-2 LLM, **`groq`** (per-token, ~10k batches)          | $150 – $1,500           | Replaces the LLM GPU line; scales with the selective slice |
| Compute — CPU services (API, ingestion, assembler, DBs)             | $200 – $600             | Several small nodes                                        |
| Storage (Postgres + pgvector + ClickHouse + object, growing)        | $50 – $250              | Hundreds of GB → TB (vectors live in Postgres)            |
| Networking / egress                                                 | $50 – $300              | Dashboard, exports, inter-AZ (+Groq egress on `groq`)      |
| Monitoring                                                          | $30 – $150              | Self-hosted or small managed                               |
| **Total**                                                           | **~$1,600 – $6,000/mo** | LLM + NLP GPUs dominate on `local`                         |

### 16.4 Enterprise — ~100,000 posts/batch

Multi-node K8s, GPU node pools per stage, several A100/H100 or many L40S.

| Line                                                                   | Estimate (USD/mo)         | Notes                                                   |
| ---------------------------------------------------------------------- | ------------------------- | ------------------------------------------------------- |
| Compute — NLP fleet (several data-center GPUs, autoscaled, spot-heavy) | $3,000 – $12,000          | Horizontal scale; spot saves 50–70%                     |
| Compute — Stage-2 LLM `local` (A100/H100 or many L40S, vLLM)           | $5,000 – $25,000          | Biggest line on `local`; reserved/spot critical         |
| — **or** Stage-2 LLM `groq` (per-token, ~100k batches)                 | $1,500 – $15,000          | No LLM GPUs; cost tracks the selective slice + caching  |
| Compute — CPU services + DB nodes                                      | $1,000 – $4,000           | HA Postgres (with pgvector), ClickHouse cluster         |
| Storage (TBs across stores + object + backups)                         | $300 – $2,000             | Grows with retention                                    |
| Networking / egress                                                    | $300 – $2,000             | Significant at this scale                               |
| Monitoring / observability                                             | $150 – $600               |                                                         |
| **Total**                                                              | **~$10,000 – $48,000/mo** | LLM GPUs dominate; caching/clustering decide the spread |

At this scale the **single biggest lever is keeping the LLM slice small** — every
percentage point that doesn't need the LLM, and every cache hit, directly cuts the
largest line under **either** backend (it shrinks both the GPU line on `local` and
the token line on `groq`). A common enterprise pattern is **hybrid**: own LLM GPUs
for the steady base (`local`) and burst overflow to `groq` during spikes instead of
over-provisioning GPUs.

### 16.5 Levers ranked by impact

1. **Hybrid routing** — keep the LLM slice in the single digits %. Biggest lever.
2. **Caching + dedup** — social feeds are repetitive; cache hits are free results.
3. **Cluster-level LLM** — summarize clusters, not individual posts.
4. **Quantization + batching** — maximize GPU utilization (vLLM/Triton).
5. **Spot/reserved GPUs** — batch tolerates preemption (Kafka replay); reserve the
   steady base.
6. **Right-size the backend.** `local` (vLLM) — zero per-token pricing, no data
   egress, scale by adding GPUs; best for steady volume. `groq` — no LLM GPUs to own
   or reserve, pay only for the selective slice; best for bursty/low volume or
   no-GPU MVPs. Either way, right-size the roles: cheap LLM-A for per-post, larger
   LLM-B only for low-volume cluster/report work. The backend is switchable at
   runtime, so re-evaluate it as volume and GPU prices change.

---

## 17. API design

REST over HTTPS. Auth via `Authorization: Bearer <JWT>` or `X-API-Key: <key>`. All
endpoints versioned under `/v1`. Conventions: `202 Accepted` for async work;
idempotency via `Idempotency-Key`; pagination via `?limit=&cursor=`; consistent
error envelope.

### 17.1 Ingestion — pull from upstream (`POST /v1/ingest/sync`) + push (`POST /v1/posts/upload`)

The **primary** path is a **pull** of the upstream **post-with-details** payload
(post **with `comments[]` embedded**, plus `engagement`/`reactionBreakdown`/
`sampleShares`) into our own database — see [data_contract.md](data_contract.md).
`POST /v1/ingest/sync` triggers a pull for a selector; the service fetches, copies,
runs OCR, and analyzes (**post first, then its comments**), with no write-back. The
**`POST /v1/posts/upload`** path remains for external/replay sources and accepts the
**same field names**.

**Request — pull (`POST /v1/ingest/sync`):**

```json
{
  "source": "upstream",
  "selector": {
    "campaign_id": "cmoldmxzr02d8fu22vhvrg23c",
    "posted_from": "2026-05-01T00:00:00",
    "posted_to": "2026-05-31T00:00:00"
  },
  "options": {
    "tasks": ["all"],
    "want_summary": true,
    "summary_lang": "auto",
    "llm_backend": "auto"
  }
}
```

The service reads records keyed by CUID `id`, derives `platform` from each `url`
host, keeps upstream `sentiment`/`viralPotential` as `baseline_*`, **runs OCR on
`photoUrls`**, and recomputes richer sentiment for the post **and every embedded
comment** (the upstream ships none). Comments arrive as a stored sample
(`engagement.storedCommentRows` of `commentCount`) → analysis reports **coverage**.
Re-pulling the same `id` upserts.

**Request — push (`POST /v1/posts/upload`, inline batch, upstream field names):**

```json
{
  "source": "inline",
  "posts": [
    {
      "id": "cmosjpp9305n0u9tskgmd1c4k",
      "campaignId": "cmoldmxzr02d8fu22vhvrg23c",
      "platformPostId": "4460219584209360",
      "url": "https://www.facebook.com/4460219584209360",
      "caption": "শাপলা চত্বরের সেই রাতের কথা ...",
      "photoUrls": [
        "posts/cmoldmxzr02d8fu22vhvrg23c/4460219584209360/18f4cbb26803.jpg"
      ],
      "postType": "PHOTO_TEXT",
      "postedAt": "2026-05-04T18:19:14",
      "scrapedAt": "2026-05-05T17:39:44.464",
      "sentiment": -0.85,
      "viralPotential": 0.78,
      "engagement": {
        "commentCount": 1562,
        "totalReactions": 84979,
        "shareCount": 3189,
        "storedCommentRows": 112,
        "storedReactionRows": 0
      },
      "reactionBreakdown": {
        "SAD": 65289,
        "LIKE": 18235,
        "LOVE": 682,
        "ANGRY": 26
      },
      "comments": [
        {
          "id": "cmosktkag038n8jv53z8hx4ea",
          "parentId": null,
          "likes": 574,
          "replyCount": 14,
          "sentiment": null,
          "category": "NEUTRAL",
          "authorUsername": "Abdur Rahman Wisdom's",
          "text": "এই ছবিগুলো প্রমাণ করে যে পুলিশ আমাদের বন্ধু ছিল না কখনো।"
        }
      ]
    }
  ],
  "options": {
    "tasks": ["all"],
    "want_summary": true,
    "summary_lang": "auto",
    "llm_backend": "auto"
  }
}
```

`comments` carries the embedded thread (a stored sample); comment `sentiment` arrives
`null` and is **computed by us**.
`summary_lang: "auto"` keeps the summary in the post's detected language; pass
`"bn"`/`"en"` to force it. Banglish comments are handled natively. `platform` and
`baseline_*` are derived on ingest in both paths.

`llm_backend` selects the Stage-2 LLM provider for this request: `"local"`
(self-hosted vLLM), `"groq"` (Groq Cloud API), or `"auto"` (default — use the
server's configured backend and failover policy). A per-request override lets
callers pin sensitive data to `"local"` or send burst traffic to `"groq"` without
changing server config. **Tenant policy wins:** a tenant pinned to `local` for
data-residency cannot be overridden to `groq` by a request (the override is
rejected with `forbidden`). See §14.2.

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
    "model_profile": "default",
    "llm_backend": "auto"
  }
}
```

`selector` may instead be `{ "post_ids": [...] }` (upstream CUIDs),
`{ "campaign_id": "cmpe1djj…" }`, or
`{ "filter": { "platform": "telegram", "from": "...", "to": "..." } }`
(`platform` is the derived host value: `facebook` | `telegram` | `x` |
`instagram` | …).
`llm_backend` (`auto` | `local` | `groq`) overrides the Stage-2 provider for this
run — handy to reprocess a batch on a different backend (e.g. compare local vs Groq
output, or rerun on `groq` while LLM GPUs are down), subject to tenant policy.

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
      "post_id": "cmouf3g7p0dnae4hkfuk6spet",
      "campaign_id": "cmold8r5301u8fu22m7flh3pc",
      "platform": "facebook",
      "platform_post_id": "122161870454710684",
      "media_type": "TEXT",
      "language": "bn",
      "language_mix": ["bn", "banglish", "en"],
      "language_confidence": 0.96,
      "post_type": "opinion",
      "post_summary": "ভারতে মুসলিমদের পরিস্থিতি নিয়ে একটি ক্ষুব্ধ মতামত পোস্ট; মন্তব্যেও ক্ষোভ ও উদ্বেগ প্রবল।",
      "post_summary_lang": "bn",
      "overall_sentiment": "negative",
      "sentiment_score": -0.8,
      "text_sentiment": { "label": "negative", "score": -0.8 },
      "image_sentiment": null,
      "baseline_sentiment": -0.85,
      "baseline_viral_potential": 0.78,
      "emotion": "anger",
      "intents": ["express_grievance", "inform"],
      "topics": ["india", "muslims", "politics"],
      "entities": [
        { "type": "location", "value": "India", "confidence": 0.93 }
      ],
      "brand_mentions": [],
      "keywords": ["ভারত", "মুসলিম"],
      "toxicity_score": 0.34,
      "hate_speech_score": 0.21,
      "engagement": {
        "reactions": 26700,
        "comment_count": 6567,
        "share_count": 3136,
        "stored_comments": 607
      },
      "reaction_breakdown": {
        "SAD": 13047,
        "LIKE": 11219,
        "ANGRY": 1363,
        "HAHA": 948,
        "LOVE": 80,
        "WOW": 29,
        "CARE": 14
      },
      "comment_analysis": {
        "analyzed": 607,
        "coverage": "607/6567 stored",
        "sentiment_breakdown": {
          "positive": 41,
          "negative": 466,
          "neutral": 100
        },
        "themes": [
          "anger at India's treatment of Muslims",
          "calls for awareness",
          "links shared"
        ]
      },
      "post_summary_source": "llm",
      "confidence": 0.9,
      "processing": {
        "unit": "post+thread",
        "stage1_ms": 120,
        "llm_used": true,
        "llm_role": "LLM-A",
        "llm_backend": "local",
        "llm_model": "Qwen2.5-7B-Instruct"
      },
      "created_at": "2026-05-06T16:45:03"
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
      "sample_post_ids": ["cmq7orcjr2w78x80tufd0nza4"]
    }
  ],
  "metrics": { "total_posts": 10000, "languages": { "bn": 6200, "en": 3800 } },
  "generated_by": "insight_agent",
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
(pgvector retrieval + LLM) for citation-backed output.

### 17.5 Supporting endpoints

| Endpoint                           | Purpose                                                                                |
| ---------------------------------- | -------------------------------------------------------------------------------------- |
| `POST /v1/auth/token`              | Exchange credentials/API key for a JWT                                                 |
| `GET /v1/health` / `GET /v1/ready` | Liveness / readiness probes                                                            |
| `GET /v1/usage`                    | Per-tenant usage + cost metering (posts, LLM calls, by backend incl. Groq tokens/cost) |
| `GET /v1/search?q=&semantic=true`  | Semantic/keyword search over analyzed posts (pgvector + ClickHouse)                    |
| `DELETE /v1/posts/{id}`            | Data deletion (retention / GDPR-style)                                                 |

### 17.6 Error envelope

```json
{
  "error": {
    "code": "validation_error",
    "message": "post[1].caption exceeds max length",
    "request_id": "req_01J0...",
    "details": [{ "field": "posts[1].caption", "issue": "too_long" }]
  }
}
```

Standard codes: `unauthorized`, `forbidden` (incl. an `llm_backend` override that
violates tenant data-residency policy), `validation_error`, `rate_limited` (with
`Retry-After` — also surfaced when the `groq` backend returns HTTP 429),
`not_found`, `conflict` (idempotency), `internal`. The service handles a saturated
or failed backend internally (failover/degrade per §10) rather than surfacing a raw
upstream error where possible.

---

## 18. Worked examples

Real records from the upstream **post-with-details** payload (verbatim from
[posts_with_details.json](posts_with_details.json)) run through the smart layer.
Analysis is **multimodal and full-thread**: text `text_sentiment` + visual
`image_sentiment` (we also OCR the image), fused and cross-checked against
`reaction_breakdown`; `post_summary` grounded on caption + OCR + image; and **our**
per-comment sentiment over the **embedded** comments (the upstream ships `null`),
reported with **coverage**. Sentiment is recomputed with the upstream post score
kept as `baseline_sentiment`. Full versions (incl. a third, `null`-caption PHOTO
example) in [examples.md](examples.md).

### 18.1 Example 1 — Facebook, Bangla, photo+text (grief post, embedded comments)

Heavy Bangla photo+text post (Shapla Chattar commemoration); `reaction_breakdown` is
dominated by `SAD` (65,289), agreeing with the recomputed negative sentiment. 112 of
1,562 comments are embedded and scored by us.

**Output JSON (input is the §17.1 push example):**

```json
{
  "post_id": "cmosjpp9305n0u9tskgmd1c4k",
  "campaign_id": "cmoldmxzr02d8fu22vhvrg23c",
  "platform": "facebook",
  "platform_post_id": "4460219584209360",
  "media_type": "PHOTO_TEXT",
  "language": "bn",
  "post_type": "commemoration",
  "post_summary": "শাপলা চত্বরের ঘটনার স্মরণে একটি আবেগঘন বাংলা পোস্ট; ছবিতে সেই রাতের দৃশ্য। পোস্ট ও মন্তব্যে শোক এবং আওয়ামী লীগের প্রতি ক্ষোভ প্রবল।",
  "post_summary_lang": "bn",
  "post_summary_grounding": ["caption", "image"],
  "overall_sentiment": "negative",
  "sentiment_score": -0.82,
  "text_sentiment": { "label": "negative", "score": -0.85 },
  "image_sentiment": {
    "label": "negative",
    "score": -0.7,
    "per_image": [-0.7]
  },
  "baseline_sentiment": -0.85,
  "baseline_viral_potential": 0.78,
  "emotion": "sadness",
  "topics": ["shapla chattar", "2013", "politics", "grief"],
  "engagement": {
    "reactions": 84979,
    "comment_count": 1562,
    "share_count": 3189,
    "stored_comments": 112
  },
  "reaction_breakdown": {
    "SAD": 65289,
    "LIKE": 18235,
    "LOVE": 682,
    "HAHA": 566,
    "CARE": 125,
    "WOW": 56,
    "ANGRY": 26
  },
  "image_analysis": {
    "image_count": 1,
    "ocr_text": "",
    "description": "a dark night-time scene of a crowd / security forces",
    "vision_model": "SigLIP + Qwen2.5-VL-7B"
  },
  "comment_analysis": {
    "analyzed": 112,
    "coverage": "112/1562 stored",
    "sentiment_breakdown": { "positive": 6, "negative": 89, "neutral": 17 },
    "themes": [
      "grief and remembrance",
      "anger at Awami League",
      "calls for justice"
    ],
    "representative_comments": [
      {
        "author": "Abdur Rahman Wisdom's",
        "lang": "bn",
        "sentiment": "negative",
        "likes": 574,
        "text": "এই ছবিগুলো প্রমাণ করে যে পুলিশ আমাদের বন্ধু ছিল না কখনো।"
      }
    ]
  },
  "post_summary_source": "vlm",
  "confidence": 0.9,
  "processing": {
    "unit": "post+thread",
    "stage1_ms": 95,
    "llm_used": true,
    "llm_role": "LLM-A",
    "llm_backend": "local",
    "vision_used": true,
    "vision_model": "Qwen2.5-VL-7B-Instruct"
  },
  "created_at": "2026-05-04T18:19:14",
  "scraped_at": "2026-05-05T17:39:44.464"
}
```

**What did the work:** text sentiment on the caption (`-0.85`) + a visual model and
**our OCR** on the photo (`image_sentiment -0.7`) fused to `-0.82`, **agreeing with
the `SAD`-dominated `reaction_breakdown`**. The **112 embedded comments** were each
scored by us (the upstream shipped `sentiment: null`) → an 89/17/6 breakdown with
**coverage `112/1562`**. A VLM produced the Bangla summary grounded on caption + image.

### 18.2 Example 2 — Facebook, Bangla, text-only (embedded comments)

Text-only opinion post (no image → `image_sentiment: null`); 607 of 6,567 comments
embedded and scored. See §17.3 for the full result object
(`post_id: cmouf3g7p0dnae4hkfuk6spet`): recomputed `-0.8` (baseline `-0.85`),
`reaction_breakdown` `SAD`+`ANGRY`-heavy, `comment_analysis` 466/100/41 with coverage
`607/6567`.

### 18.3 How these map to the owner's request

The owner asked for `{ post_summary, sentiment_analysis, "and something like
that" }`, over the **real** post-with-details records. The schema delivers:

- **`post_summary`** — in the original language (`post_summary_lang`), **grounded on
  caption + OCR + image** (`post_summary_grounding`), so a `null`-caption photo post
  is still summarized from its picture.
- **`sentiment_analysis`** — **multimodal and full-thread**: `text_sentiment`,
  `image_sentiment`, the fused `overall_sentiment`/`sentiment_score` (cross-checked
  against `reaction_breakdown`), and **our** per-comment sentiment in
  `comment_analysis.sentiment_breakdown` with **coverage**. The upstream post score
  is kept as `baseline_sentiment`. **Order: post text → image → fuse → summary →
  comments.**
- **"something like that"** — `post_type`, `media_type`, `image_analysis`,
  `reaction_breakdown`, `shares`, `intents`, `topics`, `comment_analysis.themes`,
  toxicity, engagement (with comment **coverage**), and `campaign_id`/`platform`
  provenance — rich structured signal.

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
  worker-nlp     (Python)           → Stage-1 text suite + vision (image sentiment, OCR, GPU)
  worker-llm     (Python)           → Stage-2 worker (text LLM + VLM); LLM_BACKEND=local|groq
  vllm           (vLLM, optional)   → local backend only: LLM-A (+LLM-B) + VLM on GPU
  agent-orch     (FastAPI)          → AI agents (insight/deep-dive/alerting) → LLM-B + MCP [Phase 2]
  mcp-servers    (FastAPI + MCP)    → analytics-mcp · retrieval-mcp · ingest-mcp (internal) [Phase 2]
  redis          (cache/queue)      → Redis Streams = bus + cache + dedup
  postgres       (ops + jobs + vectors) → pgvector/pgvector:pg16 image; analysis_results.embedding vector(768)
  clickhouse     (analytics)
  minio          (object storage)
  prometheus + grafana + loki       → monitoring
```

- Queue = **Redis Streams** (no separate Kafka yet).
- API services can be one process for the MVP; split later.
- **Stage-2 backend is a config switch** on `worker-llm`:
  - `LLM_BACKEND=local` → it talks to the `vllm` service; GPU is shared between NLP
    and the local LLM(s) via time-slicing (run LLM-A only at MVP scale).
  - `LLM_BACKEND=groq` → drop the `vllm` service entirely and set `GROQ_API_KEY` +
    role→model IDs; the worker calls Groq over HTTPS and needs **no GPU**. Flip the
    env var to switch at any time (no image rebuild).
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
   ┌──────▼───────┐      ┌──────▼────────────────────┐
   │ triton (GPU) │      │ Stage-2 backend:          │   LLM_BACKEND switch:
   └──────────────┘      │  vllm A+B (GPU)  ⇄  Groq  │   local (in-cluster GPU)
                         │                    (API)  │   or groq (egress HTTPS)
                         └───────────────────────────┘
          │ writes (assembler Deployment) │
   ┌──────▼────────────────┬──────────┬──────────────┬───────────┐
   │ postgres + pgvector    │clickhouse│              │ minio/S3  │  StatefulSets / managed
   │ (HA, replica; vectors) │(cluster) │              │           │
   └────────────────────────┴──────────┴──────────────┴───────────┘
        observability namespace: prometheus, grafana, loki, jaeger, otel-collector
```

### 19.4 Kubernetes deployment plan

**Namespaces:** `platform` (services + workers), `data` (DBs, queue), `models`
(Triton, vLLM), `observability` (monitoring), `ingress`.

**Workload mapping:**

- **Stateless services** (auth, ingestion, reporting, user-mgmt, assembler, router,
  **agent-orchestrator**, **MCP servers**) → `Deployment` + `Service`, HPA on
  CPU/RPS, `PodDisruptionBudget`, liveness/readiness probes. Agent-orchestrator + MCP
  servers are **CPU-only** (they query stores + call the LLM backend); MCP servers
  are `ClusterIP`-only (internal).
- **Workers** (nlp, llm) → `Deployment` on **GPU node pools** (nodeSelector +
  tolerations + `nvidia.com/gpu` resource requests), scaled by **KEDA** on Kafka
  consumer lag.
- **Model servers** → Triton (NLP) always on the GPU pool. The **Stage-2 LLM
  backend is selected per environment** via `LLM_BACKEND`:
  - `local`: two vLLM `Deployment`s (LLM-A, LLM-B) on the GPU pool, a `Service` each
    for in-cluster HTTP; no egress.
  - `groq`: no vLLM deployments — the Stage-2 worker (a stateless `Deployment`,
    HPA/KEDA on queue depth, **no GPU**) calls Groq over HTTPS. Allow egress to Groq
    in `NetworkPolicy`/egress rules and mount `GROQ_API_KEY` from a Secret.
    Both are valid simultaneously for **hybrid/failover**; the worker picks per
    request/policy. Switching backends is a config rollout, not a rebuild.
- **Stateful infra** (Kafka, PostgreSQL with pgvector, ClickHouse) → operators or
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
data; model servers reachable only by workers). On the `groq` backend, allow
**egress from the Stage-2 worker to the Groq API only** (default-deny egress
elsewhere); on `local` the workers need no internet egress at all. Optional
**Linkerd** mesh for mTLS + retries + canary. Secrets via Kubernetes Secrets +
Vault/External Secrets — including the **`GROQ_API_KEY`**, mounted only into the
Stage-2 worker when the `groq` backend is enabled. Private node pools for `data`
and `models`; only ingress is internet-facing.

**Reliability:** Multi-AZ node pools; PodDisruptionBudgets; replicas ≥ 2 for
stateless. DLQ topic in Kafka; alert on DLQ growth. Rolling updates with readiness
gates; canary via mesh or two Deployments. Backups: Postgres PITR (covers the
pgvector embeddings) + ClickHouse snapshots to object storage.

**CI/CD:** Build → scan images → push to registry → deploy via Helm/Kustomize (+
ArgoCD for GitOps). Model artifacts versioned in object storage; `model_versions`
recorded in every result for reproducible replay.

---

## 20. Implementation plan & roadmap

Phased build from MVP (1k) → Production (10k) → Enterprise (100k).

### Phase 0 — Foundations (week 0–1)

- Repo + monorepo layout (services, workers, infra, models, dashboard).
- Lock the **input contract** ([data_contract.md](data_contract.md)): the upstream
  **post-with-details** schema (real sample
  [posts_with_details.json](posts_with_details.json)) — post with embedded
  `comments[]`, `engagement`, `reactionBreakdown`, `sampleShares` — the pull +
  own-DB integration (no write-back), platform-from-URL, and recompute-with-baseline
  sentiment. Lock the **canonical output JSON schema** (§8) and a shared JSON Schema
  validator. These contracts are the heart of the microservice; lock them early.
- Build the **upstream API client + ingestion**: pull the post-with-details payload
  keyed by CUID `id` (comments embedded), derive `platform`, keep upstream
  `sentiment`/`viralPotential` as `baseline_*`, **run OCR on `photoUrls`**, record
  comment **coverage**, upsert into **our own database**.
- Stand up local Docker Compose skeleton: Postgres (`pgvector/pgvector:pg16`, with
  the `vector` extension enabled), Redis, ClickHouse,
  MinIO, a stub API, Prometheus/Grafana.
- Pick and pin model versions (§14); download local weights to object storage.
  Define the **Stage-2 LLM backend interface** (OpenAI-compatible) and wire both
  providers behind it — `local` (vLLM) and `groq` — selected by `LLM_BACKEND`
  config with a per-request override and per-tenant policy. Pin the Groq
  role→model IDs and store `GROQ_API_KEY` as a secret.
- Build a small **labeled eval set** per task and per language (bn / en / Banglish).

**Exit criterion:** a single post flows API → Redis Stream → stub worker → Postgres
→ `GET /analysis/{id}`, with valid JSON.

### Phase 1 — MVP (week 1–4) — 1,000 posts/batch

Goal: prove the hybrid pipeline and output quality end-to-end, cheaply.

> **First target (priority order)** — the multimodal post-and-thread pipeline,
> shippable on today's data (comments are **embedded**): **(1) post text sentiment →
> (2) image sentiment (visual) + our OCR → (3) fuse (cross-check `reactionBreakdown`)
> → (4) post summary grounded on caption + image/OCR → (5) per-comment sentiment over
> the embedded thread** ([data_contract.md](data_contract.md) §4).

- **Ingestion service:** **pull the post-with-details payload** (comments embedded),
  derive `platform` from URL host, keep upstream `sentiment`/`viralPotential` as
  `baseline_*`, **run OCR on `photoUrls`** (no OCR is shipped), normalize the caption
  and OCR text, take the embedded comment thread (record **coverage**
  `storedCommentRows`/`commentCount`), content-hash **dedup** (Redis), upsert into
  **our own DB** keyed by CUID `id`, enqueue to Redis Streams. `/v1/posts/upload`
  wired for replay/external.
- **Stage-1 NLP + vision worker:** runs **post first, then each comment**. _Text:_
  language/Banglish detection (fastText) → shared XLM-R encoder with
  sentiment/emotion/topic/intent heads (**recomputed** `text_sentiment`, upstream
  score kept as baseline) → toxicity/hate → NER (GLiNER/spaCy) → embedding (bge-m3)
  → keywords. _Vision (image posts):_ SigLIP/CLIP `image_sentiment`, **our OCR**
  (PaddleOCR/Tesseract), a small VLM image description. _Fuse_ → post
  `overall_sentiment` (cross-check `reactionBreakdown`). Run the text models over
  **each embedded comment** (upstream ships none) → `sentiment_breakdown` with
  **coverage**. Micro-batched; confidence per field.
- **Router/Triage:** confidence gates + task flags; decide LLM/VLM routing.
- **Stage-2 LLM/VLM worker:** a backend-agnostic worker for selective
  summarization/insight, running either backend — `local` (vLLM serving **LLM-A**
  quantized 7B **plus a VLM** `Qwen2.5-VL` for image-grounded summaries) or `groq`
  (fast text model + a vision model). The **`post_summary` is grounded on caption +
  OCR + image** for photo posts. Ship both adapters in the MVP so the switch is
  exercised early; **LLM response cache** in Redis keyed by
  `(backend, model, task, content_hash)`. (LLM-B is added in Phase 2 for
  cluster/report quality.)
- **Result assembler:** merge + JSON-schema validate + write to the 3 backends —
  Postgres + pgvector (canonical row + `analysis_results.embedding`), ClickHouse,
  MinIO.
- **APIs:** `/posts/upload`, `/analysis/run`, `/analysis/{id}`, `/reports` (basic),
  auth (API key + JWT).
- **Web dashboard (v1) — plain HTML/CSS/JS** (vanilla, no framework): job status,
  results table, per-post sentiment + comment breakdown + reaction chart, static
  files calling the read APIs.
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
  the **LLM-B** role — on `local`, a quantized 14B/32B on a data-center GPU
  (L40S/A100) via vLLM alongside LLM-A; on `groq`, just a larger model ID.
- **Reliability:** retries + backoff everywhere, idempotent assembler, circuit
  breakers around each LLM backend; if a backend is saturated, degrade LLM-B→LLM-A,
  **fail over local↔Groq** (where policy allows), or fall back to Stage-1-only;
  PDBs, multi-replica.
- **Cluster summarization:** k-means/HDBSCAN over embeddings → LLM summarizes
  clusters, not posts (key cost lever at 10k).
- **Reporting:** trend analysis, brand-mention tracking, political analysis on
  ClickHouse; grounded report generation via RAG (pgvector + LLM).
- **Agentic insight layer (§14.6):** stand up the **MCP servers**
  (analytics/retrieval/ingest) + **agent orchestrator**; ship the **Insight/Analyst
  agent** (`POST /v1/agents/query` + agent-generated reports) on LLM-B over those
  tools, then **coverage deep-dive** and **alerting** agents — gated, cached,
  budget-capped, tenant-policy-bound; corpus-tier only.
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
- **Data layer scale-out:** Postgres read replicas (pgvector queries served from
  replicas); ClickHouse cluster (shards+replicas); partition the `analysis_results`
  vector index by tenant; possibly migrate to Milvus if vectors reach
  billions (§13.5).
- **LLM scale:** on `local`, multiple vLLM replicas for both models (tensor
  parallelism for LLM-B), absorbing spikes by adding GPU replicas; on `groq`, no GPU
  scaling — negotiate quota for peak. **Hybrid** is the typical enterprise pattern:
  own GPUs for the steady base, burst overflow to Groq during spikes.
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

| Risk                                    | Mitigation                                                                                                    |
| --------------------------------------- | ------------------------------------------------------------------------------------------------------------- |
| Bangla / Banglish accuracy below bar    | Multilingual encoder + Bangla fine-tune + Banglish-heavy eval set                                             |
| LLM slice creeps up → cost spikes       | Confidence-gate tuning, caching, clustering, alert on LLM-share metric                                        |
| GPU cost overrun                        | Spot for batch, reserved base, quantization, right-sizing                                                     |
| Queue/worker overload                   | KEDA on lag, bounded queues, backpressure, DLQ                                                                |
| Data privacy / PII                      | Encryption at rest/in transit, retention/deletion APIs, access audit                                          |
| Prompt injection via post text          | Treat post text as untrusted; never let it alter system instructions                                          |
| Model regression on upgrade             | Eval-set gate before ship; Kafka replay to compare                                                            |
| Groq backend leaks PII (data egress)    | Default `local`; pin sensitive tenants to `local`; reject policy-violating overrides; audit `llm_backend`     |
| Groq outage / rate-limit / price change | Failover to `local` (or degrade to Stage-1-only); retry on 429; alert on Groq error/cost; cap per-call tokens |

```

```

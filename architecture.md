# Architecture — Social Media "Smart Layer" Microservice

This is the **recommended** architecture. For the alternatives that were weighed
to arrive here, see [possible_architecture.md](possible_architecture.md). For
model choices see [models.md](models.md), for cost see
[cost_estimation.md](cost_estimation.md), for real input→output examples see
[examples.md](examples.md).

## 0. What this service is

An **existing social-media monitoring platform** scrapes 1,000+ real-time
Bangla/English/Banglish posts (Facebook in the current sample; others by URL host)
**with their comments embedded**, and exposes them as a single **post-with-details**
payload (post + `comments[]` + `engagement` + `reactionBreakdown` + `sampleShares`).
This service is a **smart, self-contained microservice** — a "thinking layer" —
that sits on top of that platform and:

- **pulls** the post-with-details payload into its **own separate database** —
  read-only consumer, no write-back (full input contract:
  [data_contract.md](data_contract.md)). The **comment thread is embedded**, so no
  separate fetch/join is needed.
- decides _per item_ how much intelligence each one needs (the cheap NLP models
  vs. an LLM — this routing is the "smart" part), and
- returns one **structured JSON** object per thread (summary in the post's own
  language, sentiment, topics, intents, entities, brand mentions, comment
  analysis, and optional per-entity **target stance** — see §6) that downstream
  projects consume directly.

The upstream stores a coarse post `sentiment` and `viralPotential`; we keep those
as a **baseline** and **recompute** our own richer sentiment. **Comment sentiment is
empty upstream — we compute it.** **OCR is no longer shipped — we run it ourselves**
on `photoUrls`. The free **`reactionBreakdown`** (LIKE/LOVE/HAHA/WOW/SAD/ANGRY/CARE)
is a crowd emotion prior we cross-check against ([data_contract.md](data_contract.md) §4).
Platform is **derived from each post's URL host**, so the service stays
platform-agnostic.

**The pipeline order** (see [data_contract.md](data_contract.md) §4):
(1) **text sentiment** on the caption, (2) **image sentiment** from a _visual_
model on the photo when one is present (we also OCR the image), (3) **fuse** the
signals into the post's overall sentiment, (4) a **post summary grounded on
caption (+ image/OCR when available)**, then (5) **per-comment sentiment** over
the embedded comments → thread breakdown + themes.

> **Step 2 is implemented but unexercised (4 Aug 2026).** The corpus's 69
> `photoUrls` are relative object-storage keys and the objects are not in MinIO,
> so no image bytes are reachable in any runnable configuration and the image
> term has never contributed a non-zero value
> ([PROJECT_ASSESSMENT.md](PROJECT_ASSESSMENT.md) §5.2). Consequences now
> visible in the output rather than hidden:
>
> - a failed fetch reports `image_analysis.vision_status` (`fetch_failed`,
>   `model_unavailable`, `stub`) instead of a fabricated `neutral` verdict —
>   previously a real-mode run claimed `vision_model: "SigLIP"` for an image
>   SigLIP never saw;
> - **fusion renormalises over the terms that carry a real verdict**, so an
>   absent image term no longer consumes its 0.4 weight and shrink a genuine text
>   signal 40% toward neutral;
> - OCR is off by default (`STAGE1_OCR_SENTIMENT=false`) and the working corpus
>   the working corpus is `posts_with_details.json` (all 50 posts) — the 7
>   null-caption posts stay in, because the missing image bytes (not a filtered
>   corpus) are what keep the OCR branch cold, and their 1,307 comments analyse
>   normally.
>
> Post sentiment is therefore a **text** measurement today. Step 5 is the claim
> that carries the most weight, and it is real: every non-emoji comment is
> labelled.

Hard product constraints from the owner: **fast**, **cost-effective**,
**efficient**, and **accurate on Bangla and Banglish** (with fine-tuning hooks
for when accuracy must improve). It must **scale** horizontally to handle batches
of 1k → 10k → 100k threads.

The Stage-2 LLM is a **pluggable backend with two interchangeable providers —
`local` (self-hosted vLLM) and `groq` (Groq Cloud API) — switchable at runtime**
(see [models.md](models.md) §2). The default backend is **local** (no per-token
bill, no data egress); **Groq** is an opt-in switch for fastest inference and
zero GPU ops. The `local` backend is any OpenAI-compatible server: vLLM in
production, **Ollama** for local dev (the runbooks use Ollama with
`qwen2.5:7b` / `qwen3-vl:4b` — see [run.md](run.md) §3). The choice is config- and request-level, so the operator can flip
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
4. **Storage by access pattern.** Use the right store per job — OLTP and vector
   search co-locate in Postgres (the `pgvector` extension), while heavy
   aggregation lives in a columnar store.
5. **Idempotent + observable.** Every post has a stable hash; reprocessing is
   safe. Every stage emits metrics, logs, and traces.

---

## 2. High-level architecture

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
     │ (JWT, API keys) │        │ (validate, dedup, │       │ + Agent Orchestr.│  FastAPI
     │   (FastAPI)     │        │  enqueue) FastAPI │       │ (read APIs,      │
     └─────────────────┘        └─────────┬─────────┘       │  agent runs)     │
                                          │ produce         └────────┬─────────┘
                                          │                          │ read / agent tools
                                ┌─────────▼─────────┐       ┌─────────▼─────────────────┐
                                │   Message Bus     │       │  Agentic insight layer    │
                                │ Kafka / Redis Str │       │  AI agents (LLM-B/VLM) ──▶ │
                                │  (partitioned)    │       │  MCP servers:             │
                                └─────────┬─────────┘       │  analytics · retrieval ·  │
                                          │ consume         │  ingest  (see §11)        │
                                          │                 └─────────┬─────────────────┘
                                          │                          │ read
                                ┌─────────▼─────────┐                │
                                │   Message Bus     │                │
                                │ Kafka / Redis Str │                │
                                │  (partitioned)    │                │
                                └─────────┬─────────┘                │
                                          │ consume                  │


        │  STAGE 1 — Heavy LLM worker (GPU/Ollama), horizontally scaled      │
        │  lang detect · NER · summarization (gemma) · text features         │
        └─────────────────────────────────┬──────────────────────────────────┘
                                          │ writes features + summary
                                ┌─────────▼─────────────┐
                                │ Router / Triage       │  post-level gate +
                                │ (confidence gate,     │  comment selection
                                │  comment selection)   │  (all with text, or top-N)
                                └─────┬─────────────────┘
                                      │
                              ┌───────▼───────────────────────────┐
                              │ STAGE 2 — Parallel Execution      │
                              │ Lane A: post summary / type /      │
                              │         insight (gated)            │
                              │ Lane B: comment ensemble over the  │
                              │   router's set — 7 cheap heads +   │
                              │   LLM stance pass + dedup cache    │
                              └───────┬───────────────────────────┘
                              └─────────────┤
                                ┌───────────▼───────────┐
                                │  Result Assembler      │  builds final JSON
                                │  + JSON schema validate│
                                └───────────┬───────────┘
                                            │ fan-out writes
            ┌───────────────────┬───────────┼───────────────────┬───────────────┐
       ┌────▼─────┐       ┌──────▼─────┐     ┌──────▼──────┐  ┌─────▼─────┐
       │PostgreSQL│       │ ClickHouse │     │   Redis     │  │  Object   │
       │ ops+jobs │       │ analytics  │     │ cache/dedup │  │  storage  │
       │+pgvector │       └────────────┘     └─────────────┘  └───────────┘
       │ vectors  │
       └──────────┘
```

The **Router/Triage** between Stage 1 and Stage 2 is the heart of the cost
strategy — see §5. All backend services are **Python + FastAPI**; the web dashboard
is **React 19 + Vite + Tailwind** (`dashboard/`; the vanilla HTML/CSS/JS build
this document originally specified is preserved at `dashboard_legacy/`). Above
the per-post pipeline sits a selective **agentic
insight layer** — AI agents that reach data through **MCP servers** for analyst
Q&A, grounded reports, and targeted deep-dives (never per post) — see §11.

---

## 3. Data flow (end to end)

1. **Ingest (pull).** The Ingestion Service **pulls the post-with-details payload**
   (a campaign / time window / id) — post **with its `comments[]` embedded**, plus
   `engagement`, `reactionBreakdown`, and `sampleShares` (see
   [data_contract.md](data_contract.md)). It copies the records into **our own
   database** (no write-back upstream), derives `platform` from the URL host,
   keeps the upstream `sentiment`/`viralPotential` as `baseline_*`, **runs OCR on
   `photoUrls`** (the payload no longer ships OCR text), normalizes text (Unicode
   NFC, emoji handling, Bangla/English/Banglish script tagging) over the
   **caption + OCR text**, takes the embedded comment thread as-is (it tracks
   `storedCommentRows` of `commentCount` — record coverage), and computes a content
   hash over the post + comments. A pushed batch (`POST /v1/posts/upload`) is also
   accepted for external/replay sources, but the upstream pull is the primary path.
2. **Dedup gate.** Hash is checked against Redis (recent) and Postgres
   (historical). Exact duplicates short-circuit to the cached result; near
   duplicates (cosine > threshold on the `analysis_results.embedding` vector)
   can reuse prior analysis. This
   alone removes a large fraction of LLM/NLP work on real social feeds.
3. **Enqueue.** A `job` row is created in PostgreSQL (`status=queued`). One
   message per post is produced to the bus, partitioned by `post_id` hash so a
   post's ordering is stable and load spreads evenly.
4. **Stage 1 — Fast NLP + vision (parallel).** Worker pulls a batch
   (micro-batching) and runs the small-model suite. **Multimodal, post first, then
   comments** (see [data_contract.md](data_contract.md) §4):
   - **Text path** — the **caption + our OCR text** gets language/Banglish
     detection → **our recomputed** `text_sentiment`, emotion, topic, intent,
     toxicity/hate, NER, keyword extraction, and a sentence embedding.
   - **Vision path (image posts)** — a cheap **visual** model scores
     `image_sentiment` per image (language-agnostic), and we **run OCR** + a short
     image description for grounding ([models.md](models.md) §1). Skipped for
     text-only posts.
   - **Fuse** `text_sentiment` + `image_sentiment` → post-level
     `overall_sentiment`/`sentiment_score` (text-weighted when a caption exists;
     image + OCR-weighted for `null`-caption photo posts), **cross-checked against
     `reactionBreakdown`** (SAD/ANGRY-dominant ⇒ expect negative). Upstream
     `sentiment`/`viralPotential` are retained alongside as `baseline_*`.
     The **same text models** then run over **each embedded comment** (the upstream
     gives no comment sentiment), aggregated into the thread's `sentiment_breakdown`
     and themes (weighted by `likes`), reporting **coverage** (`analyzed` of
     `commentCount`). (For a rare comment-less post, the post pass
     runs standalone and `comment_analysis.analyzed = 0`.) Each output carries a
     **confidence** score. Results are written to a partial-result store.

   > **Implementation note — Stage-1 NLP engine (`STAGE1_LLM`).** The Stage-1
   > field set, fusion, comment coverage, and confidence above are the contract;
   > _how_ they're computed is pluggable. The default deployment realizes the
   > "small-model suite" with a **single fast LLM** — the `stage1` role
   > (`gemma3:4b` on local Ollama by default) — which emits the whole NLP field set
   > (sentiment/emotion/topic/intent/toxicity/NER/keywords) in one structured-JSON
   > call per caption and per batched comment group. **Stage 2 runs on a different,
   > larger model** (the `stage2` role — `qwen2.5:7b`). The embedding still comes
   > from the SentenceTransformer (or the shared stub), since a chat LLM can't emit
   > a 768-dim vector. Set `STAGE1_LLM=false` to fall back to the specialized
   > small-model classifiers (XLM-R/fastText/GLiNER/…) of [models.md](models.md) §1;
   > any LLM failure also degrades to the deterministic stub, so offline/CI stays
   > green. See [models.md](models.md) §2 for the role-to-model mapping.
5. **Router/Triage (the "smart thinking layer").** For each thread the router
   decides:
   - All required fields produced with confidence ≥ threshold, and no LLM-only
     task requested → **mark complete**, skip Stage 2.
   - A `post_summary` is requested, or confidence is below threshold, or the
     thread is ambiguous / heavily Banglish / long → **route to Stage 2** with a
     compact, token-minimized prompt (post + a representative/clustered subset of
     comments, truncated). This selectivity is what keeps the service fast and
     cheap.
   - **Which comments Stage 2 analyses**, independently of the post-level gate:
     by default **every comment with text** (`ROUTER_COMMENT_TOP_N=0`), or the N
     most-reacted ones when a positive cap is set. The selection is made once,
     here, and marked on the comments (`stage2_selected`), so every Stage-2 voter
     reads the same set —
     the seven cheap classifier heads, the near-duplicate cache and the LLM
     stance pass. A cap living inside the stance pass instead (the older
     `COMMENT_STANCE_MAX_PER_POST`) bought the tokens back but still ran seven
     models over the whole thread, and left the LLM column of the per-comment
     comparison empty on comments every cheap head had voted on. Comments below
     the cut are kept, persisted and reported (`ensemble.not_analysed`) — but with
     no model verdict, so they read `uncertain` at zero voters rather than
     borrowing Stage 1's keyword label. Nothing is dropped from the payload.
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
   **Lane B — the comment ensemble (the other half of the same worker).** Not
   gated by the confidence rules: it runs for every post, over the router's
   selected comments, and is where most of the pipeline's per-comment signal comes
   from. **Eight labellers per comment**, combined by `libs/ensemble.combine()`:
   **seven small sentiment heads** batched on CPU (`xlmr`, `distilbert`,
   `twitter_xlmr`, `banglabert`, `bengali_sentiment_bert`, `mbert`, `modernbert` —
   checkpoints in `STAGE2_CLASSIFIER_1..7`), and the **LLM stance pass**, the only
   labeller that sees the post and therefore judges stance *toward* it. Each
   verdict is kept separately in `parallel_labels`, so the dashboard can show all
   eight side by side on one comment. **Only a model may label a comment:** Stage
   1's emoji + keyword rule does not vote (removed 17 Aug 2026), because it answers
   on every comment — mostly with the deterministic hash stub — and a voter that
   can never abstain makes the agreement figures unfalsifiable. What the ensemble is **not** is nine independent
   readings — five of the seven heads are multilingual encoders trained on
   overlapping data, so a 7-0 vote is weaker evidence than seven unrelated models
   would be. A voter that fails to load or returns an off-taxonomy label
   **abstains**; it is never counted as a neutral vote, and `label_voters` reports
   how many actually spoke. Zero voters is reported as `uncertain`, not neutral.
   `libs/comment_groups.py` labels one representative per exact-duplicate group
   and propagates the verdict, which is a cache rather than an approximation.
7. **Assemble + validate.** The Result Assembler merges Stage 1 + Stage 2 into
   the canonical JSON (see §6), validates against the JSON Schema, and sets
   `confidence` = aggregate.
8. **Persist (fan-out to 3 backends).**
   - PostgreSQL (+ pgvector): job status, per-post status, the canonical result
     row, **and** the `analysis_results.embedding` `vector(768)` column for
     semantic search, clustering, and dedup — an idempotent upsert keyed by
     `post_id`.
   - ClickHouse: a denormalized analytics row for fast aggregations/trends.
   - Object storage: raw payload + any generated reports.
9. **Serve.** `GET /analysis/{id}`, `GET /reports`, and dashboard queries read
   from PostgreSQL (point lookups) and ClickHouse (aggregations).

---

## 4. Service breakdown (backend)

| Service                          | Responsibility                                                                                                                                                                             | Stack                                                                                      | Scaling unit        |
| -------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------ | ------------------- |
| **API Gateway**                  | TLS termination, routing, rate limiting, request size limits, CORS                                                                                                                         | NGINX / K8s Ingress (+ optional Kong)                                                      | replicas behind LB  |
| **Auth Service**                 | API keys, JWT issue/verify, RBAC, per-tenant quotas                                                                                                                                        | FastAPI + PostgreSQL + Redis                                                               | stateless replicas  |
| **Ingestion Service**            | Validate, normalize, dedup, create job, enqueue                                                                                                                                            | FastAPI (async)                                                                            | stateless replicas  |
| **Stage-1 NLP + Vision Workers** | Run small-model suite (text) **and the visual model on image posts** (`image_sentiment` + image description), micro-batched, emit features + confidence                                    | Python (Ray/Celery consumer) + Triton/ONNX/CTranslate2; SigLIP/CLIP + light VLM for vision | GPU/CPU worker pool |
| **Router/Triage**                | Apply confidence gates + task flags; decide LLM/VLM routing; **select the top-N comments by reaction count** that every Stage-2 voter will read                                            | lightweight Python service or in-worker rule module                                        | stateless           |
| **Stage-2 LLM/VLM Workers**      | Two lanes, run concurrently: **Lane A** selective summarization (text **and image-grounded via a VLM**) / post type / insight, gated; **Lane B** the per-comment ensemble — 7 batched sentiment heads + the context-aware LLM stance pass + dedup propagation, ungated | Thin worker → local vLLM (LLM-A+LLM-B + a VLM) **or** Groq API (text + vision model); the 7 heads are `transformers` pipelines on CPU (`STAGE2_CLASSIFIER_DEVICE`) | GPU pool, stateless |
| **Result Assembler**             | Merge, JSON-schema validate, compute aggregate confidence                                                                                                                                  | Python consumer                                                                            | stateless replicas  |
| **Reporting/Query Service**      | Read APIs, report generation, exports                                                                                                                                                      | FastAPI + ClickHouse + PostgreSQL                                                          | stateless replicas  |
| **Agent Orchestrator**           | Runs the **selective AI agents** — nine of them (§11) — corpus/report tier only, never per-post                                                           | FastAPI + agent loop → the dedicated `agent` LLM role + MCP tools                             | stateless replicas  |
| **MCP Servers**                  | Standardized tool/resource interfaces the agents call: `analytics-mcp` (ClickHouse/Postgres), `retrieval-mcp` (Postgres + pgvector + fetch), `ingest-mcp` (trigger upstream pull / fetch more comments) | FastAPI + MCP SDK (stdio/HTTP)                                                             | stateless replicas  |
| **User Management**              | Tenants, users, roles, billing/usage metering                                                                                                                                              | FastAPI + PostgreSQL                                                                       | stateless replicas  |

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
   84% of posts (measured)     16% (measured)                  (summary/insight/report)
```

> **Measured, 4 August 2026** ([PROJECT_ASSESSMENT.md](PROJECT_ASSESSMENT.md)
> §4.6): **16%** of posts reach Stage 2 on the shipped configuration
> (`STAGE1_LLM=true`, `gemma3:4b`), **74%** under the keyword stub. The "~90–95%
> bypass" above was a design target, never a measurement; the gate in fact spent
> a period routing **100%** of posts because of two field-name bugs (§4).
>
> Read the rate as **a measure of Stage-1 quality**: a Stage 1 that types a post
> confidently bypasses Stage 2, so the rate *falls as Stage 1 improves*. Same
> code, two Stage-1 engines, two rates.
>
> **The gate is only half the cost story.** 85–96% of LLM calls are
> comment-level, and comment labelling is not gated by it (Stage 1 labels every
> comment; Lane B runs for every post). The bound on comment volume is
> `ROUTER_COMMENT_TOP_N` — see the next list — and quoting the routing rate alone
> invites the wrong question. [cost_estimation.md](cost_estimation.md) §5.

Levers that keep token usage and cost low:

- **Confidence gating.** Only uncertain posts reach the LLM. Tune thresholds per
  task from a labeled validation set.
- **Top-N comment selection (the biggest per-post lever).** The router hands
  Stage 2 only the N most-reacted comments with text, bounding Stage-2 comment cost
  at `ceil(N / COMMENT_STANCE_BATCH)` LLM calls however large the thread. It caps
  every voter at once, which matters: a cap on the LLM alone still runs seven models
  over the whole thread and leaves the per-comment comparison with an empty LLM
  column. **It ships at `0` — the whole thread — so this lever is available, not
  applied.** Reach for it when a run is too slow; the cost of leaving it off is
  ~0.92 s/comment of classifier CPU plus one stance batch per 25 comments.
- **Exact + near-duplicate caching.** Social feeds are highly repetitive
  (reshares, copypasta, viral captions). Hash + embedding dedup avoids reanalysis.
  Reuse is **post-level only**: a caption match is not a thread match, so the
  reused post keeps its own identity, engagement and reactions, and its comment
  thread is reported unanalysed rather than inheriting the source's per-comment
  labels (PROJECT_ASSESSMENT §13.3).
- **Batch & cluster summarization.** Don't summarize 10,000 posts individually.
  Cluster embeddings (pgvector + k-means/HDBSCAN), then have the LLM summarize a
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

Expected outcome, **revised against measurement**: the LLM touches **16%** of
posts on the shipped configuration. The per-batch bill, however, is dominated by
neither post-level nor cluster-level generation — it is dominated by **per-comment
labelling** (85–96% of calls), because full per-comment coverage is a deliberate
choice ([PROJECT_ASSESSMENT.md](PROJECT_ASSESSMENT.md) §6.3, §6.8). The lever
that matters most is comments-per-thread and the batch size, not the confidence
threshold. Cluster-level summarization is still a good idea but is not yet real:
it currently clusters **stub embeddings** by default (§5.9). Its output now
reaches the API and the dashboard as `embedding_clusters`, with
`embedding_clusters_are_stub` disclosing when the groupings are noise — until
§13.1 the summaries were computed, paid for, and stripped by the response model.
Quantified in [cost_estimation.md](cost_estimation.md).

---

## 6. Canonical output JSON

**The unit of analysis is a _post together with its comment thread_, not an
isolated post.** The post arrives **with its `comments[]` embedded** (a stored
sample of `engagement.commentCount`; see [data_contract.md](data_contract.md)). The
smart layer analyzes the whole thread and emits one JSON object per thread, ready
for a downstream service to consume. (Real example below: a Bangla photo+text post.)

Field provenance: `post_id`/`campaign_id`/`platform_post_id`/`media_type`/
`baseline_sentiment`/`baseline_viral_potential`/`reaction_breakdown`/`shares`/raw
`engagement` come **from the upstream payload** (verbatim or derived); `platform`
is **derived from the URL host**; everything else — including **all comment
sentiment** and **OCR text** — is **computed by this service**. The upstream coarse
post sentiment is kept as `baseline_sentiment` while `overall_sentiment`/
`sentiment_score` are our recomputed values — a **fusion of `text_sentiment`
(caption) and `image_sentiment` (the photo)**, cross-checked against
`reaction_breakdown` ([data_contract.md](data_contract.md) §4). `image_sentiment`/
`image_analysis` are `null` for text-only posts; `text_sentiment` is `null` when
the `caption` is `null`.

The assembler emits and validates this schema. `post_summary` is **grounded on the
caption + OCR + image** (`post_summary_grounding` records which) and written **in
the post's own language** (detected, never forced; Banglish normalized to the
dominant language). `comment_analysis.coverage` reports `analyzed / commentCount`
since only a stored sample of comments is shipped.

```json
{
  "post_id": "cmosjpp9305n0u9tskgmd1c4k",
  "campaign_id": "cmoldmxzr02d8fu22vhvrg23c",
  "platform": "facebook",
  "platform_post_id": "4460219584209360",
  "media_type": "PHOTO_TEXT",
  "language": "bn",
  "language_method": "fasttext",
  "post_text": "শাপলা চত্বরের সেই রক্তাক্ত রাতের কথা আজও ভুলিনি।",
  "post_type": "commemoration",
  "post_summary": "শাপলা চত্বরের ঘটনার স্মরণে একটি আবেগঘন বাংলা পোস্ট; ছবিতে সেই রাতের দৃশ্য। পোস্ট ও মন্তব্যে শোক ও আওয়ামী লীগের প্রতি ক্ষোভ প্রবল।",
  "post_summary_lang": "bn",
  "post_summary_source": "vlm",
  "post_summary_grounding": "caption+image",
  "overall_sentiment": "negative",
  "sentiment_score": -0.82,
  "text_sentiment": { "label": "negative", "score": -0.85 },
  "image_sentiment": { "label": "negative", "score": -0.7 },
  "baseline_sentiment": -0.85,
  "baseline_viral_potential": 0.78,
  "emotion": {
    "primary": "sadness",
    "scores": { "sadness": 0.62, "anger": 0.21, "fear": 0.06, "neutral": 0.05, "disgust": 0.03, "surprise": 0.02, "joy": 0.01 }
  },
  "intents": ["commemorate", "express_grievance"],
  "topics": ["shapla chattar", "2013", "politics", "grief"],
  "insight": "Commenters treat the anniversary as unfinished business, not history.",
  "entities": [
    { "text": "Shapla Chattar", "label": "EVENT", "confidence": 0.9 },
    { "text": "Awami League", "label": "ORG", "confidence": 0.86 }
  ],
  "brand_mentions": [],
  "keywords": ["শাপলা", "শোক", "আওয়ামী লীগ"],
  "toxicity_score": 0.18,
  "hate_speech_score": 0.12,
  "engagement": {
    "comment_count": 1562,
    "stored_comments": 112,
    "total_reactions": 84979,
    "share_count": 3189
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
  "shares": [],
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
    "vision_model": "SigLIP (sentiment) + Qwen2.5-VL (description)"
  },
  "comment_analysis": {
    "analyzed": 112,
    "coverage": 0.0717,
    "summary": "মন্তব্যের সিংহভাগ শোক ও ক্ষোভে ভরা; অনেকে আওয়ামী লীগের সমালোচনা করেছেন এবং বিচার দাবি করেছেন।",
    "summary_source": "llm",
    "sentiment_breakdown": { "positive": 6, "negative": 89, "neutral": 17 },
    "emotion_breakdown": { "anger": 38, "sadness": 47, "joy": 3, "fear": 6, "disgust": 8, "surprise": 1, "neutral": 9 },
    "method_breakdown": { "fast": 60, "model": 0, "llm": 52 },
    "themes": ["grief and remembrance", "anger at Awami League", "calls for justice"],
    "top_keywords": ["শাপলা", "শোক", "আওয়ামী"],
    "representative_comments": [
      {
        "id": "cmco_rep_1",
        "text": "এই ছবিগুলো প্রমাণ করে যে পুলিশ আমাদের বন্ধু ছিল না কখনো।",
        "sentiment": "negative",
        "likes": 574
      }
    ]
  },
  "confidence": { "overall": 0.92, "sentiment": 0.95, "language": 0.98, "topics": 0.88 },
  "processing": {
    "stage1_ms": 58,
    "stage2_ms": 412,
    "llm_used": true,
    "llm_backend": "local",
    "llm_model": "qwen2.5:7b",
    "role_models": { "summary": "qwen2.5:7b", "stage2": "qwen2.5:7b", "vlm": "qwen3-vl:4b" },
    "nlp_engine": "llm",
    "stub_mode": false,
    "degraded_components": [],
    "schema_version": "1.3"
  },
  "created_at": "2026-05-04T18:19:14",
  "scraped_at": "2026-05-05T17:39:44.464"
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
- **`image_analysis`** holds per-image visual `sentiment`, `ocr_text` (**our** OCR
  — the payload no longer ships `photoOcrTexts`), and a short `description` used to
  ground the summary. It is the bridge between the image and `post_summary`.
- **`reaction_breakdown`** (carried from upstream) is a free crowd **emotion
  signal** — `SAD`/`ANGRY`-heavy vs `HAHA`/`LOVE`-heavy — used to cross-check
  `emotion` and the fused sentiment. **`engagement.stored_comments`** +
  `comment_analysis.coverage` make the comment **sampling** explicit (we see a
  stored subset of `comment_count`). **`shares`** carries the `sampleShares`
  amplification signal.
- **`media_type`** is the upstream `postType` (TEXT/PHOTO/PHOTO_TEXT) and is
  **distinct** from the semantic `post_type` (complaint/promotion/…).
- `post_summary` is **grounded on caption + OCR + image** (`post_summary_grounding`
  lists which); `post_summary_lang` records the original language. `post_summary_source`
  is `vlm` when a vision-language model produced it, `llm` for text-only.
- `post_type`, `intents`, `brand_mentions`, and `comment_analysis.themes` are the
  "and something like that" fields — useful structured signal for downstream use.
- `post_summary_source` and `processing.llm_used`/`llm_backend`/`llm_model`
  make the hybrid behavior auditable (did we call Stage-2's LLM, on which
  backend — `local` or `groq` — which concrete model, was it worth it). The
  Stage-1 partial result additionally records its own engine/model for
  reproducibility (`nlp_engine`, `model_versions`).
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

| Layer          | Choice                                                                                                   | Why                                                                                                                                                                             |
| -------------- | -------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| API services   | **Python + FastAPI** (async)                                                                             | Matches team skills; great for I/O-bound APIs and ML glue                                                                                                                       |
| Workers        | **Python**, Celery or Ray for orchestration                                                              | Native ML ecosystem; Ray scales to multi-node cleanly                                                                                                                           |
| Model serving  | **Triton/ONNX/CTranslate2** (NLP); **SigLIP/CLIP + VLM** (vision); Stage-2 **vLLM**⇄**Groq**             | High GPU utilization for NLP; cheap visual sentiment + a VLM for image-grounded summaries; Stage-2 backend switchable local↔Groq                                                |
| Agents + tools | **Agent orchestrator** (FastAPI) on the pluggable LLM-B/VLM backend; **MCP servers** (FastAPI + MCP SDK) | Tool-using agents for corpus-level insight; MCP gives a standardized tool/resource interface over our stores + upstream (§11). Backend models (Qwen/Llama) support tool calling |
| Message bus    | **Kafka** (prod), **Redis Streams** (MVP)                                                                | Durable, partitioned, replayable at scale; simple to start                                                                                                                      |
| Operational DB | **PostgreSQL + pgvector**                                                                                | ACID jobs/state, JSONB flexibility, mature; the `pgvector` extension adds the `analysis_results.embedding` `vector(768)` column for dedup/search/clustering — one store, no separate vector service |
| Analytics DB   | **ClickHouse**                                                                                           | Columnar, billions of rows, sub-second aggregations for trends                                                                                                                  |
| Cache          | **Redis**                                                                                                | LLM/embedding/query cache, dedup set, rate limits                                                                                                                               |
| Object storage | **S3 / MinIO**                                                                                           | Raw payloads, reports, model artifacts                                                                                                                                          |
| Orchestration  | **Kubernetes** (prod), **Docker Compose** (MVP)                                                          | Autoscaling + HA vs simplicity                                                                                                                                                  |
| Autoscaling    | **KEDA** (scale on queue depth) + HPA                                                                    | Workers track backlog, not just CPU                                                                                                                                             |
| Observability  | **Prometheus + Grafana + Loki + OpenTelemetry + Jaeger**                                                 | Metrics, logs, traces — see [infrastructure.md](infrastructure.md)                                                                                                              |
| Frontend       | **React 19 + Vite + Tailwind** (`dashboard/`)                                                            | Shipped UI: chart.js charts, SSE live trace, per-comment labeller comparison. Calls the read APIs directly. The vanilla no-build dashboard this row originally specified is kept at `dashboard_legacy/` |

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
  isolation and quotas in the Auth service. **As implemented (5 Aug 2026):**
  - a JWT-shaped credential is verified as a token on **every** transport —
    header, `X-API-Key`, or the `?api_key=` query parameter `EventSource` needs.
    Only the header used to be parsed as one, so an expired or forged token
    authenticated on all four SSE streams;
  - API keys are stored as SHA-256 hashes in `api_keys`, and **`tenant_id` comes
    from that row**, never from a client-supplied token body. This is what makes
    the privacy-locked-tenant pin below enforceable for API-key callers;
  - claims taken from a token are allowlisted and `auth_method` is set
    server-side — the payload used to be spread last, so any claim in the token
    won, including the one distinguishing a human session from a service call;
  - `POST /v1/auth/sse-ticket` issues a single-use ~60-second ticket, so a
    streaming URL in a proxy log is harmless;
  - `JWT_SECRET` is read per call from one place by both issuer and verifier, so
    it can be rotated without a restart and cannot drift; a placeholder value is
    refused outside dev.
- **Fail closed, not open.** The tenant-policy check used to `return` on any
  database error — permitting egress to Groq precisely when it could not verify
  the policy. It now returns 503. A privacy guarantee that evaporates when the
  database hiccups is not a guarantee, and this is the failure mode where you
  most want it to hold.
- **Rate limiting & quotas** at the gateway (per key/tenant) to prevent abuse and
  runaway cost.
- **Input hardening:** size caps, schema validation, content sanitization;
  treat post text as untrusted (prompt-injection-aware when building LLM prompts
  — never let post content alter system instructions). **As implemented:** MCP
  tool results — which carry Facebook comment text verbatim — are wrapped in
  `<tool_data trust="untrusted">` delimiters with forged-delimiter
  neutralisation, and the agent system prompt states that content inside them is
  data and must never be obeyed. On a corpus of political content with
  adversarial participants this is a realistic threat, not a hypothetical. **No
  general solution to prompt injection exists**; these are the standard
  mitigations, and they should be described as risk reduction rather than a fix
  ([FEATURES.md](FEATURES.md) §13).
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
  **Enforced at the enqueue boundary.** The workers have no database and no
  tenant, so they cannot evaluate a policy; the API resolves each job's backend
  (request > toggle > env), applies the lock where the tenant *is* known, and
  stamps the decision into the job envelope, which both stages honour. An
  explicit request for `groq` is refused; the global toggle is downgraded to
  `local` for a locked tenant rather than erroring, since an operator's switch is
  not that tenant's choice. `PUT /v1/config/llm` requires an admin role. Until
  §13.5 the pipeline read the global key directly and the guarded per-request
  option was read by no worker ([PROJECT_ASSESSMENT.md](PROJECT_ASSESSMENT.md)
  §13.5).
- **Secrets:** Kubernetes Secrets / Vault; no secrets in images or env files —
  including the **Groq API key**, which is mounted only into Stage-2 workers.
- **Network:** private subnets for DBs and model servers; only the gateway is
  internet-facing. NetworkPolicies in K8s.
- **Audit logging** of admin and data-access actions.
- **Supply chain:** pinned dependencies, image scanning, signed images.

---

## 9a. Watchlist-driven target stance

An optional layer that answers a question document-level sentiment cannot:
**not "is this comment angry?" but "who is it angry at?"** Full design in
[stance_targets.md](stance_targets.md).

```text
config/stance_targets.yml                (operator-supplied, versioned)
        │
        ▼
src/defense/libs/stance_targets.py    alias matcher — Bangla script · romanized Banglish · English
        │
        ├──▶ STAGE 1: match every comment of EVERY post (pure string work, free)
        │            + deterministic clause-based scorer  → target_stances
        │
        └──▶ STAGE 2: matched entities are injected into the comment-stance
                     prompt THAT ALREADY RUNS  → target_stances (method="llm")
                     ⇒ zero additional LLM calls
        │
        ▼
per-post rollup: {entity: {mentions, supportive, opposing, neutral, method}}
```

Three design decisions worth knowing:

1. **Aliases are the feature.** This corpus writes the same entity in three
   scripts. A watchlist matching one spelling silently matches almost nothing —
   the §5.1 failure mode again — so a target that matches *zero* comments across
   a run is logged as the alias-coverage bug it almost certainly is.
2. **Target stance lives in its own output field**, never merged into
   `sentiment`. A comment can be positive in tone while opposing a listed entity;
   conflating the two destroys the only distinction the feature exists to make.
3. **It is a stated bias model, not a measurement.** A file declaring "support
   for X is positive" encodes a political stance into the labels — legitimate for
   a monitoring product, indefensible presented as neutral analysis. The file is
   versioned with a named owner per entry, and the `neutral:` bucket exists for
   when monitoring rather than advocacy is wanted.

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

---

## 11. Agentic insight layer — MCP servers + AI agents

The per-post / per-comment pipeline (§3) is **deterministic NLP + single-shot
LLM/VLM** and stays that way — that is the cost model. **Above** it sits a small,
**selective agent layer** for **corpus-level** work that genuinely needs planning
and multiple tool calls: analyst Q&A, grounded report/insight generation, and
targeted deep-dives. **Agents never run per post** (that would re-introduce the
"LLM-everywhere" cost the design forbids — §1); they run on demand or on a schedule,
over the data we already analyzed.

### MCP servers (standardized tools/resources)

Agents reach data and actions through **MCP (Model Context Protocol) servers** —
small FastAPI services exposing typed tools over MCP (stdio/HTTP), so the agent
(and any future LLM client) gets one consistent tool interface instead of bespoke
glue:

| MCP server      | Tools it exposes                                                                 | Backed by                      |
| --------------- | -------------------------------------------------------------------------------- | ------------------------------ |
| `analytics-mcp` | `trend_query`, `sentiment_over_time`, `top_posts`, `reaction_mix`, point lookups | ClickHouse + PostgreSQL        |
| `retrieval-mcp` | `semantic_search`, `get_post`, `get_thread`, `representative_comments`           | Postgres + pgvector            |
| `ingest-mcp`    | `pull_campaign`, `fetch_more_comments` (raise coverage), `refresh_post`          | upstream post-with-details API |

MCP servers are **read-mostly** and respect the same auth/tenant scoping as the
read APIs; `ingest-mcp` is the only one that triggers (idempotent) writes into our
own DB, never into upstream.

### AI agents (selective, corpus/report tier)

Each agent is an LLM loop on the dedicated **`agent`** role (`AGENT_LOCAL_MODEL`,
default `llama3.1:8b-16k`, ⇄ `AGENT_GROQ_MODEL`, default
`llama-3.3-70b-versatile`); both support tool/function calling, which MCP builds
on. A **VLM** step is used when an answer needs the images. The roster is **nine**
agents, defined in [`registry.py`](src/defense/services/agents/registry.py) — that
file is the source of truth for tools and budgets, and `GET /v1/agents/types`
serves it live:

- **Insight / Analyst agent** (budget 10) — answers questions ("what are people
  saying about X this week?") by planning
  `trend_query → semantic_search → get_thread → synthesize → cite`. This replaces
  single-shot RAG generation ([models.md](models.md) §5) with a tool-using loop, and
  every claim is grounded in retrieved posts/comments.
- **Coverage deep-dive agent** (5) — fires when a post's `comment_analysis.coverage`
  is low (`storedCommentRows ≪ commentCount`) or a post is flagged viral; calls
  `ingest-mcp.fetch_more_comments`, re-runs the comment pass, and escalates a richer
  thread analysis. This is the agentic way to spend effort only where it matters.
- **Alerting / monitoring agent** (8, scheduled) — checks two thresholds
  **separately**: negative sentiment >50% in a period, and avg toxicity >0.6. Its
  prompt names which tool serves which, because `sentiment_over_time` has no
  toxicity field and `trend_query` returns `avg_toxicity` on every row — and a run
  that read neither still delivered a confident toxicity verdict.
- **Stance agent** (12) — target-dependent stance across the operator watchlist,
  the system's most novel signal made interactive.
- **Comparator** (15) — cross-campaign and cross-period comparison.
- **Toxicity agent** (14) — harm patterns; `search_comments` lets it find a
  harassment pattern directly instead of walking `top_posts → get_thread`.
- **Narrative agent** (12) — theme discovery over embedding clusters.
- **Quality agent** (8) — coverage, ensemble agreement and vector provenance: the
  agent that answers "how reliable is this?".
- **Reporter** (15) — drafts the grounded **trend / brand / political report** end
  to end.

### Guardrails (so agents don't blow the cost model)

- **Gated & budgeted.** Agents are invoked by an explicit request, a schedule, or a
  router escalation — not per post. Per-run **tool-call and token budgets** cap cost.
- **Cached.** Agent results and tool outputs are cached (Redis) keyed by
  `(agent, inputs, backend, model)`; repeated questions are free.
- **Grounded & auditable.** Reports cite the posts/comments retrieved; each run
  records the backend, model, tools called, and token usage (ties into `/v1/usage`).
- **A result cannot eat the context window.** Tool results are serialised with
  `ensure_ascii=False` (the escaping alone was half the token bill on a Bengali
  corpus) and capped at 6,000 characters, dropping **whole rows** with a note
  saying what was withheld. Citations are read off what the model was *shown*.
- **A repeat is not dispatched.** A byte-identical call is answered from the first
  result with the untried tools named; after two, the tools are withdrawn — a model
  repeating itself has stopped gathering evidence.
- **A run is only `completed` if it answered something.** Code output, payload
  narration, self-narration and the empty-template shape each get one
  markdown-synthesis retry and then fail the run. An answer that denies retrieving
  post-level data in a run that retrieved it is contradicted in writing, and
  ungrounded ids and quotes are appended as a warning block rather than dropped.
- **Backend-agnostic & policy-bound.** Agents honor the same `local`⇄`groq` policy
  as Stage 2 — privacy-locked tenants keep agent calls on `local` so retrieved
  content never egresses (§9).

This layer was **not required for the MVP** (the core pipeline shipped first) — see
[plan.md](plan.md), which is the dated plan record. It has since landed in full:
nine agents, the MCP tool servers, and the hardening above. What it still lacks is
a *measurement* — the agent answers are unmeasured for accuracy, which is the
`🟡 Works, unmeasured` in [FEATURES.md](FEATURES.md) §9.

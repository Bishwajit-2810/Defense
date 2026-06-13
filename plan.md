# Implementation Plan & Roadmap

Phased plan to build the smart layer from [what.txt](what.txt) from MVP (1k) →
Production (10k) → Enterprise (100k), plus best practices for processing 10,000+
posts efficiently. Read alongside [architecture.md](architecture.md).

---

## Phase 0 — Foundations (week 0–1)

- Repo + monorepo layout (services, workers, infra, models, dashboard).
- Lock the **input contract** ([data_contract.md](data_contract.md)): the upstream
  **post-with-details** schema (real sample in
  [posts_with_details.json](posts_with_details.json)) — post with embedded
  `comments[]`, `engagement`, `reactionBreakdown`, and `sampleShares` — the pull +
  own-DB integration (no write-back), platform-from-URL, and the recompute-with-baseline
  sentiment policy. Lock the **canonical output JSON schema**
  ([architecture.md](architecture.md) §6, worked in [examples.md](examples.md)) and
  a shared JSON Schema validator. These contracts (what we pull, what downstream
  consumes) are the heart of the microservice; lock them early.
- Build the **upstream API client + ingestion**: pull the post-with-details payload
  keyed by CUID `id` (comments embedded), derive `platform`, keep upstream
  `sentiment`/`viralPotential` as `baseline_*`, **run OCR on `photoUrls`** (no OCR
  is shipped), record comment **coverage** (`storedCommentRows`/`commentCount`),
  upsert into **our own database**.
- Stand up local Docker Compose skeleton: Postgres (`pgvector/pgvector:pg16`,
  with the pgvector extension for vector search), Redis, ClickHouse, MinIO, a stub
  API, Prometheus/Grafana.
- Pick and pin model versions ([models.md](models.md)), **including the vision
  models** (image-sentiment: SigLIP/CLIP; image-description/summary VLM:
  Qwen2.5-VL); download local weights to object storage. Define the **Stage-2 LLM
  backend interface** (OpenAI-compatible, with a text and a vision model id per
  backend) and wire both providers behind it — `local` (vLLM) and `groq` — selected by
  `LLM_BACKEND` config with a per-request override and per-tenant policy. Pin the
  Groq role→model IDs and store `GROQ_API_KEY` as a secret.
- Build a small **labeled eval set** per task and per language (bn / en /
  Banglish) — needed to tune confidence thresholds and judge fine-tuning later.
  The scoring rubric, gold sets, ship gates, and drift monitoring are in
  [evaluation.md](evaluation.md).

**Exit criterion:** a single post flows API → Redis Stream → stub worker →
Postgres → `GET /analysis/{id}`, with valid JSON.

---

## Phase 1 — MVP (week 1–4) — 1,000 posts/batch

Goal: prove the hybrid pipeline and output quality end-to-end, cheaply.

> **First target (priority order)** — the multimodal post-and-thread pipeline,
> shippable on the data we have today (comments are **embedded** in the payload):
> **(1) post text sentiment** (caption) → **(2) image sentiment** (visual model on
> the photo, when present; we also OCR it) → **(3) fuse** into post
> `overall_sentiment` (cross-check `reactionBreakdown`) → **(4) post summary
> grounded on caption + image/OCR** → **(5) per-comment sentiment** over the
> embedded thread. See [data_contract.md](data_contract.md) §4.

- **Ingestion service:** **pull the post-with-details payload** (comments embedded),
  derive `platform` from URL host, keep upstream `sentiment`/`viralPotential` as
  `baseline_*`, **run OCR on `photoUrls`** (no OCR is shipped), normalize the
  caption + OCR (Unicode, Bangla/English/Banglish script tagging), take the embedded
  comment thread (record **coverage** `storedCommentRows`/`commentCount`),
  content-hash **dedup** (Redis), upsert into **our own DB** keyed by CUID `id`, job
  creation, enqueue to Redis Streams. The `/v1/posts/upload` push path is wired for
  replay/external sources.
- **Stage-1 NLP + vision worker:** runs **post first, then each comment**.
  - _Text:_ language/Banglish detection (fastText) → shared XLM-R encoder with
    sentiment/emotion/topic/intent heads (**recomputed** `text_sentiment`, upstream
    score kept as baseline) → toxicity/hate → NER (GLiNER/spaCy) → embedding
    (`paraphrase-multilingual-mpnet-base-v2`, 768-dim — matches the
    `analysis_results.embedding vector(768)` column) → keywords.
  - _Vision (image posts):_ a cheap **visual** model (SigLIP/CLIP zero-shot or a
    fine-tuned ViT) scores `image_sentiment`; we **run OCR** (PaddleOCR/Tesseract);
    a small VLM (Qwen2.5-VL) produces an image description.
  - _Fuse_ `text_sentiment` + `image_sentiment` → post `overall_sentiment`
    (cross-checked against `reactionBreakdown`). Run the text models over **each
    embedded comment** (the upstream ships none) → thread `sentiment_breakdown` with
    **coverage**. Micro-batched; confidence per field.
- **Router/Triage:** confidence gates + task flags; decide LLM/VLM routing.
- **Stage-2 LLM/VLM worker:** a backend-agnostic worker for selective summarization
  /insight — running either backend, `local` (vLLM serving **LLM-A** quantized 7B
  **plus a VLM** `Qwen2.5-VL` for image-grounded summaries) or `groq` (fast text
  model plus a vision model). The **`post_summary` is grounded on caption + OCR +
  image** for
  photo posts. Ship both adapters in the MVP so the switch is exercised early;
  **LLM response cache** in Redis keyed by `(backend, model, task, content_hash)`.
  (LLM-B is added in Phase 2 for cluster/report quality.)
- **Result assembler:** merge + JSON-schema validate + write to Postgres
  (including the `analysis_results.embedding` `vector(768)` column via pgvector),
  ClickHouse, MinIO.
- **APIs:** `/posts/upload`, `/analysis/run`, `/analysis/{id}`, `/reports`
  (basic), auth (API key + JWT).
- **Web dashboard (v1) — plain HTML/CSS/JS** (vanilla, no framework): job status,
  results table, per-post sentiment + comment breakdown + reaction chart, served as
  static files calling the read APIs.
- **Monitoring:** Prometheus + Grafana + Loki; track LLM-routing rate + cache hits.

**Exit criteria:** process 1,000-post batches reliably; measured LLM slice in
single digits %; per-task accuracy baselined on the eval set; cost-per-1k
recorded.

---

## Phase 2 — Production hardening (week 4–10) — 10,000 posts/batch

Goal: scale, reliability, and the move to Kubernetes.

- **Migrate bus Redis Streams → Kafka** (partitioned by `hash(post_id)`); add
  **DLQ** topic + replay tooling.
- **Split services** (auth, ingestion, reporting, user-mgmt) into separate
  Deployments; introduce the Router as its own concern if not inlined.
- **Kubernetes** ([deployment.md](deployment.md)): namespaces, GPU node pools,
  **KEDA** autoscaling on Kafka lag, HPA on API tier, NetworkPolicies, secrets,
  ingress + cert-manager.
- **Model serving upgrade:** Triton for the NLP fleet (dynamic batching), and add
  the **LLM-B** role — on `local`, a quantized 14B/32B on a data-center GPU
  (L40S/A100) via vLLM alongside LLM-A; on `groq`, just a larger model ID.
- **Reliability:** retries + backoff everywhere, idempotent assembler, circuit
  breakers around each LLM backend; if a backend is saturated, degrade
  LLM-B→LLM-A, **fail over local↔Groq** (where policy allows), or fall back to
  Stage-1-only; PDBs, multi-replica.
- **Cluster summarization:** k-means/HDBSCAN over embeddings → LLM summarizes
  clusters, not posts (key cost lever at 10k).
- **Reporting:** trend analysis, brand-mention tracking, political analysis on
  ClickHouse; grounded report generation via RAG (Postgres + pgvector + LLM).
- **Agentic insight layer ([architecture.md](architecture.md) §11):** stand up the
  **MCP servers** (`analytics-mcp`, `retrieval-mcp`, `ingest-mcp` — FastAPI + MCP
  SDK) and the **agent orchestrator**; ship the **Insight/Analyst agent**
  (`POST /v1/agents/query` + agent-generated reports) on **LLM-B** over those tools,
  then the **coverage deep-dive** and **alerting** agents. Gate, cache, and
  budget-cap every agent run; honor the `local`⇄`groq` tenant policy. (Corpus-tier
  only — never per post.)
- **Observability:** OpenTelemetry traces → Jaeger; dashboards for throughput,
  cost-per-batch, LLM slice, cache hit rates, queue lag, DLQ size.
- **First fine-tune:** LoRA/QLoRA on router-flagged + labeled data; ship only if
  it beats baseline on the eval set ([models.md](models.md) §4).

**Exit criteria:** 10,000-post batches within target latency; autoscaling proven
under burst; DLQ < threshold; LLM slice held in single digits %; per-language
accuracy improved over MVP baseline.

---

## Phase 3 — Enterprise scale (week 10+) — 100,000 posts/batch

Goal: horizontal scale, cost efficiency, resilience at volume.

- **GPU node pools per stage**, spot/preemptible for batch surges (Kafka replay
  makes preemption safe); reserved capacity for steady base.
- **Data layer scale-out:** Postgres read replicas (the pgvector index scales with
  them); ClickHouse cluster (shards+replicas); partition/replicate the pgvector
  embeddings by tenant; possibly a dedicated vector engine (e.g. Milvus) if vectors
  reach billions ([possible_architecture.md](possible_architecture.md) §5).
- **LLM scale:** on `local`, multiple vLLM replicas for both models (tensor
  parallelism for LLM-B), absorbing spikes by adding GPU replicas; on `groq`, no
  GPU scaling — negotiate quota for peak. **Hybrid** is the typical enterprise
  pattern: own GPUs for the steady base, burst overflow to Groq during spikes.
- **Aggressive caching + dedup** tuning — the dominant cost lever at this scale.
- **Service mesh** (Linkerd) for mTLS + canary; GitOps (ArgoCD).
- **Continuous fine-tuning loop** + distillation to shrink the LLM slice further.
- **Multi-tenancy & quotas** fully enforced; per-tenant cost metering (`/usage`).

**Exit criteria:** 100k batches at target SLA; cost-per-1k flat or falling vs.
Production; graceful degradation under failure proven (chaos test).

---

## Best practices for processing 10,000+ posts efficiently

1. **Never LLM-per-post.** Route by confidence; LLM only for what small models
   can't do or are unsure about ([architecture.md](architecture.md) §5).
2. **Dedup hard.** Hash + near-duplicate (embedding) detection — social feeds are
   highly repetitive; cache hits are free results.
3. **Micro-batch on GPU.** Triton dynamic batching + vLLM continuous batching =
   high utilization, low cost per post.
4. **Cluster, then summarize.** One LLM call per cluster, not per post.
5. **Share one encoder across tasks.** Multiple classification heads on a single
   XLM-R pass instead of N independent models.
6. **Partition = parallelism.** Partition the bus by `hash(post_id)`; keep
   partitions ≥ consumers; scale workers on **lag**, not CPU.
7. **Stateless workers, durable queue.** Scale to zero between batches, surge for
   bursts; at-least-once + idempotent writes = no loss/double-count.
8. **Quantize everything servable.** INT8/FP16 NLP (ONNX/CTranslate2), AWQ/GPTQ
   LLM.
9. **Spot for batch.** Cheap GPUs for surges; Kafka replay covers preemption.
10. **Cache every layer.** Dedup, embeddings, LLM responses, query results.
11. **Backpressure + DLQ.** Bounded queues prevent cascades; DLQ captures
    poison messages for replay.
12. **Measure the right things.** % posts hitting LLM, cache hit rates,
    cost-per-1k, per-language accuracy, queue lag — these drive every tuning
    decision.
13. **Token minimization.** Send only needed fields + truncated text; structured
    JSON output mode.
14. **Reproducible replay.** Record `model_versions` per result; replay batches
    from Kafka after model upgrades.

---

## Risk register (top items)

| Risk                                                          | Mitigation                                                                                                                                                                |
| ------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Comment sample ≪ total (`storedCommentRows` ≪ `commentCount`) | Analyze the stored sample; **report coverage** (`analyzed/commentCount`) — never imply full coverage; request deeper comment pulls where it matters                       |
| Comment sentiment is entirely ours (upstream ships `null`)    | Same multilingual sentiment models as the post; Banglish-heavy eval set for comments; weight by `likes` for themes                                                        |
| Image/VLM cost or latency (every image post)                  | Cheap visual model (SigLIP/CLIP) for `image_sentiment` on all images; VLM only **selectively** for the grounded summary (router-gated); cache by image hash; batch on GPU |
| Visual sentiment accuracy on local content                    | Calibrate against `baseline_sentiment`; eval set including news-graphics/memes; fall back to OCR-text sentiment when image confidence is low                              |
| Upstream schema / platform drift                              | Platform derived from URL host (open-ended); ingest keyed by CUID `id`; tolerate new fields, validate the ones we use                                                     |
| Bangla / Banglish accuracy below bar                          | Multilingual encoder + Bangla fine-tune + Banglish-heavy eval set                                                                                                         |
| LLM slice creeps up → cost spikes                             | Confidence-gate tuning, caching, clustering, alert on LLM-share metric                                                                                                    |
| GPU cost overrun                                              | Spot for batch, reserved base, quantization, right-sizing                                                                                                                 |
| Queue/worker overload                                         | KEDA on lag, bounded queues, backpressure, DLQ                                                                                                                            |
| Data privacy / PII                                            | Encryption at rest/in transit, retention/deletion APIs, access audit                                                                                                      |
| Prompt injection via post text                                | Treat post text as untrusted; never let it alter system instructions                                                                                                      |
| Model regression on upgrade                                   | Eval-set gate before ship; Kafka replay to compare                                                                                                                        |
| Groq backend leaks PII (data egress)                          | Default `local`; pin sensitive tenants to `local`; reject policy-violating overrides; audit `llm_backend`                                                                 |
| Groq outage / rate-limit / price change                       | Failover to `local` (or degrade to Stage-1-only); retry on 429; alert on Groq error/cost; cap per-call tokens                                                             |

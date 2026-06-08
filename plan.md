# Implementation Plan & Roadmap

Phased plan to build the smart layer from [what.txt](what.txt) from MVP (1k) →
Production (10k) → Enterprise (100k), plus best practices for processing 10,000+
posts efficiently. Read alongside [architecture.md](architecture.md).

---

## Phase 0 — Foundations (week 0–1)

- Repo + monorepo layout (services, workers, infra, models, dashboard).
- Define the **input schema** (post + nested comment thread) and the **canonical
  output JSON schema** ([architecture.md](architecture.md) §6, worked out in
  [examples.md](examples.md)), plus a JSON Schema validator shared by all
  services. These two contracts (what the scraper sends, what downstream
  consumes) are the heart of the microservice; lock them early.
- Stand up local Docker Compose skeleton: Postgres, Redis, Qdrant, ClickHouse,
  MinIO, a stub API, Prometheus/Grafana.
- Pick and pin model versions ([models.md](models.md)); download weights to
  object storage.
- Build a small **labeled eval set** per task and per language (bn / en /
  Banglish) — needed to tune confidence thresholds and judge fine-tuning later.

**Exit criterion:** a single post flows API → Redis Stream → stub worker →
Postgres → `GET /analysis/{id}`, with valid JSON.

---

## Phase 1 — MVP (week 1–4) — 1,000 posts/batch

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
- **Result assembler:** merge + JSON-schema validate + write to Postgres,
  ClickHouse, Qdrant, MinIO.
- **APIs:** `/posts/upload`, `/analysis/run`, `/analysis/{id}`, `/reports`
  (basic), auth (API key + JWT).
- **Flutter dashboard (v1):** upload, job status, results table, basic charts.
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
  **LLM-B** — a quantized 14B/32B on a data-center GPU (L40S/A100) via vLLM —
  alongside LLM-A. Both local.
- **Reliability:** retries + backoff everywhere, idempotent assembler, circuit
  breakers around each LLM; if LLM-B is saturated, degrade to LLM-A or
  Stage-1-only (no external API fallback), PDBs, multi-replica.
- **Cluster summarization:** k-means/HDBSCAN over embeddings → LLM summarizes
  clusters, not posts (key cost lever at 10k).
- **Reporting:** trend analysis, brand-mention tracking, political analysis on
  ClickHouse; grounded report generation via RAG (Qdrant + LLM).
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
- **Data layer scale-out:** Postgres read replicas; ClickHouse cluster
  (shards+replicas); Qdrant sharded by tenant; possibly Milvus if vectors reach
  billions ([possible_architecture.md](possible_architecture.md) §5).
- **LLM scale:** multiple vLLM replicas for both local models (tensor parallelism
  for LLM-B); absorb spikes by adding GPU replicas, not by calling an external API.
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

| Risk                                 | Mitigation                                                             |
| ------------------------------------ | ---------------------------------------------------------------------- |
| Bangla / Banglish accuracy below bar | Multilingual encoder + Bangla fine-tune + Banglish-heavy eval set      |
| LLM slice creeps up → cost spikes    | Confidence-gate tuning, caching, clustering, alert on LLM-share metric |
| GPU cost overrun                     | Spot for batch, reserved base, quantization, right-sizing              |
| Queue/worker overload                | KEDA on lag, bounded queues, backpressure, DLQ                         |
| Data privacy / PII                   | Encryption at rest/in transit, retention/deletion APIs, access audit   |
| Prompt injection via post text       | Treat post text as untrusted; never let it alter system instructions   |
| Model regression on upgrade          | Eval-set gate before ship; Kafka replay to compare                     |

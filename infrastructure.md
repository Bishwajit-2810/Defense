# Infrastructure — GPU Sizing, Monitoring, Caching, Scaling

Infrastructure and scaling for the smart layer from [what.txt](what.txt): GPU
sizing, the monitoring stack, caching, and how it scales from 1k → 100k threads
per batch. Cost figures live in [cost_estimation.md](cost_estimation.md).

---

## 1. Throughput model (how to size anything)

"Threads per batch" must be translated to a target latency to size hardware.
**A unit is a post + its comment thread**, so a single "item" can be a handful to
hundreds of short texts — Stage-1 throughput is better measured in **texts
(post + comments) per second** than threads/sec. Two example service levels:

- **Stage-1 NLP** (shared XLM-R encoder + heads + embedding), batched on GPU:
  order of **hundreds–low-thousands of texts/sec** on a single modern GPU
  depending on text length and quantization. A thread with 50 comments = ~51
  texts.
- **Stage-2 LLMs** (two local models on vLLM, continuous batching): **LLM-A**
  (7B/8B) for per-post refinement and **LLM-B** (14B/32B) for cluster/report
  generation — order of **thousands of output tokens/sec** aggregate; but they
  only see the **selective slice** (single-digit % of posts) and mostly
  **cluster-level** calls. No external API is involved.

So the NLP fleet, not the LLM, dominates raw text throughput; the LLM dominates
_quality_ work on a small slice (one cluster-level summary per thread, not one
per comment). Size them independently.

---

## 2. GPU requirements by stage

### MVP — 1,000 posts/batch

- **1 × consumer GPU** (e.g. RTX 4090 / 3090, 24 GB) runs the whole show: the
  NLP suite (quantized) + the local LLMs via vLLM, time-sliced. At MVP scale run
  just **LLM-A** (quantized 7B) for all Stage-2 work; add **LLM-B** when
  cluster/report quality demands it.
- CPU-only is even possible for the smallest NLP models if no GPU is available,
  at lower throughput.
- **Recommendation:** single workstation/server with one 24 GB consumer GPU, or a
  single cloud GPU instance. Docker Compose.

### Production — 10,000 posts/batch

- **NLP fleet:** 1–2 GPUs (24 GB consumer or 1× data-center L4/A10, 24 GB) with
  Triton dynamic batching, autoscaled by queue depth.
- **LLMs:** **LLM-A** (quantized 7B) on an L4/A10 + **LLM-B** (quantized 14B/32B)
  on a data-center GPU (**L40S / A100 40GB**); at lower volume LLM-A can share the
  NLP GPU. Continuous batching keeps utilization high. Both local — no API.
- **Recommendation:** small Kubernetes cluster, 2–4 GPUs total, KEDA autoscaling.

### Enterprise — 100,000 posts/batch

- **NLP fleet:** several data-center GPUs (**A10 / L4 / L40S**), autoscaled;
  horizontal first.
- **LLMs:** multiple **A100/H100** (or several L40S) behind vLLM serving both
  local models, multi-replica with tensor parallelism for LLM-B; scale out by
  adding GPU replicas for spikes (never an external API).
- **Recommendation:** multi-node K8s, GPU node pools per stage, aggressive
  caching + clustering to hold down the LLM slice.

### GPU class guidance

| Class                              | Examples                                                  | Best for                                 | Tradeoff                                                          |
| ---------------------------------- | --------------------------------------------------------- | ---------------------------------------- | ----------------------------------------------------------------- |
| **Consumer**                       | RTX 3090 / 4090 (24 GB)                                   | MVP, NLP fleet, dev                      | Cheapest per FLOP; no NVLink/ECC, licensing limits in some clouds |
| **Data-center inference**          | L4 (24 GB), A10 (24 GB), L40S (48 GB)                     | Production NLP + small/mid LLM           | Best perf/$ for inference; power-efficient                        |
| **Data-center training/large LLM** | A100 (40/80 GB), H100 (80 GB)                             | Enterprise LLM, fine-tuning large models | Expensive; reserve for where they pay off                         |
| **Cloud alternatives**             | AWS g5/g6, GCP G2, Azure NC/ND, Lambda, RunPod, CoreWeave | Any stage, elastic                       | Spot/preemptible for batch saves a lot; on-demand for steady base |

**Cost lever:** run the steady base load on owned/reserved GPUs and burst to
**spot/preemptible cloud GPUs** for large batches — batch work tolerates
interruptions (just re-queue from Kafka).

---

## 3. Monitoring stack

```text
            ┌──────────────────────────────────────────────────────┐
   services │ OpenTelemetry SDK in every service/worker             │
   & workers│  emits: metrics, logs, traces                         │
            └───────┬───────────────┬───────────────┬──────────────┘
                    │ metrics       │ logs          │ traces
              ┌─────▼─────┐   ┌─────▼─────┐   ┌──────▼──────┐
              │ Prometheus│   │   Loki    │   │   Jaeger    │
              │ (scrape/  │   │ (logs)    │   │ (traces)    │
              │  remote-w)│   │           │   │             │
              └─────┬─────┘   └─────┬─────┘   └──────┬──────┘
                    └───────────────┼────────────────┘
                              ┌─────▼─────┐
                              │  Grafana  │  dashboards + alerts
                              └───────────┘
```

- **Prometheus** — metrics: queue depth/lag, posts/sec per stage, GPU
  utilization (DCGM exporter), LLM-routing rate, cache hit rate, error rate,
  p50/p95/p99 latency, DLQ size.
- **Grafana** — dashboards (pipeline throughput, cost-per-batch, LLM slice %) +
  alerting (Alertmanager): queue lag > threshold, DLQ growth, GPU saturation,
  error-rate spikes.
- **Loki** — centralized structured logs, correlated to traces by trace_id.
- **OpenTelemetry** — single instrumentation standard across all services;
  exports metrics→Prometheus, logs→Loki, traces→Jaeger.
- **Jaeger** — distributed traces follow a post from ingestion → NLP → router →
  (LLM) → assembler → store; essential for debugging latency and the router.

**Key product metrics to watch:** % of posts that hit the LLM, cache hit rates
(dedup/embedding/LLM), cost per 1k posts, per-language accuracy drift.

---

## 4. Caching strategy (summary)

Detailed rationale in [possible_architecture.md](possible_architecture.md) §6.

| Cache                          | Key                           | Purpose                                   | TTL                  |
| ------------------------------ | ----------------------------- | ----------------------------------------- | -------------------- |
| **Dedup set** (Redis)          | `content_hash`                | Skip re-analysis of exact dupes/reshares  | long / per-retention |
| **Embedding cache** (Redis)    | `content_hash`                | Avoid recomputing vectors                 | long                 |
| **LLM response cache** (Redis) | `(model, task, content_hash)` | Free repeats of LLM calls                 | medium–long          |
| **Query cache** (Redis)        | normalized query              | Fast dashboard aggregations               | short (secs–mins)    |
| **CDN**                        | URL                           | Flutter web assets, static report exports | long, versioned      |

On real social feeds these caches remove a large fraction of total work — they
are a primary cost lever, not an afterthought.

---

## 5. Scaling strategy

- **Horizontal first.** Add worker replicas + queue partitions before bigger GPUs.
- **Autoscale on backlog.** **KEDA** scales each worker pool on its queue
  lag/depth (not CPU) — the right signal for batch pipelines. HPA covers the
  stateless API tier on CPU/RPS.
- **Independent pools.** NLP and LLM pools scale separately; the cheap fleet can
  grow 10× without touching the LLM GPUs.
- **Partitioning.** Partition the bus by `hash(post_id)`; keep partitions ≥ max
  consumers so workers never starve.
- **Stateless workers.** All state in queue + stores → scale to zero between
  batches, scale out instantly for a 10k/100k burst.
- **Spot for batch.** Use preemptible/spot GPU nodes for batch surges; Kafka
  replay makes interruptions safe.
- **Backpressure.** Bounded queues + DLQ prevent overload cascades.
- **DB scaling.** PostgreSQL read replicas; ClickHouse shards/replicas; Qdrant
  collections sharded by tenant at large scale.

See [deployment.md](deployment.md) for the concrete Kubernetes/KEDA setup and
[plan.md](plan.md) for the order to build it in.

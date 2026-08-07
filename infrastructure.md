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
- **Stage-1 vision** (image sentiment + **our OCR**): a **SigLIP/CLIP** pass on each
  image post is cheap — **hundreds of images/sec** on one GPU (one forward pass per
  image); **OCR** (PaddleOCR/Tesseract) adds a light CPU/GPU pass per image since
  the payload no longer ships OCR text. Most posts have an image, so size for ~one
  image embed + OCR per post on top of the text load. Image **description/summary**
  uses the VLM and runs only on the **selective** Stage-2 slice, not every image.
  **Currently unexercised** — no image bytes are reachable, so this line is
  sizing for a capability that does not run today
  ([PROJECT_ASSESSMENT.md](PROJECT_ASSESSMENT.md) §5.2). Exclude it from an MVP
  GPU budget and restore it when the objects are uploaded.
> **Size the LLM for COMMENTS, not posts.** Since every non-emoji comment gets an
> LLM label, **85–96% of Stage-2 LLM calls are comment-level** — ~350 calls for a
> 43-post corpus against ~20 post-level ones
> ([PROJECT_ASSESSMENT.md](PROJECT_ASSESSMENT.md) §6.8). The throughput figure
> that matters is *comment batches per second* (25 comments per call, 3 batches
> in flight per post), not posts per second. Capping it is a config choice
> (`STAGE1_LLM_COMMENT_MAX`, `COMMENT_STANCE_MAX_PER_POST`) that trades coverage
> for cost — quote whichever you chose alongside the coverage number.

- **Stage-2 LLMs** (two roles — **LLM-A** 7B/8B for per-post refinement, **LLM-B**
  14B/32B for cluster/report generation): order of **thousands of output
  tokens/sec** aggregate; but they only see the **selective slice** (16% of posts,
  % of posts) and mostly **cluster-level** calls. This throughput is delivered by
  the configured backend: `local` (vLLM continuous batching on our GPUs — sized
  below) or `groq` (Groq LPU — throughput is Groq's to scale, bounded by your
  rate-limit/quota rather than your GPUs).

So the NLP fleet, not the LLM, dominates raw text throughput; the LLM dominates
_quality_ work on a small slice (one cluster-level summary per thread, not one
per comment). Size them independently — and note that the `groq` backend removes
LLM GPU sizing from the equation entirely (you size only the NLP fleet).

- **Agent orchestrator + MCP servers** ([architecture.md](architecture.md) §11) are
  **stateless FastAPI services** with a **negligible compute footprint** (a few
  small CPU replicas) — they orchestrate and query, they don't run models. Their
  cost is the **LLM-B/VLM calls** they make, which land on the **same Stage-2
  backend** you already sized (and are gated/budget-capped, so the slice stays
  small). No extra GPU pool is needed for the agent layer.

---

## 2. GPU requirements by stage

### MVP — 1,000 posts/batch

> **Backend choice changes the GPU need.** On the **`local`** backend you size a
> GPU for the LLM (below). On the **`groq`** backend the Stage-2 LLM runs on Groq
> — **no LLM GPU at all**, so the MVP can run NLP on a small GPU or even CPU-only
> and call Groq for summaries. This is the cheapest way to stand up an MVP without
> owning a GPU; switch to `local` later for cost/privacy as volume grows.

- **`local` backend:** **1 × consumer GPU** (e.g. RTX 4090 / 3090, 24 GB) runs the
  whole show: the NLP suite (quantized) + a **vision model** (SigLIP/CLIP for image
  sentiment — tiny) + the local LLMs/VLM via vLLM, time-sliced. At MVP scale run
  just **LLM-A** (quantized 7B) plus a small **VLM** (`Qwen2.5-VL-3B/7B`, quantized)
  for image-grounded summaries; add **LLM-B** when cluster/report quality demands
  it. The 7B VLM fits alongside a 7B LLM-A on 24 GB when quantized and time-sliced;
  on the **`groq`** backend the VLM is just a hosted vision model id — no extra GPU.
- CPU-only is even possible for the smallest NLP models if no GPU is available,
  at lower throughput (pair with the `groq` backend to skip GPUs entirely).
- **Recommendation:** single workstation/server with one 24 GB consumer GPU, or a
  single cloud GPU instance — or, with the `groq` backend, no GPU. Docker Compose.

### Production — 10,000 posts/batch

- **NLP fleet:** 1–2 GPUs (24 GB consumer or 1× data-center L4/A10, 24 GB) with
  Triton dynamic batching, autoscaled by queue depth.
- **LLMs (`local` backend):** **LLM-A** (quantized 7B) on an L4/A10 + **LLM-B**
  (quantized 14B/32B) on a data-center GPU (**L40S / A100 40GB**); at lower volume
  LLM-A can share the NLP GPU. Continuous batching keeps utilization high.
- **LLMs (`groq` backend):** none of the above — no LLM GPUs to provision. The
  Stage-2 worker pool becomes CPU-only and scales on queue depth; mind Groq
  rate-limits/quotas as the throughput ceiling instead of GPU count.
- **Recommendation:** small Kubernetes cluster — 2–4 GPUs on `local`, or just the
  NLP GPU(s) on `groq` — KEDA autoscaling.

### Enterprise — 100,000 posts/batch

- **NLP fleet:** several data-center GPUs (**A10 / L4 / L40S**), autoscaled;
  horizontal first.
- **LLMs (`local` backend):** multiple **A100/H100** (or several L40S) behind vLLM
  serving both models, multi-replica with tensor parallelism for LLM-B; scale out
  by adding GPU replicas for spikes.
- **LLMs (`groq` backend):** no LLM GPU pool; scale the stateless Stage-2 workers
  and negotiate a Groq rate-limit/quota that covers peak. A **hybrid** is common at
  this scale — own GPUs for the steady base, burst overflow to Groq during spikes
  instead of over-provisioning GPUs.
- **Recommendation:** multi-node K8s, GPU node pools per stage (local) and/or a
  Groq burst path, aggressive caching + clustering to hold down the LLM slice.

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
  utilization (DCGM exporter, `local` backend), LLM-routing rate, cache hit rate,
  error rate, p50/p95/p99 latency, DLQ size. **Per-backend LLM metrics:** calls
  and latency split by `backend` (local/groq); on `groq` also **tokens
  in/out + estimated cost**, HTTP 429 rate-limit hits, and Groq API error rate;
  on `local` also vLLM queue/KV-cache utilization. A **backend label** on every
  LLM metric makes a runtime switch visible on the dashboards.
- **Grafana** — dashboards (pipeline throughput, cost-per-batch, LLM slice %) +
  alerting (Alertmanager): queue lag > threshold, DLQ growth, GPU saturation,
  error-rate spikes.
- **Loki** — centralized structured logs, correlated to traces by trace_id.
- **OpenTelemetry** — single instrumentation standard across all services;
  exports metrics→Prometheus, logs→Loki, traces→Jaeger.
- **Jaeger** — distributed traces follow a post from ingestion → NLP → router →
  (LLM) → assembler → store; essential for debugging latency and the router.

**Key product metrics to watch:** % of posts that hit the LLM, cache hit rates
(dedup/embedding/LLM), cost per 1k posts (GPU-amortized on `local`, token-billed
on `groq`), the active **LLM backend mix** (local vs groq share), and per-language
accuracy drift.

---

## 4. Caching strategy (summary)

Detailed rationale in [possible_architecture.md](possible_architecture.md) §6.

| Cache                          | Key                                    | Purpose                                                        | TTL                  |
| ------------------------------ | -------------------------------------- | -------------------------------------------------------------- | -------------------- |
| **Dedup set** (Redis)          | `content_hash`                         | Skip re-analysis of exact dupes/reshares                       | long / per-retention |
| **Embedding cache** (Redis)    | `content_hash`                         | Avoid recomputing vectors                                      | long                 |
| **LLM response cache** (Redis) | `(backend, model, task, content_hash)` | Free repeats of LLM calls                                      | medium–long          |
| **Query cache** (Redis)        | normalized query                       | Fast dashboard aggregations                                    | short (secs–mins)    |
| **Usage counters** (Redis)     | `usage:{tokens,calls}:…`               | The only source `GET /v1/usage` reads — tokens/calls per backend+model, per lane, per task | no TTL (reset on wipe) |
| **CDN**                        | URL                                    | Static **HTML/CSS/JS** dashboard assets, static report exports | long, versioned      |

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
- **Auth/tenancy state lives in Postgres**, not in memory: `api_keys` (hashed,
  tenant-scoped), `users` (PBKDF2), `tenant_policies` (`privacy_locked`). SSE
  tickets live in Redis with a ~60s TTL. Nothing about a principal is trusted
  from the client, which is what makes per-tenant isolation enforceable.
- **Stateless workers.** All state in queue + stores → scale to zero between
  batches, scale out instantly for a 10k/100k burst.
- **Spot for batch.** Use preemptible/spot GPU nodes for batch surges; Kafka
  replay makes interruptions safe.
- **Backpressure.** Bounded queues + DLQ prevent overload cascades.
- **DB scaling.** PostgreSQL read replicas (the pgvector `analysis_results.embedding`
  index rides along on the replicas for semantic-search fan-out); ClickHouse
  shards/replicas.

See [deployment.md](deployment.md) for the concrete Kubernetes/KEDA setup and
[plan.md](plan.md) for the order to build it in.

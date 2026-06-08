# Cost Estimation

Monthly cost estimates for MVP / Production / Enterprise across compute,
storage, networking, and LLM — sizing the "cost-effective, no paid API"
requirement from [what.txt](what.txt).

> **These are planning-grade order-of-magnitude estimates, not quotes.** Cloud
> GPU prices change frequently and vary by region, commitment (on-demand vs
> reserved vs spot), and provider. Treat the _ratios and the dominant cost
> drivers_ as the durable insight; re-price against live vendor pages before
> committing budget. **There is no LLM API line at all** — both LLMs are
> self-hosted (see [models.md](models.md) §2), so cost is GPU + storage +
> networking only, with no per-token charges. The hybrid design's whole point is
> to keep the GPU-bound LLM slice small.

Assumptions used below: a "batch" is the per-run size; we assume **continuous
operation** processing many batches/day. A unit is a **post + its comment thread**
(so one unit may be tens–hundreds of short texts). The headline number that
matters is **cost per 1,000 threads analyzed**, which the hybrid pipeline drives
down by keeping the LLM to one cluster-level summary per thread, not one call per
comment.

---

## 1. What drives cost

| Driver      | Without hybrid (LLM-per-post)                 | With hybrid (this design)                                             |
| ----------- | --------------------------------------------- | --------------------------------------------------------------------- |
| LLM tokens  | **Dominant, runaway** — every post = LLM call | Small — only the selective slice + cluster-level calls                |
| GPU compute | Moderate                                      | **Now the main line**, but cheap & predictable (batched small models) |
| Storage     | Small                                         | Small                                                                 |
| Networking  | Small–moderate                                | Small–moderate                                                        |

The hybrid architecture, plus self-hosting both LLMs locally, converts an
unbounded per-token LLM bill into a **bounded, mostly-fixed GPU bill** — there is
no per-token API charge to grow with volume.

---

## 2. MVP — ~1,000 posts/batch

Single host, one 24 GB consumer GPU, Docker Compose. Self-hosted everything.

| Line                                                                 | Estimate (USD/mo)   | Notes                                                                           |
| -------------------------------------------------------------------- | ------------------- | ------------------------------------------------------------------------------- |
| Compute (1 GPU host, on-prem amortized **or** 1 cloud GPU part-time) | $150 – $700         | On-prem 4090 amortized at low end; cloud L4/A10 on-demand part-time at high end |
| Storage (Postgres + ClickHouse + Qdrant + object, modest)            | $10 – $40           | Tens of GB                                                                      |
| Networking                                                           | $5 – $30            | Mostly egress for dashboard/API                                                 |
| LLM (both local models, time-sliced on the same GPU)                 | ~$0 incremental     | LLM-A + LLM-B share the GPU; selective + cached                                 |
| Monitoring (self-hosted Prometheus/Grafana/Loki)                     | ~$0 – $20           | Runs on the same box                                                            |
| **Total**                                                            | **~$170 – $800/mo** | Dominated by the single GPU                                                     |

At MVP scale both local LLMs **time-slice the one GPU** (and LLM-B can be dropped
entirely, using LLM-A for all Stage-2 work, until volume justifies the second
model). There is no API line to add — capacity grows by adding GPUs, not by
sending data off-box.

---

## 3. Production — ~10,000 posts/batch

Small K8s cluster, 2–4 GPUs (mix of consumer/L4/A10 + one L40S/A100 for the LLM),
KEDA autoscaling, spot for batch surges.

| Line                                                       | Estimate (USD/mo)       | Notes                               |
| ---------------------------------------------------------- | ----------------------- | ----------------------------------- |
| Compute — NLP fleet (1–2 GPUs, autoscaled, partly spot)    | $400 – $1,500           | Scales with daily volume            |
| Compute — LLM GPUs (LLM-A on L4/A10 + LLM-B on L40S/A100)  | $900 – $3,200           | Both local; reserved/spot lower     |
| Compute — CPU services (API, ingestion, assembler, DBs)    | $200 – $600             | Several small nodes                 |
| Storage (Postgres + ClickHouse + Qdrant + object, growing) | $50 – $250              | Hundreds of GB → TB                 |
| Networking / egress                                        | $50 – $300              | Dashboard, exports, inter-AZ        |
| Monitoring                                                 | $30 – $150              | Self-hosted or small managed        |
| **Total**                                                  | **~$1,600 – $6,000/mo** | LLM + NLP GPUs dominate; no API     |

**Cost-per-1k-posts** falls sharply here vs. MVP because batched GPUs are
well-utilized and the LLM slice stays small.

---

## 4. Enterprise — ~100,000 posts/batch

Multi-node K8s, GPU node pools per stage, several A100/H100 or many L40S,
aggressive caching + clustering, spot for batch.

| Line                                                                   | Estimate (USD/mo)         | Notes                                                   |
| ---------------------------------------------------------------------- | ------------------------- | ------------------------------------------------------- |
| Compute — NLP fleet (several data-center GPUs, autoscaled, spot-heavy) | $3,000 – $12,000          | Horizontal scale; spot saves 50–70%                     |
| Compute — LLM-A + LLM-B (multiple A100/H100 or many L40S, vLLM)        | $5,000 – $25,000          | Biggest line; both local; reserved/spot critical        |
| Compute — CPU services + DB nodes                                      | $1,000 – $4,000           | HA Postgres, ClickHouse cluster, Qdrant shards          |
| Storage (TBs across stores + object + backups)                         | $300 – $2,000             | Grows with retention                                    |
| Networking / egress                                                    | $300 – $2,000             | Significant at this scale                               |
| Monitoring / observability                                             | $150 – $600               |                                                         |
| **Total**                                                              | **~$10,000 – $48,000/mo** | LLM GPUs dominate; caching/clustering decide the spread |

At this scale the **single biggest cost lever is keeping the LLM slice small** —
every percentage point of posts that _doesn't_ need the LLM, and every cache hit,
directly cuts the largest line. Reserved/committed-use GPU pricing and spot for
batch are the next biggest levers.

---

## 5. Levers ranked by impact

1. **Hybrid routing** — keep the LLM slice in the single digits %. Biggest lever.
2. **Caching + dedup** — social feeds are repetitive; cache hits are free results.
3. **Cluster-level LLM** — summarize clusters, not individual posts.
4. **Quantization + batching** — maximize GPU utilization (vLLM/Triton).
5. **Spot/reserved GPUs** — batch tolerates preemption (Kafka replay); reserve
   the steady base.
6. **Both LLMs local, no API** — zero per-token pricing and no data egress; scale
   by adding GPUs. Right-size: cheap LLM-A for per-post, larger LLM-B only for
   low-volume cluster/report work.

See [architecture.md](architecture.md) §5 and [models.md](models.md) for how
these are implemented, and [infrastructure.md](infrastructure.md) for the GPU
classes priced above.

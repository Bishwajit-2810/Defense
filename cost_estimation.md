# Cost Estimation

Monthly cost estimates for MVP / Production / Enterprise across compute,
storage, networking, and LLM — sizing the "cost-effective" requirement from
[what.txt](what.txt). The Stage-2 LLM cost depends on the **selected backend**
(see [models.md](models.md) §2): `local` (self-hosted vLLM) is a **GPU line, no
per-token charge**; `groq` (Groq Cloud API) is a **per-token API line, no LLM
GPU**. Both are priced below.

> **These are planning-grade order-of-magnitude estimates, not quotes.** Cloud
> GPU prices change frequently and vary by region, commitment (on-demand vs
> reserved vs spot), and provider. Treat the _ratios and the dominant cost
> drivers_ as the durable insight; re-price against live vendor pages before
> committing budget. **The LLM cost shape depends on the backend** (see
> [models.md](models.md) §2): on the **`local`** backend there is no API line —
> cost is GPU + storage + networking, no per-token charge; on the **`groq`**
> backend the LLM becomes a **per-token API line** but the LLM GPU line disappears.
> Either way the hybrid design's whole point is to keep the LLM slice small — that
> caps the GPU bill _and_ the token bill.

Assumptions used below: a "batch" is the per-run size; we assume **continuous
operation** processing many batches/day. A unit is a **post + its comment thread**
(so one unit may be tens–hundreds of short texts). The headline number that
matters is **cost per 1,000 threads analyzed**, which the hybrid pipeline drives
down by keeping the LLM to one cluster-level summary per thread, not one call per
comment.

---

## 1. What drives cost

| Driver           | Without hybrid (LLM-per-post)                 | With hybrid (this design)                                             |
| ---------------- | --------------------------------------------- | --------------------------------------------------------------------- |
| LLM tokens       | **Dominant, runaway** — every post = LLM call | Small — only the selective slice + cluster-level calls                |
| LLM GPU (local)  | Moderate                                      | Fixed GPU line (only on the `local` backend)                          |
| LLM API (groq)   | **Dominant, runaway**                         | Small per-token line (only on the `groq` backend)                     |
| NLP GPU compute  | Moderate                                      | **The main fixed line**, cheap & predictable (batched small models)   |
| Storage          | Small                                         | Small                                                                 |
| Networking       | Small–moderate                                | Small–moderate (+ egress to Groq on the `groq` backend)               |

The hybrid architecture keeps the LLM slice tiny, which caps cost under either
backend: on `local` it converts an unbounded per-token bill into a **bounded,
mostly-fixed GPU bill**; on `groq` it keeps the **per-token bill small and
proportional to the few calls that actually reach the LLM**, with no LLM GPU to
own. The operator can switch backends as volume, GPU availability, and data policy
change.

---

## 2. MVP — ~1,000 posts/batch

Single host, one 24 GB consumer GPU, Docker Compose. Self-hosted everything.

| Line                                                                 | Estimate (USD/mo)   | Notes                                                                           |
| -------------------------------------------------------------------- | ------------------- | ------------------------------------------------------------------------------- |
| Compute (1 GPU host, on-prem amortized **or** 1 cloud GPU part-time) | $150 – $700         | On-prem 4090 amortized at low end; cloud L4/A10 on-demand part-time at high end |
| Storage (Postgres + ClickHouse + Qdrant + object, modest)            | $10 – $40           | Tens of GB                                                                      |
| Networking                                                           | $5 – $30            | Mostly egress for dashboard/API                                                 |
| LLM — **`local`** (both roles time-sliced on the same GPU)           | ~$0 incremental     | LLM-A + LLM-B share the GPU; selective + cached                                 |
| LLM — **`groq`** alternative (per-token, ~1k batches)                | ~$5 – $50           | Replaces the LLM GPU; only the selective slice is billed; tiny at MVP volume    |
| Monitoring (self-hosted Prometheus/Grafana/Loki)                     | ~$0 – $20           | Runs on the same box                                                            |
| **Total**                                                            | **~$170 – $800/mo** | Dominated by the single GPU                                                     |

Two ways to run Stage-2 at MVP scale:

- **`local` (default):** both roles **time-slice the one GPU** (LLM-B can be
  dropped, using LLM-A for everything, until volume justifies it). No API line;
  capacity grows by adding GPUs.
- **`groq`:** drop the LLM from the GPU entirely and call Groq per token. At ~1k
  batches the selective slice is tiny, so the token bill is a few dollars — and
  you may not need a GPU at all if the NLP fleet runs CPU-only. Trade: prompt data
  leaves the box. Switchable at runtime, so you can start on Groq and move to
  `local` as volume grows (or vice-versa).

---

## 3. Production — ~10,000 posts/batch

Small K8s cluster, 2–4 GPUs (mix of consumer/L4/A10 + one L40S/A100 for the LLM),
KEDA autoscaling, spot for batch surges.

| Line                                                                | Estimate (USD/mo)       | Notes                                                      |
| ------------------------------------------------------------------- | ----------------------- | ---------------------------------------------------------- |
| Compute — NLP fleet (1–2 GPUs, autoscaled, partly spot)             | $400 – $1,500           | Scales with daily volume                                   |
| Compute — Stage-2 LLM, **`local`** (LLM-A L4/A10 + LLM-B L40S/A100) | $900 – $3,200           | GPU line; reserved/spot lower                              |
| — **or** Stage-2 LLM, **`groq`** (per-token, ~10k batches)          | $150 – $1,500           | Replaces the LLM GPU line; scales with the selective slice |
| Compute — CPU services (API, ingestion, assembler, DBs)             | $200 – $600             | Several small nodes                                        |
| Storage (Postgres + ClickHouse + Qdrant + object, growing)          | $50 – $250              | Hundreds of GB → TB                                        |
| Networking / egress                                                 | $50 – $300              | Dashboard, exports, inter-AZ (+Groq egress on `groq`)      |
| Monitoring                                                          | $30 – $150              | Self-hosted or small managed                               |
| **Total**                                                           | **~$1,600 – $6,000/mo** | LLM + NLP GPUs dominate on `local`                         |

**Cost-per-1k-posts** falls sharply here vs. MVP because batched GPUs are
well-utilized and the LLM slice stays small. Pick the LLM line by backend — own
the LLM GPUs (`local`) for steady volume, or pay per token (`groq`) to avoid
running and reserving LLM GPUs; the two are interchangeable, so you can move
between them as utilization and burst patterns dictate.

---

## 4. Enterprise — ~100,000 posts/batch

Multi-node K8s, GPU node pools per stage, several A100/H100 or many L40S,
aggressive caching + clustering, spot for batch.

| Line                                                                   | Estimate (USD/mo)         | Notes                                                   |
| ---------------------------------------------------------------------- | ------------------------- | ------------------------------------------------------- |
| Compute — NLP fleet (several data-center GPUs, autoscaled, spot-heavy) | $3,000 – $12,000          | Horizontal scale; spot saves 50–70%                     |
| Compute — Stage-2 LLM `local` (A100/H100 or many L40S, vLLM)           | $5,000 – $25,000          | Biggest line on `local`; reserved/spot critical         |
| — **or** Stage-2 LLM `groq` (per-token, ~100k batches)                 | $1,500 – $15,000          | No LLM GPUs; cost tracks the selective slice + caching  |
| Compute — CPU services + DB nodes                                      | $1,000 – $4,000           | HA Postgres, ClickHouse cluster, Qdrant shards          |
| Storage (TBs across stores + object + backups)                         | $300 – $2,000             | Grows with retention                                    |
| Networking / egress                                                    | $300 – $2,000             | Significant at this scale                               |
| Monitoring / observability                                             | $150 – $600               |                                                         |
| **Total**                                                              | **~$10,000 – $48,000/mo** | LLM GPUs dominate; caching/clustering decide the spread |

At this scale the **single biggest cost lever is keeping the LLM slice small** —
every percentage point of posts that _doesn't_ need the LLM, and every cache hit,
directly cuts the largest line under **either** backend (it shrinks both the GPU
line on `local` and the token line on `groq`). On `local`, reserved/committed-use
GPU pricing and spot for batch are the next biggest levers; on `groq`, batching,
caching, and capping the per-call token budget are. A common enterprise pattern is
**hybrid**: own LLM GPUs for the steady base (`local`) and burst overflow to
`groq` during spikes instead of over-provisioning GPUs.

---

## 5. Levers ranked by impact

1. **Hybrid routing** — keep the LLM slice in the single digits %. Biggest lever.
2. **Caching + dedup** — social feeds are repetitive; cache hits are free results.
3. **Cluster-level LLM** — summarize clusters, not individual posts.
4. **Quantization + batching** — maximize GPU utilization (vLLM/Triton).
5. **Spot/reserved GPUs** — batch tolerates preemption (Kafka replay); reserve
   the steady base.
6. **Right-size the backend.** `local` (vLLM) — zero per-token pricing, no data
   egress, scale by adding GPUs; best for steady volume. `groq` — no LLM GPUs to
   own or reserve, pay only for the selective slice; best for bursty/low volume or
   no-GPU MVPs. Either way, right-size the roles: cheap LLM-A for per-post, larger
   LLM-B only for low-volume cluster/report work. The backend is switchable at
   runtime, so re-evaluate it as volume and GPU prices change.

See [architecture.md](architecture.md) §5 and [models.md](models.md) for how
these are implemented, and [infrastructure.md](infrastructure.md) for the GPU
classes priced above.

# Cost Estimation

Monthly cost estimates for MVP / Production / Enterprise across compute,
storage, networking, and LLM — sizing the "cost-effective" requirement from
[architecture.md](architecture.md). The Stage-2 LLM cost depends on the **selected backend**
(see [models.md](models.md) §2): `local` (self-hosted vLLM) is a **GPU line, no
per-token charge**; `groq` (Groq Cloud API) is a **per-token API line, no LLM
GPU**. Both are priced below.

> **Implementation companions:** the gate that decides post-level spend and how to
> read its rate is [ROUTER.md](ROUTER.md); the comment lane that dominates the bill
> is [STAGE2_LLM.md](STAGE2_LLM.md); the counters that make any of this checkable —
> five lanes, per backend and model, with `pipeline_tokens` isolating the per-post
> figure — are [LLM_BACKENDS.md](LLM_BACKENDS.md) §4.


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
matters is **cost per 1,000 threads analyzed**.

> ## ⚠ The cost shape changed — measured 4 August 2026
>
> This document was written assuming **the LLM slice is a small, post-level
> fraction**, and that the router gate is the dominant lever. Both assumptions
> are now out of date, and the numbers below are stale accordingly.
>
> **What actually happens** on the 43-post working corpus, cold cache
> (`python -m eval.measure_routing_rate`, PROJECT_ASSESSMENT §6.8):
>
> | | Keyword-stub Stage 1 | Stage-1 LLM (shipped) |
> | --- | --- | --- |
> | Posts routed to Stage 2 | 74% | **16%** |
> | Post-level LLM calls | 124 (15%) | 22 (4%) |
> | **Comment-level LLM calls** | **689 (85%)** | **507 (96%)** |
> | Total calls per corpus run | 813 | 529 |
> | Share the router gate governs | 55% | **30%** |
>
> Three corrections follow, and they invert the framing of §1 below:
>
> 1. **The LLM bill is comment-dominated, not post-dominated.** Since every
>    non-emoji comment is labelled by an LLM (PROJECT_ASSESSMENT §6.3 — the
>    previous 60/40 caps meant only ~29% of comments were ever labelled while the
>    output described itself as full coverage), the per-thread cost scales with
>    **comments per thread**, not with posts. A 2,857-comment thread costs ~115
>    LLM calls on its own.
> 2. **The routing gate is no longer the "biggest lever."** It governs 30–55% of
>    calls, and *the better Stage 1 gets the less it governs* — Stage-1 comment
>    labelling runs for every post, routed or not. The biggest lever is now
>    **comments per thread** and the batch size (`STAGE1_LLM_BATCH`), not the
>    confidence threshold.
> 3. **"Single digits %" was never measured and is not achievable as stated.**
>    See §5 below.
>
> **Sizing rule of thumb that replaces the old one:** per thread, budget
> `ceil(non_emoji_comments / 25)` Stage-1 calls, plus
> `ceil(min(non_emoji_comments, ROUTER_COMMENT_TOP_N) / 25)` for the Stage-2
> comment pass, plus ~3 post-level calls if the post routes. **All three coverage
> knobs default to 0 (no cap)**, so budget the full thread: a 2,857-comment post is
> ~115 Stage-2 stance batches *and* ~44 min of classifier CPU at 7 heads. Setting
> `ROUTER_COMMENT_TOP_N=100` bounds the Stage-2 term at **4 calls per post**
> however large the thread — the single biggest lever in this document, and the one
> to reach for first if a run is too slow. Full comment coverage is a *chosen*
> trade; these knobs are how you un-choose it.
>
> **Per-backend pricing is now real.** `GET /v1/usage` reports
> `tokens_by_backend_model` and `cost_by_backend_model`, with **local priced at
> 0.0** (its marginal token cost genuinely is zero) and Groq priced per model.
> One blended `$0.002/1k` rate used to be applied to every token, which was wrong
> for both backends in opposite directions. The dollar figures below still use
> the old blended assumption and should be re-derived from a real run
> (PROJECT_ASSESSMENT §9.8).
>
> **Quote `pipeline_tokens`, not `total_tokens`, for a per-post figure.** The
> counters are written by `LLMClient` itself, so they now cover **every** LLM
> caller, and `lane_split` reports five lanes:
>
> | Lane | What it is | Scales with |
> | ---- | ---------- | ----------- |
> | `post` | summary / post_type / insight | posts routed to Stage 2 |
> | `comment` | comment stance, thread summary | comments per post |
> | `stage1` | Stage-1's own LLM path (`STAGE1_LLM=true`) | posts **and** comments |
> | `interactive` | `/v1/chat`, report narratives, cluster summaries | *questions asked*, not corpus size |
> | `agent` | agent tool-calling turns | *questions asked* |
>
> `pipeline_tokens` sums the first three. The last two are per-question costs with
> no relation to how many posts you analysed, so folding them into a per-post
> figure inflates it by however much anyone used the chatbot. Until
> PROJECT_ASSESSMENT §13.4 only the Stage-2 worker incremented anything, so the
> `stage1`, `interactive` and `agent` lanes spent tokens nothing counted at all —
> and in the shipped `STAGE1_LLM=true` configuration that is the majority of
> calls (§6.8 measured comment-level work at 96% of the total there).

---

## 1. What drives cost

| Driver          | Without hybrid (LLM-per-post)                 | With hybrid (this design)                                                                                                                                                                                                     |
| --------------- | --------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| LLM tokens      | **Dominant, runaway** — every post = LLM call | Small — only the selective slice + cluster-level calls                                                                                                                                                                        |
| LLM GPU (local) | Moderate                                      | Fixed GPU line (only on the `local` backend)                                                                                                                                                                                  |
| LLM API (groq)  | **Dominant, runaway**                         | Small per-token line (only on the `groq` backend)                                                                                                                                                                             |
| NLP GPU compute | Moderate                                      | **The main fixed line**, cheap & predictable (batched small models)                                                                                                                                                           |
| Comment-ensemble CPU | n/a | Small fixed line — the **seven Stage-2 sentiment heads** (135–280M encoders) run batched on **CPU** by design (`STAGE2_CLASSIFIER_DEVICE=cpu`): no per-token bill, no GPU contention with the LLM, ~3 GB of checkpoint disk. **They do scale with thread size** at the shipped `ROUTER_COMMENT_TOP_N=0`: measured ~0.92 s/comment across the seven heads, so a 2,857-comment post is ~44 min of CPU. Setting a positive cap makes this per-post cost constant instead. They buy the agreement signal that decides where the *expensive* voter is needed |
| Vision compute  | n/a                                           | Small fixed line — cheap **image-sentiment** (SigLIP/CLIP) **+ our OCR** (PaddleOCR/Tesseract) on image posts; the **VLM** summary runs only on the selective slice (GPU on `local`, per-token vision calls on `groq`)        |
| Agents + MCP    | n/a                                           | Tiny — stateless FastAPI services (a few CPU replicas); their only real cost is **low-volume LLM-B calls** for reports/analyst Q&A, gated + budget-capped, billed under the LLM line ([architecture.md](architecture.md) §11) |
| Storage         | Small                                         | Small — three stores (Postgres + pgvector, ClickHouse, object), the vector index living inside Postgres rather than a separate service                                                                                       |
| Networking      | Small–moderate                                | Small–moderate (+ egress to Groq on the `groq` backend)                                                                                                                                                                       |

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
| Storage (Postgres + pgvector + ClickHouse + object, modest)          | $10 – $40           | Tens of GB; vectors live in Postgres via pgvector (no separate store)           |
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
| Compute — CPU services (API, ingestion, assembler, DBs)             | $200 – $600             | Several small nodes; Postgres carries the pgvector index (some extra RAM/CPU) |
| Storage (Postgres + pgvector + ClickHouse + object, growing)        | $50 – $250              | Hundreds of GB → TB; vectors stored in Postgres via pgvector |
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
| Compute — CPU services + DB nodes                                      | $1,000 – $4,000           | HA Postgres (carries the pgvector index — extra RAM/CPU), ClickHouse cluster |
| Storage (TBs across stores + object + backups)                         | $300 – $2,000             | Grows with retention; vector storage folded into Postgres via pgvector |
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

**Re-ranked 4 August 2026 against the measured split.** The old ranking put
"hybrid routing, keep the LLM slice in the single digits %" first. That was
wrong in two ways: single digits was never measured (the shipped rate is **16%**,
and the gate spent a period routing **100%** — PROJECT_ASSESSMENT §4), and the
gate governs only 30–55% of calls because comment labelling runs for every post.

1. **Comment volume per thread.** The dominant driver: 85–96% of LLM calls are
   comment-level, and a thread's cost is `ceil(non_emoji_comments / batch)`.
   Levers, in order of bluntness:
   - `ROUTER_COMMENT_TOP_N` (default **0 = no cap**): set it to 100 and the router
     hands Stage 2 only the 100 most-reacted comments with text, so a
     2,857-comment thread costs `ceil(100/25) = 4` stance calls instead of 115 —
     and ~1.5 min of classifier CPU instead of ~44. It caps every Stage-2 voter at
     once, which is the point: a cap on the LLM alone saves the tokens and still
     runs seven models over the whole thread. Stage-1 comment labelling is *not*
     capped by it, so Stage-1 coverage is unchanged; what shrinks is the ensemble,
     and `ensemble.not_analysed` reports by how much.
   - `STAGE1_LLM_COMMENT_MAX` / `COMMENT_STANCE_MAX_PER_POST` (default **0** =
     every comment). Setting these caps the bill directly — but it also caps
     coverage, so the caps and the coverage claim must be quoted together.
   - `STAGE1_LLM_BATCH` (default 25): more comments per call is fewer calls, at
     the cost of a longer prompt and a coarser retry granularity.
   - Emoji filtering — real but small: **2.8%** of comments, not the ~17% an
     earlier estimate implied.
2. **Caching + dedup** — social feeds are repetitive; cache hits are free
   results. The Stage-2 response cache now keys on the **resolved model id**, so
   changing a model correctly misses instead of silently serving the old model's
   answers (PROJECT_ASSESSMENT §5.10). `LLM_CACHE_DISABLED=1` bypasses it for
   evaluation runs.
3. **Hybrid routing (the confidence gate)** — still a real lever, governing
   30–55% of calls, and still the one with an accuracy trade-off worth plotting
   (§9 P1.5's threshold sweep). But note the counter-intuitive part: *the better
   Stage 1 gets, the smaller this lever becomes*, because fewer posts route while
   comment labelling is unchanged.
4. **Model choice per role.** Summarization and classification now resolve to
   separate roles (`summary` vs `stage2`), so the fluent, expensive model is
   spent once per post instead of on every classification call. Compare
   candidates with `python -m eval.bakeoff_summary`.
5. **Cluster-level LLM** — summarize clusters, not individual posts. Note that
   the clustering currently runs on **stub embeddings** by default
   (PROJECT_ASSESSMENT §5.9), so this lever is not yet real.
6. **Quantization + batching** — maximize GPU utilization (vLLM/Triton).
7. **Spot/reserved GPUs** — batch tolerates preemption (Kafka replay); reserve
   the steady base.
8. **Right-size the backend.** `local` (vLLM) — zero per-token pricing, no data
   egress, scale by adding GPUs; best for steady volume. `groq` — no LLM GPUs to
   own or reserve, pay only for the selective slice; best for bursty/low volume or
   no-GPU MVPs. Either way, right-size the roles: cheap LLM-A for per-post, larger
   LLM-B only for low-volume cluster/report work. The backend is switchable at
   runtime, so re-evaluate it as volume and GPU prices change.

See [architecture.md](architecture.md) §5 and [models.md](models.md) for how
these are implemented, and [infrastructure.md](infrastructure.md) for the GPU
classes priced above.

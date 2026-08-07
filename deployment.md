# Deployment — Docker Compose vs Kubernetes, and the K8s Plan

How the smart layer from [what.txt](what.txt) is deployed: the MVP shape, the
production shape, and the full Kubernetes plan that delivers the "proper scaling"
the owner asked for.

---

## 1. Docker Compose vs Kubernetes

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

**Recommendation:** Docker Compose for the MVP (get to working software fast),
Kubernetes + KEDA for Production and Enterprise.

---

## 2. MVP architecture (Docker Compose)

Single host with one GPU. One `docker-compose.yml` brings up:

```text
services:
  gateway        (NGINX)            → TLS, routing, rate limit
  api            (FastAPI)          → auth + ingestion + reporting (combined for MVP)
  worker-nlp     (Python)           → Stage-1 text suite + vision (image sentiment, OCR, GPU)
  worker-llm     (Python)           → Stage-2 worker (text LLM + VLM); LLM_BACKEND=local|groq
  vllm           (vLLM, optional)   → local backend only: LLM-A (+LLM-B) + VLM (Qwen2.5-VL) on GPU
  agent-orch     (FastAPI)          → AI agents (insight/analyst, deep-dive, alerting) → LLM-B + MCP   [Phase 2]
  mcp-servers    (FastAPI + MCP)    → analytics-mcp · retrieval-mcp · ingest-mcp (internal tools)        [Phase 2]
  redis          (cache/queue)      → Redis Streams = bus + cache + dedup
  postgres       (ops + jobs + vectors via pgvector)
  clickhouse     (analytics)
  minio          (object storage)
  prometheus + grafana + loki       → monitoring
```

- Queue = **Redis Streams** (no separate Kafka to operate yet).
- API services can be one process for the MVP; split later.
- **Stage-2 backend is a config switch** on `worker-llm`:
  - `LLM_BACKEND=local` → it talks to the `vllm` service; GPU is shared between
    NLP and the local LLM(s) via time-slicing (run LLM-A only at MVP scale).
  - `LLM_BACKEND=groq` → drop the `vllm` service entirely and set
    `GROQ_API_KEY` + role→model IDs; the worker calls Groq over HTTPS and needs
    **no GPU**. Flip the env var to switch at any time (no image rebuild).
- Goal: prove the hybrid pipeline end-to-end on 1k post+comment-thread batches —
  the post-with-details payload (comments embedded) pulled into our own DB, structured
  JSON out (see [examples.md](examples.md), [data_contract.md](data_contract.md)).

---

## 2b. What actually ships in this repo — and where each piece runs

§2 above is the target shape. What is checked in today differs in two ways worth
knowing before you run anything: there is **no `gateway`/NGINX service and no
`vllm` service**, and **Ollama is not containerised at all**. There are two run
modes, and the documented one is a hybrid.

### Mode A — `uv run run_all.py` (the local dev path, and the one everything is tested against)

| In Docker (4 containers) | On your host |
| --- | --- |
| `postgres` (pgvector), `redis`, `clickhouse`, `minio` | the 5 workers + the API (uvicorn, `127.0.0.1:8001`) |
| | the dashboard static server (`:8080`) |
| | with `--with-agents`: analytics-mcp `:8110`, retrieval `:8101`, ingest `:8102`, agents `:8010` |
| | **Ollama (`:11434`)** — you install it and pull the models yourself |

`run_all.py` starts exactly `["postgres", "redis", "clickhouse", "minio"]` in
Docker; everything else is a host subprocess run with `.venv/bin/python`. So
Docker alone is **not** enough — you also need `uv`, Ollama, and the three pulled
models (`gemma3:4b`, `qwen2.5:7b`, `qwen3-vl:4b`).

### Mode B — `cd deploy && docker compose up` (everything containerised)

The compose file does define all ten app services — api, ingestion, the four
workers, the three MCP servers and agents — each with a real Dockerfile, plus
observability (`prometheus`, `grafana`, `jaeger`, `loki`, `promtail`) that Mode A
never starts. `deploy/.env` is present, so `env_file` resolves.

Two things still reach outside the compose network:

* **Ollama.** Containers reach the *host's* Ollama via
  `host.docker.internal:host-gateway` (`LOCAL_LLM_BASE_URL` in `deploy/.env`), so
  even full-Docker needs it running on the host — or `LLM_BACKEND=groq` with a
  real key, since `.env` ships the `your_groq_api_key_here` placeholder.
* **The API port differs**: `:8000` in compose, `:8001` under `run_all.py`. The
  dashboard targets the dev API on `:8001`.

> **Status honestly stated:** Mode B's app-service images are **not** what any of
> this project's verification runs used — every measurement, probe and test in
> [PROJECT_ASSESSMENT.md](PROJECT_ASSESSMENT.md) was taken under Mode A or against
> throwaway containers. The compose config is complete and the Dockerfiles exist,
> but `docker compose build` has not been exercised as part of the assessment, and
> the Kubernetes manifests have never run in a cluster (§5.5). Treat Mode B as
> configured-but-unexercised until someone builds it.

---

## 3. Production architecture (Kubernetes)

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
   ┌──────▼──────────────┬───────────┬──────────▼┐
   │ postgres + pgvector │clickhouse │ minio/S3  │  StatefulSets / managed
   │ (HA, replica;       │(cluster)  │           │
   │  vectors)           │           │           │
   └─────────────────────┴───────────┴───────────┘
        observability namespace: prometheus, grafana, loki, jaeger, otel-collector
```

---

## 4. Kubernetes deployment plan

### Namespaces

`platform` (services + workers), `data` (DBs, queue), `models` (Triton, vLLM),
`observability` (monitoring), `ingress`.

### Workload mapping

- **Stateless services** (auth, ingestion, reporting, user-mgmt, assembler,
  router, **agent-orchestrator**, **MCP servers**) → `Deployment` + `Service`, HPA
  on CPU/RPS, `PodDisruptionBudget`, liveness/readiness probes. The agent
  orchestrator + MCP servers (analytics/retrieval/ingest) are **CPU-only** — they
  query stores and call the Stage-2 LLM backend; no GPU. MCP servers are
  `ClusterIP`-only (internal), reachable by the orchestrator, not the gateway.
- **Workers** (nlp, llm) → `Deployment` on **GPU node pools** (nodeSelector +
  tolerations + `nvidia.com/gpu` resource requests), scaled by **KEDA** on Kafka
  consumer lag.
- **Model servers** → Triton (NLP) always on the GPU pool. The **Stage-2 LLM
  backend is selected per environment** via `LLM_BACKEND`:
  - `local`: two vLLM `Deployment`s (LLM-A, LLM-B) on the GPU pool, a `Service`
    each for in-cluster HTTP; no egress.
  - `groq`: no vLLM deployments — the Stage-2 worker (a stateless `Deployment`,
    HPA/KEDA on queue depth, **no GPU**) calls Groq over HTTPS. Allow egress to
    Groq in `NetworkPolicy`/egress rules and mount `GROQ_API_KEY` from a Secret.
    Both are valid simultaneously for **hybrid/failover**; the worker picks per
    request/policy. Switching backends is a config rollout, not a rebuild.
- **Stateful infra** (Kafka, PostgreSQL + pgvector, ClickHouse) → operators or
  `StatefulSet` + `PersistentVolumeClaim`; or managed equivalents to cut ops. The
  vector index lives in Postgres (the `pgvector` extension), so it scales with the
  Postgres StatefulSet — no separate vector-store workload.
- **Object storage** → MinIO operator or cloud S3.

### Autoscaling

- **KEDA** `ScaledObject` per worker pool with a **lag** trigger
  (scale 0→N when the consumer group's lag > threshold) → workers track backlog,
  scale to zero between batches, surge for 10k/100k bursts.

> **Two things here failed silently until 4 August 2026** — worth knowing
> because both are easy to reintroduce ([PROJECT_ASSESSMENT.md](PROJECT_ASSESSMENT.md) §5.5):
>
> 1. **The trigger must name the consumer group the worker actually creates.**
>    Three of five scalers named groups (`ingestion-group`, `router-group`,
>    `stage2-llm-group`) that no worker ever created, and two named streams
>    (`stage1_nlp:queue`, `stage2_llm:queue`) that do not exist. A
>    `redis-streams` trigger pointed at a nonexistent group reports **no
>    backlog** — so ingestion, the router and **Stage 2, the only stage where
>    scaling changes cost or latency**, never scaled, while the manifests read as
>    correct. Names now come from `libs/streams.py` and `tests/test_streams.py`
>    asserts the manifests agree with the workers.
> 2. **`pendingEntriesCount` cannot scale from zero.** Pending entries are
>    messages *delivered to a consumer and not yet ACKed*; with
>    `minReplicaCount: 0` there is no consumer, so nothing is delivered, the
>    count stays 0, and KEDA never wakes the deployment. Every trigger now uses
>    **`lagCount`** (stream length vs the group's last-delivered id), which is
>    the only Redis-Streams metric that works from zero.

- **HPA** for the API tier (CPU / requests-per-second).
- **Cluster Autoscaler / Karpenter** to add GPU nodes (incl. **spot**) for batch
  surges; batch tolerates preemption thanks to Kafka replay.

### Database schema to provision

Beyond the analysis tables, three things must be seeded for the API to run
**closed** rather than in dev-permissive mode (see [run.md](run.md) §"Auth &
tenancy"):

| Table | Why |
| ----- | --- |
| `api_keys` | SHA-256 key hashes plus the **`tenant_id`** each key belongs to. Without rows, unknown keys are accepted in dev and rejected everywhere else. The tenant comes from here, never from a client-supplied token. |
| `users` | `pbkdf2_sha256` password hashes. Without rows, `POST /v1/auth/token` issues a token to anybody in dev and refuses everyone outside it. |
| `tenant_policies` | `privacy_locked` per tenant. The check **fails closed**: an unreachable policy table refuses a `groq` request rather than permitting it. |

Also set **`APP_ENV`** to something other than `dev`, or the permissive fallbacks
stay on and a placeholder `JWT_SECRET` is tolerated.

### Networking & security

- **Ingress controller** (NGINX) terminates TLS; cert-manager for certs.
- **NetworkPolicies**: only ingress reaches services; only services reach data
  namespace; model servers reachable only by workers. On the `groq` backend,
  allow **egress from the Stage-2 worker to the Groq API only** (default-deny
  egress elsewhere); on `local` the workers need no internet egress at all.
- Optional **Linkerd** service mesh for mTLS + retries + traffic splitting
  (canary) east-west.
- **Secrets** via Kubernetes Secrets + (recommended) Vault/External Secrets —
  including the **`GROQ_API_KEY`**, mounted only into the Stage-2 worker when the
  `groq` backend is enabled.
- Private node pools for `data` and `models`; only ingress is internet-facing.

### Reliability

- Multi-AZ node pools; PodDisruptionBudgets; replicas ≥ 2 for stateless.
- DLQ topic in Kafka; alert on DLQ growth (see [infrastructure.md](infrastructure.md)).
- Rolling updates with readiness gates; canary via mesh or two Deployments.
- Backups: Postgres PITR (covers the pgvector embeddings), ClickHouse snapshots to object storage.

### CI/CD

- Build → scan images → push to registry → deploy via Helm/Kustomize (+ ArgoCD
  for GitOps). Model artifacts versioned in object storage; `model_versions`
  recorded in every result for reproducible replay.

See [plan.md](plan.md) for the phased order to build all of this.

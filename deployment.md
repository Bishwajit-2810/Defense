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
  worker-nlp     (Python)           → Stage-1 small-model suite (GPU)
  worker-llm     (Python + vLLM)    → Stage-2 local LLMs A+B (share GPU, no API)
  redis          (cache/queue)      → Redis Streams = bus + cache + dedup
  postgres       (ops + jobs)
  clickhouse     (analytics)
  qdrant         (vectors)
  minio          (object storage)
  prometheus + grafana + loki       → monitoring
```

- Queue = **Redis Streams** (no separate Kafka to operate yet).
- API services can be one process for the MVP; split later.
- GPU shared between NLP and the local LLM(s) via time-slicing (run LLM-A only at
  MVP scale; both LLM-A and LLM-B are self-hosted — no external API).
- Goal: prove the hybrid pipeline end-to-end on 1k post+comment-thread batches —
  scraper payload in, structured JSON out (see [examples.md](examples.md)).

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
   ┌──────▼───────┐      ┌──────▼────────┐
   │ triton (GPU) │      │ vllm A + B    │   model servers (2 local LLMs, no API)
   └──────────────┘      └───────────────┘
          │ writes (assembler Deployment) │
   ┌──────▼───────┬──────────┬────────────▼──────┬───────────┐
   │ postgres     │clickhouse│ qdrant            │ minio/S3  │  StatefulSets / managed
   │ (HA, replica)│(cluster) │ (sharded)         │           │
   └──────────────┴──────────┴───────────────────┴───────────┘
        observability namespace: prometheus, grafana, loki, jaeger, otel-collector
```

---

## 4. Kubernetes deployment plan

### Namespaces

`platform` (services + workers), `data` (DBs, queue), `models` (Triton, vLLM),
`observability` (monitoring), `ingress`.

### Workload mapping

- **Stateless services** (auth, ingestion, reporting, user-mgmt, assembler,
  router) → `Deployment` + `Service`, HPA on CPU/RPS, `PodDisruptionBudget`,
  liveness/readiness probes.
- **Workers** (nlp, llm) → `Deployment` on **GPU node pools** (nodeSelector +
  tolerations + `nvidia.com/gpu` resource requests), scaled by **KEDA** on Kafka
  consumer lag.
- **Model servers** (Triton + two vLLM deployments, LLM-A and LLM-B) →
  `Deployment`/`StatefulSet` on GPU pool, `Service` each for in-cluster
  gRPC/HTTP. Both LLMs are local; no egress to an external API.
- **Stateful infra** (Kafka, PostgreSQL, ClickHouse, Qdrant) → operators or
  `StatefulSet` + `PersistentVolumeClaim`; or managed equivalents to cut ops.
- **Object storage** → MinIO operator or cloud S3.

### Autoscaling

- **KEDA** `ScaledObject` per worker pool with a Kafka-lag trigger
  (e.g. scale 0→N when topic lag > threshold) → workers track backlog, scale to
  zero between batches, surge for 10k/100k bursts.
- **HPA** for the API tier (CPU / requests-per-second).
- **Cluster Autoscaler / Karpenter** to add GPU nodes (incl. **spot**) for batch
  surges; batch tolerates preemption thanks to Kafka replay.

### Networking & security

- **Ingress controller** (NGINX) terminates TLS; cert-manager for certs.
- **NetworkPolicies**: only ingress reaches services; only services reach data
  namespace; model servers reachable only by workers.
- Optional **Linkerd** service mesh for mTLS + retries + traffic splitting
  (canary) east-west.
- **Secrets** via Kubernetes Secrets + (recommended) Vault/External Secrets.
- Private node pools for `data` and `models`; only ingress is internet-facing.

### Reliability

- Multi-AZ node pools; PodDisruptionBudgets; replicas ≥ 2 for stateless.
- DLQ topic in Kafka; alert on DLQ growth (see [infrastructure.md](infrastructure.md)).
- Rolling updates with readiness gates; canary via mesh or two Deployments.
- Backups: Postgres PITR, ClickHouse + Qdrant snapshots to object storage.

### CI/CD

- Build → scan images → push to registry → deploy via Helm/Kustomize (+ ArgoCD
  for GitOps). Model artifacts versioned in object storage; `model_versions`
  recorded in every result for reproducible replay.

See [plan.md](plan.md) for the phased order to build all of this.

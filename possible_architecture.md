# Possible Architectures & Tradeoffs

This document records the major options considered for each decision point and
why the recommended choice in [architecture.md](architecture.md) won. Use it to
revisit a decision if constraints change.

Context for every choice below: the service is a **self-hosted microservice** that
ingests scraped **post + comment threads** (Bangla/English/Banglish) and emits
structured JSON, under hard constraints — **no external/paid API**, fast, cheap,
Bangla-accurate. That rules out anything that forces data egress or per-token
billing, and favors operationally simple, horizontally scalable components.

---

## 1. Overall topology options

### Option A — Monolith + background workers (rejected for prod)

Single FastAPI app with a Celery/Redis worker pool. All analysis in-process.

- **Pros:** Fastest to build, one deployable, fine for the MVP's 1k posts.
- **Cons:** Stages can't scale independently; expensive LLM GPUs idle while NLP
  runs; one crash takes everything; hard to reach 10k+.
- **Verdict:** Good _only_ as the MVP shape (Docker Compose). Don't carry to prod.

### Option B — Stage-decoupled, queue-based microservices (RECOMMENDED)

Ingestion → bus → Stage-1 NLP pool → router → Stage-2 LLM pool → assembler →
stores. See [architecture.md](architecture.md) §2.

- **Pros:** Independent scaling per stage, backpressure, fault isolation,
  replay, clean autoscaling on queue depth.
- **Cons:** More moving parts; needs orchestration (K8s) and observability.
- **Verdict:** Chosen for Production/Enterprise.

### Option C — Serverless / FaaS pipeline (rejected)

Lambda/Cloud Run functions per stage, managed queues.

- **Pros:** No infra to run; scales to zero; pay-per-use.
- **Cons:** GPU support is poor/expensive on FaaS; cold starts on big models are
  killers; per-invocation model loading wastes money. Self-hosting GPUs is far
  cheaper at sustained batch volume.
- **Verdict:** Only attractive if volume is sparse and bursty — not this workload.

---

## 2. Message bus: Kafka vs RabbitMQ vs Redis Streams vs NATS

| Criterion              | **Kafka**                    | **RabbitMQ**              | **Redis Streams**               | **NATS (JetStream)**  |
| ---------------------- | ---------------------------- | ------------------------- | ------------------------------- | --------------------- |
| Model                  | Distributed log, partitioned | Broker + exchanges/queues | Log on Redis                    | Lightweight log/queue |
| Throughput             | Very high (millions/s)       | High                      | High (single-node bound)        | Very high             |
| Replay / retention     | Excellent (native, long)     | Limited (consume-once)    | Good (capped streams)           | Good (JetStream)      |
| Ordering               | Per-partition                | Per-queue                 | Per-stream                      | Per-subject           |
| Scalability            | Horizontal, mature           | Cluster (more fiddly)     | Limited by Redis node           | Horizontal, simple    |
| Operational complexity | High (ZK/KRaft, tuning)      | Medium                    | **Low**                         | Low–medium            |
| Cost                   | Higher (infra + ops)         | Medium                    | **Low** (often already present) | Low                   |
| Reliability            | Excellent                    | Excellent                 | Good (needs persistence config) | Good                  |
| Ecosystem              | Huge (Connect, streams)      | Mature                    | Minimal                         | Growing               |

**Decision:**

- **MVP → Redis Streams.** You already run Redis for cache/dedup; consumer
  groups give at-least-once delivery and DLQ patterns with near-zero extra ops.
- **Production/Enterprise → Kafka.** Partitioning maps perfectly to worker
  parallelism; retention/replay lets you reprocess batches after a model upgrade;
  back-pressure and lag metrics drive KEDA autoscaling.
- **RabbitMQ** would be the pick if you needed complex routing topologies or
  per-message TTL/priority more than replayable throughput — not the case here.
- **NATS** is a strong, lighter alternative to Kafka; choose it if ops simplicity
  matters more than Kafka's ecosystem (Connect, ksqlDB, tooling). Kept as a
  documented fallback.

---

## 3. Operational database: PostgreSQL vs MongoDB

| Criterion               | **PostgreSQL**                            | **MongoDB**                         |
| ----------------------- | ----------------------------------------- | ----------------------------------- |
| Schema                  | Relational + JSONB (flexible when needed) | Document, schema-flexible           |
| Transactions            | Strong ACID                               | Multi-doc since 4.x, weaker culture |
| Job/state tracking      | Excellent (constraints, FKs)              | Workable                            |
| Flexible nested results | JSONB columns                             | Native                              |
| Aggregations            | OK (not for heavy analytics)              | Aggregation pipeline (OK)           |
| Ops maturity            | Very high                                 | High                                |

**Decision: PostgreSQL.** Job/tenant/user state wants ACID and constraints, and
JSONB covers the flexible-result need without giving up relational integrity.
MongoDB (the prompt's other suggestion) is fine for the flexible result document
but we already get that via JSONB + the analytics store, so we avoid running two
philosophies. Heavy aggregation goes to ClickHouse, not the operational DB.

---

## 4. Analytics store: ClickHouse vs Elasticsearch

| Criterion               | **ClickHouse**                    | **Elasticsearch**            |
| ----------------------- | --------------------------------- | ---------------------------- |
| Primary strength        | Columnar OLAP aggregations        | Full-text search + analytics |
| Trend/aggregate queries | Excellent, sub-second on billions | Good, heavier                |
| Full-text search        | Basic                             | Excellent                    |
| Storage efficiency      | Very high (compression)           | Lower                        |
| Cost at scale           | Lower                             | Higher (JVM, shards)         |
| Ops                     | Medium                            | Medium–high                  |

**Decision: ClickHouse** for trend analysis, brand-mention counts, time-series
aggregations, and dashboard charts — that's the dominant analytics need.
**Optional Elasticsearch/OpenSearch** can be added later _only if_ rich
keyword/full-text search over post text becomes a first-class feature; Qdrant +
ClickHouse cover semantic search and aggregation in the meantime.

---

## 5. Vector database: Qdrant vs Weaviate vs Milvus

| Criterion        | **Qdrant**                    | **Weaviate**            | **Milvus**               |
| ---------------- | ----------------------------- | ----------------------- | ------------------------ |
| Language/perf    | Rust, fast, low memory        | Go, feature-rich        | C++, very scalable       |
| Filtering        | Excellent payload filters     | Good                    | Good                     |
| Ops simplicity   | **High**                      | Medium                  | Lower (more components)  |
| Built-in modules | Lean (BYO embeddings)         | Many (modules, hybrid)  | Lean                     |
| Scale ceiling    | High                          | High                    | **Very high** (billions) |
| Best fit         | Pragmatic mid-scale, easy ops | Hybrid search + modules | Massive enterprise scale |

**Decision: Qdrant** for MVP→Production: easiest to operate, fast, excellent
metadata filtering (needed to scope vectors by tenant/platform/time). Reassess
**Milvus** at Enterprise/100k-batch scale if vector count reaches billions and
you need its distributed sharding. Weaviate is the pick only if you want its
built-in hybrid-search/module ecosystem over BYO simplicity.

---

## 6. Caching layer composition

All of these are used; they are complementary, not alternatives:

- **Redis** — LLM response cache, embedding cache, dedup set, rate-limit
  counters, hot query results. Central and essential.
- **Embedding cache** — keyed by `content_hash`; avoids recomputing vectors for
  duplicates/reshares.
- **LLM response cache** — keyed by `(model, task, content_hash)`; repeats are
  free.
- **Query cache** — short-TTL cache of expensive ClickHouse aggregations powering
  the dashboard.
- **CDN** — fronts the Flutter web assets and any public/static report exports;
  not for dynamic per-tenant data.

---

## 7. Load balancing & traffic management

| Tool                                                      | Role here                                                                     | When to choose                                                                                                                                           |
| --------------------------------------------------------- | ----------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **NGINX**                                                 | API gateway / reverse proxy / TLS / simple rate limit                         | MVP and default ingress; simple, battle-tested                                                                                                           |
| **HAProxy**                                               | High-performance L4/L7 LB                                                     | If you need advanced LB algorithms / very high connection counts outside K8s                                                                             |
| **Kubernetes Ingress** (NGINX/Traefik ingress controller) | In-cluster north-south routing + TLS                                          | Production on K8s — the default                                                                                                                          |
| **Service Mesh (Istio / Linkerd)**                        | mTLS, retries, traffic splitting, fine-grained observability between services | Add at Production+ when you want zero-trust east-west + canary deploys; **Linkerd** if you want light/simple, **Istio** if you need its full feature set |

**Decision:**

- **API LB:** K8s Ingress (NGINX controller) in prod; plain NGINX in the MVP.
- **Worker LB:** not HTTP — workers self-balance by pulling from partitioned
  queues (competing consumers). "Load balancing" for workers = partition count +
  KEDA replica scaling.
- **Queue partitioning:** partition by `hash(post_id)`; keep partitions ≥ max
  consumers so every worker can be busy.
- **Service mesh:** start without one (mesh adds latency + ops); introduce
  **Linkerd** at Production for mTLS + retries if security/observability needs it.

---

## 8. LLM serving: self-host vs API, and why two local models

**Hard constraint: no external/paid LLM API.** Every model in this design,
including both Stage-2 LLMs, runs on our own GPUs. The table below records why,
and why we run _two_ local models rather than one.

| Option                            | Pros                                                                         | Cons                                                                          | Verdict                                 |
| --------------------------------- | ---------------------------------------------------------------------------- | ----------------------------------------------------------------------------- | --------------------------------------- |
| **External API**                  | Zero ops; frontier quality; elastic                                          | Per-token cost; data leaves the cluster; rate limits                          | **Rejected** — violates the no-API rule |
| **Single self-hosted LLM**        | One model to run; simplest                                                   | Either overpay (big model per post) or under-deliver (small model on reports) | Workable for the MVP only               |
| **Two self-hosted LLMs (CHOSEN)** | Right-sized per job; data in-house; no per-token bill; scale each separately | Two model deployments to run                                                  | **Chosen** for Production/Enterprise    |

Why two local models (see [models.md](models.md) §2):

- **LLM-A** — a fast 7B/8B (AWQ/GPTQ) for the **high-volume, low-difficulty**
  per-post refinement and short summaries the router sends from Stage 1.
- **LLM-B** — a larger 14B/32B for the **low-volume, high-quality** cluster
  summarization, corpus insight, and grounded report generation (RAG).

This pairs with the router in [architecture.md](architecture.md) §5: per-post
selective work hits the cheap LLM-A; cluster/report generation hits LLM-B. If
LLM-B is saturated the router degrades to LLM-A or returns Stage-1-only results —
it never calls out to a third-party API. At MVP scale the two can time-slice one
GPU, or run LLM-A alone until volume justifies LLM-B.

---

## 9. Deployment: Docker Compose vs Kubernetes

Summarized here; full plan in [deployment.md](deployment.md).

|                | **Docker Compose**  | **Kubernetes**              |
| -------------- | ------------------- | --------------------------- |
| Setup          | Minutes             | Days                        |
| Scaling        | Manual, single host | Auto (HPA/KEDA), multi-node |
| HA             | None                | Yes                         |
| GPU scheduling | Manual              | Native (device plugin)      |
| Cost           | Low                 | Higher baseline             |
| Best for       | **MVP / 1k posts**  | **Production / 10k+**       |

**Decision:** Compose for MVP, Kubernetes + KEDA for Production and Enterprise.

---

## 10. RAG: needed or not?

Short answer: **not for per-post analysis; yes for the reporting/insight layer.**
Full reasoning in [models.md](models.md) §RAG. Per-post classification needs no
retrieval. RAG becomes valuable for analyst Q&A over the corpus, grounded report
generation, and "what are people saying about X" queries — backed by Qdrant +
the embeddings you already compute.

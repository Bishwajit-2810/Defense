# Tech Stack

> **Scope.** Every technology this system runs on, the version actually installed
> on this checkout, and — where the choice was not obvious — the one-line reason it
> was chosen over the alternative. Verified against `pyproject.toml`,
> `dashboard/package.json`, `deploy/docker-compose.yml` and the installed
> environment on **23 August 2026**.
>
> Alternatives that were considered and rejected are
> [possible_architecture.md](possible_architecture.md); *why* each model was
> picked is [models.md](models.md); sizing and cost are
> [infrastructure.md](infrastructure.md) and
> [cost_estimation.md](cost_estimation.md).

---

## 1. At a glance

| Layer | Choice |
| ----- | ------ |
| **Language / runtime** | Python **3.12** (pinned), Node **26.7.0** for the dashboard toolchain |
| **Package manager** | [uv](https://docs.astral.sh/uv/) 0.12.5 (Python) · npm 12.0.2 (JS) |
| **API** | FastAPI 0.136 on Uvicorn 0.49, Starlette 0.50 |
| **Workers** | Five plain `asyncio` processes over Redis Streams — no Celery, no Airflow |
| **Message bus** | Redis Streams (MVP default) · Kafka behind `BUS_BACKEND=kafka` |
| **Operational DB + vectors** | PostgreSQL 16 + **pgvector** (one store, not two) |
| **Analytics** | ClickHouse |
| **Cache / queues / counters** | Redis 7 |
| **Object storage** | MinIO (S3-compatible) |
| **LLM serving** | Ollama (dev) / vLLM (prod) — or Groq Cloud, switchable at runtime |
| **ML runtime** | PyTorch 2.12 + Transformers 5.11 (optional extras) |
| **Agent tooling** | FastMCP 3.4 — three MCP servers, 18 tools |
| **Dashboard** | React 19 + Vite 8 + Tailwind 3, charts via Chart.js 4 |
| **Observability** | structlog → Loki · Prometheus + Grafana · OpenTelemetry → Jaeger |
| **Tests** | pytest 9 + pytest-asyncio + testcontainers · vitest 4 + Playwright · oxlint |
| **Deployment** | Docker Compose (MVP) · Kubernetes + KEDA (production) |

## 2. Backend runtime

### Python 3.12, and why it is pinned

`.python-version` pins **3.12**; `requires-python` is `>=3.11`. The pin exists for
exactly one dependency: the `ml` extra **cannot be installed on 3.13 at all**.
`fasttext-wheel` 0.9.2 publishes no cp313 wheel and its bundled C++ no longer
compiles (`args.cc` fails on an out-of-scope declaration). Nothing else in the
tree is version-constrained — the language detector alone was holding the
embedding model hostage, which is also why `embeddings` exists as a separate,
lighter extra from `ml`.

### Core dependencies (installed versions)

| Package | Version | Role |
| ------- | ------- | ---- |
| `fastapi` | 0.136.3 | API framework |
| `starlette` | 0.50.0 | Pinned `>=0.40,<0.51` — FastAPI 0.128+ needs `<0.51` |
| `uvicorn[standard]` | 0.49.0 | ASGI server |
| `sqlalchemy[asyncio]` | 2.0.50 | Async ORM / Core, used mostly as Core with explicit SQL |
| `asyncpg` | 0.31.0 | Async Postgres driver (`postgresql+asyncpg://`) |
| `psycopg2-binary` | 2.9.12 | Sync driver for the few sync paths and tooling |
| `pgvector` | 0.5.0 | Vector type binding for the `vector(768)` column |
| `redis[hiredis]` | 8.0.0 | Streams, cache, counters, pub/sub |
| `clickhouse-driver` | 0.2.10 | Analytics writes and reads |
| `boto3` | 1.43.27 | S3/MinIO object storage |
| `pydantic` / `pydantic-settings` | 2.13.4 / 2.14.1 | Request models and the single `Settings` object |
| `jsonschema` | 4.26.0 | Enforces the input/output JSON contracts |
| `structlog` | 26.1.0 | Structured logging |
| `tenacity` | 9.1.4 | Retry with exponential backoff around LLM calls |
| `openai` | 2.41.1 | The OpenAI-compatible client used for **both** backends |
| `fastmcp` | 3.4.0 | The three MCP servers |
| `torch` / `transformers` | 2.12.0 / 5.11.0 | Classifier heads and encoders |
| `numpy` | 2.4.6 | Vectors, k-means, fusion maths |
| `python-jose[cryptography]` | 3.5.0 | JWT (HS256) |
| `passlib[bcrypt]` | 1.7.4 | Password hashing |
| `weasyprint` / `reportlab` | 69.0 / 5.0.0 | PDF export — **optional import**, tolerated when absent |
| `psutil` | 7.2.2 | Host telemetry for the System Monitor |
| `prometheus-fastapi-instrumentator` | 7.1.0 | Pinned `<8` — 8.x pulls an incompatible Starlette |
| `emoji` | 2.15.0 | Comment-kind classification and the emoji lexicon |
| `msgpack` | 1.2.1 | Compact stream payloads |

### Optional extras

Deliberately split, so a box only installs what it will actually run:

| Extra | `uv sync --extra …` | Contents | Needed for |
| ----- | ------------------- | -------- | ---------- |
| `embeddings` | `embeddings` | `sentence-transformers` | Real vectors **only**. Retrieval and clustering, without loading CLIP/NER/KeyBERT onto a GPU that is usually already hosting the LLM. Pairs with `EMBEDDING_STUB_MODE=false`. |
| `ml` | `ml` | `sentence-transformers`, `fasttext-wheel`, `keybert`, `scikit-learn`, `Pillow` | The full Stage-1 model suite (`MODEL_STUB_MODE=false`). Wants a GPU. **Python ≤ 3.12.** |
| `dev` | `dev` | `pytest`, `pytest-asyncio`, `ruff` | Tests and lint |
| `obs` | `obs` | OpenTelemetry SDK + OTLP gRPC exporter + FastAPI instrumentation | `OTEL_ENABLED=true` |
| `kafka` | `kafka` | `aiokafka` | `BUS_BACKEND=kafka`. Redis Streams needs nothing extra. |

**Installed on this checkout:** `sentence-transformers` 5.5.1, `scikit-learn` 1.9.0
and `Pillow` 12.2.0 are present; `fasttext-wheel`, `keybert`, `aiokafka`,
`opentelemetry-sdk` and `ruff` are **not**. So this box runs with real embeddings
but the *script heuristic* for language detection rather than fastText — which is
exactly what `language_method` on every result reports
([STAGE1_NLP.md](STAGE1_NLP.md) §2).

`embeddings` is separate from `ml` for a reason worth keeping: hybrid retrieval
fixed *search* without real embeddings (the lexical arm carries it), but **nothing
fixes clustering** without them — hash vectors group posts at random, and the
per-cluster LLM summaries then describe nothing. See
[REPORTS.md](REPORTS.md) §4 and [SEARCH.md](SEARCH.md) §6.

## 3. Data stores — split by access pattern

| Store | Image | Holds | Why this one |
| ----- | ----- | ----- | ------------ |
| **PostgreSQL 16 + pgvector** | `pgvector/pgvector:pg16` | `posts`, `analysis_results` (canonical JSON **+ the `vector(768)` embedding**), `comments`, `post_chunks`, `comment_embeddings`, `cluster_labels`, `jobs`, `users`, `api_keys`, `tenant_policies`, `chat_conversations`, `chat_messages`, `campaigns` | Operational reads need transactions; vectors need ANN. pgvector gives both in one store, so **there is no separate vector-store URL** — this replaced a standalone Qdrant service. `deploy/init-db.sql` runs `CREATE EXTENSION vector`. |
| **ClickHouse** | `clickhouse/clickhouse-server:latest` | `analysis_events` (MergeTree, partitioned `toYYYYMM(created_at)`) · `comment_sentiments` (**ReplacingMergeTree**, so a re-analysed comment collapses in the engine) | Column-store aggregates over millions of comment rows, which is what the dashboard charts and `analytics-mcp` ask for. |
| **Redis 7** | `redis:7-alpine` | Stage streams + consumer groups, LLM response cache, dedup keys, rate limits, job counters, cancellation flags, progress pub/sub, the live log buffer, the runtime config toggles | One dependency covering the queue *and* every short-lived counter the pipeline needs. |
| **MinIO** | `minio/minio:latest` | Raw result JSON at a deterministic key | S3-compatible, so production can swap in real S3 with no code change. |

Schema DDL: [`deploy/init-db.sql`](../deploy/init-db.sql) (Postgres) and
[`src/defense/services/workers/assembler/clickhouse_init.sql`](../src/defense/services/workers/assembler/clickhouse_init.sql).
Details and the two things the ClickHouse schema deliberately does *not* have:
[ASSEMBLER.md](ASSEMBLER.md) §3.

## 4. Messaging

**Redis Streams is the MVP bus; Kafka is the production option.** Five streams,
one per hop, with a consumer group each — all names in
[`libs/streams.py`](../src/defense/libs/streams.py), which is the single source the
KEDA manifests are asserted against.

Workers are **plain `asyncio` processes**, not Celery or Airflow tasks: each is a
`XREADGROUP` loop with cooperative cancellation, bounded retry and a dead-letter
stream. That keeps the whole control surface — stop, resume, retry, DLQ — in code
this repo owns, which is what made per-post tracing and bounded stops possible.
See [PIPELINE.md](PIPELINE.md).

## 5. LLM serving

| Backend | What runs it | Cost shape |
| ------- | ------------ | ---------- |
| **`local`** (default) | **Ollama** for dev, **vLLM** for production | GPU line item, **zero marginal token cost**, no data egress |
| **`groq`** | Groq Cloud API | Per-token, zero GPU ops, fastest inference |

Both speak an OpenAI-compatible API, so the same `openai` client serves both and
switching is a config change — per request, or globally via a Redis toggle.

Seven **roles** (`stage1`, `stage2`, `summary`, `agent`, `llm_a`, `llm_b`, `vlm`)
map to concrete models per backend. Models pulled on this machine:

| Role | Local model here |
| ---- | ---------------- |
| `stage1` | `gemma3:4b` |
| `stage2`, `summary`, `llm_a`, `llm_b` | `qwen2.5:7b` |
| `agent` | **`llama3.1:8b-16k`** |
| `vlm` | `qwen3-vl:4b` |

`llama3.1:8b-16k` is a **derived tag**, built from
[`config/Modelfile.llama31-16k`](../config/Modelfile.llama31-16k) — `FROM llama3.1:8b`
plus `PARAMETER num_ctx 16384`. It exists because `ollama serve` defaults to a
4,096-token context and **silently discards** the overflow, oldest messages first
— which means the system prompt and the operator's question are what get thrown
away on any large tool result. Measured: an 11k-token prompt evaluates **24**
tokens on `llama3.1:8b` and all **11,045** on the `-16k` tag. Full detail:
[LLM_BACKENDS.md](LLM_BACKENDS.md) §2.

Setup: `ollama pull gemma3:4b qwen2.5:7b qwen3-vl:4b llama3.1:8b`, then build the
16k tag — [easy_run.md](easy_run.md) §1.

## 6. ML models (optional, `MODEL_STUB_MODE=false`)

| Task | Model |
| ---- | ----- |
| Embeddings | `paraphrase-multilingual-mpnet-base-v2` (768-dim, covers bn/en/Banglish) |
| Language ID | fastText |
| Caption sentiment | Language-routed: BanglaBERT (`bn`) / BanglishBERT (`banglish`) / XLM-R (else) |
| Comment sentiment ensemble | **Seven** HF heads — see [STAGE2_LLM.md](STAGE2_LLM.md) §3 for the checkpoints |
| NER | GLiNER multilingual |
| Keywords | KeyBERT |
| Image sentiment | SigLIP / CLIP zero-shot — ⚠️ **unexercised**, no image bytes are reachable |
| OCR | Tesseract (bn+eng) — off by default |

Selection rationale per task: [models.md](models.md). What Stage 1 does with them:
[STAGE1_NLP.md](STAGE1_NLP.md).

## 7. Frontend

| Choice | Version | Note |
| ------ | ------- | ---- |
| React | 19.2.8 | |
| Vite | 8.2.0 | Rolldown-based; `manualChunks` splits vendor libs |
| Tailwind CSS | 3.4.19 | Dark mode via the `class` strategy |
| Chart.js + react-chartjs-2 | 4.5.1 / 5.3.1 | |
| lucide-react | 1.30.0 | Icons |
| PostCSS + autoprefixer | 8.5.26 / 10.5.4 | |
| vitest + @testing-library/react + jsdom | 4.1.10 / 16.3.2 / 30.0.1 | Unit tests |
| Playwright | 1.62.1 | E2E, chromium project only |
| oxlint | 1.75.0 | Lint — silent on success |

Eleven tabs, `activeTab` state in `App.jsx`, SSE for every live surface. The API
base URL is a **hardcoded constant** in `src/utils/api.js` — no Vite proxy, no env
var. Architecture: [DASHBOARD_UI.md](DASHBOARD_UI.md).

> The original **vanilla HTML/CSS/JS** dashboard the design docs specify is
> preserved at `dashboard_legacy/` and still runs with no build step. Where a
> design doc says "plain HTML/CSS/JS, no framework", that is the superseded
> decision.

## 8. Observability

| Concern | Stack |
| ------- | ----- |
| Structured logs | `structlog` → stdout **and** a Redis buffer (`logs:recent` / `logs:live`) the dashboard Logs tab tails → Promtail 3.1.1 → **Loki 3.1.1** |
| Metrics | `prometheus-fastapi-instrumentator` → **Prometheus** → **Grafana** |
| Tracing | OpenTelemetry SDK → OTLP/gRPC → **Jaeger 1.60** (`OTEL_ENABLED=true`, `obs` extra) |
| Host + stack telemetry | `psutil` + shell probes → `GET /v1/system/{stats,health,stream}` |
| Cost | Redis counters in `LLMClient` → `GET /v1/usage` |

Details: [SYSTEM_MONITOR.md](SYSTEM_MONITOR.md).

## 9. Testing and tooling

| Tool | Version | Scope |
| ---- | ------- | ----- |
| pytest | 9.0.3 | 1,396 tests across 67 files |
| pytest-asyncio | 1.4.0 | The async suites |
| testcontainers[postgres,redis] | 4.15.0 | Throwaway datastores — a default run never touches the dev stack |
| httpx | 0.28.1 | API tests via ASGI transport |
| ruff | declared in the `dev` extra — **not** installed by a default `uv sync` | Python lint, line-length 100, target py311 (`[tool.ruff]`) |
| vitest / Playwright / oxlint | see §7 | Frontend |

Two pytest markers gate the expensive paths: `e2e` (needs a full Docker + Ollama
stack) and `destructive` (**wipes live datastores**, opt-in via
`RUN_DESTRUCTIVE_E2E=1`) — a bare `pytest` must never be able to destroy a
developer's dev environment. Latest full-chain output:
[TESTING_RESULTS.md](TESTING_RESULTS.md). How to run each suite:
[testing.md](testing.md).

## 10. Deployment

| Target | Stack |
| ------ | ----- |
| **Local dev** | `uv run run_all.py --with-agents` — datastores in Docker, 11 processes on the host (API, 5 workers, 3 MCP servers, agents, dashboard) |
| **MVP** | Docker Compose — **19 services** in [`deploy/docker-compose.yml`](../deploy/docker-compose.yml): **9** off-the-shelf images, **10** built from this repo, plus 6 named volumes and 1 network |
| **Production** | Kubernetes — 10 manifests in [`deploy/k8s/`](../deploy/k8s), including **KEDA** `ScaledObject`s that autoscale on Redis stream depth, a NetworkPolicy and an Ingress |

Compose images: `pgvector/pgvector:pg16`, `redis:7-alpine`,
`clickhouse/clickhouse-server:latest`, `minio/minio:latest`,
`prom/prometheus:latest`, `grafana/grafana:latest`,
`jaegertracing/all-in-one:1.60`, `grafana/loki:3.1.1`, `grafana/promtail:3.1.1`.

Compose vs Kubernetes trade-offs and the full K8s plan:
[deployment.md](deployment.md).

## 11. Notable version constraints

Each of these is a pin that has already broken something once:

| Constraint | Reason |
| ---------- | ------ |
| `.python-version = 3.12` | `fasttext-wheel` has no cp313 wheel and will not compile on 3.13 |
| `starlette>=0.40,<0.51` | FastAPI 0.128+ requires `<0.51` |
| `prometheus-fastapi-instrumentator>=7.0,<8` | 8.x pulls an incompatible Starlette |
| `weasyprint` imported in a `try` | PDF export must be optional, not a hard startup dependency |
| Ollama `num_ctx 16384` via a derived tag | The server's 4,096 default silently truncates, and `OLLAMA_CONTEXT_LENGTH` is read by `ollama serve`, **not** by this app |
| `EMBEDDING_DIM` must match the pgvector column | Changing it without a migration breaks every insert |

---

## Related documents

- [architecture.md](architecture.md) — how these pieces are wired together
- [possible_architecture.md](possible_architecture.md) — the alternatives considered and rejected
- [models.md](models.md) — model selection per task, and the fine-tuning strategy
- [infrastructure.md](infrastructure.md) · [cost_estimation.md](cost_estimation.md) — sizing and spend
- [deployment.md](deployment.md) — Compose vs Kubernetes
- [env.example.md](env.example.md) — every environment variable and its failure mode
- [run.md](run.md) · [easy_run.md](easy_run.md) — getting it running
- [testing.md](testing.md) · [TESTING_RESULTS.md](TESTING_RESULTS.md) — the suites and the latest run

# run.md — How to run the Smart Layer end-to-end

This is the **operational runbook**: how to bring the whole pipeline up and push
the 50 sample posts (`posts_with_details.json`) through it until they land as
schema-valid analysis in Postgres (+pgvector) / ClickHouse / MinIO, then read them
back over the API.

There are two ways to run it:

- **Mode A — Local dev (verified, recommended): infra in Docker, services on the
  host, in stub mode.** No GPU, no model weights, no Groq key. This is the path
  proven end-to-end and the one documented in full below.
- **Mode B — Full Docker Compose.** Production-style MVP: everything (infra +
  app services) in containers. The images now build (repo-root build contexts,
  module-path `CMD`s) — see [§9](#9-docker-compose-build-mode-b).

The pipeline is: `API → ingestion → stage1 NLP → router → stage2 LLM → assembler`
→ Postgres (+pgvector) + ClickHouse + MinIO. Streams flow over Redis:
`ingestion:queue → nlp:stage1:queue → router:queue → {llm:stage2:queue | assembler:queue}`.

---

## 1. Prerequisites

- **Docker** (with the `compose` plugin) — for the datastores.
- **[uv](https://docs.astral.sh/uv/)** — the Python package/venv manager. It reads
  `pyproject.toml`, creates `.venv`, and pins everything. (uv also fetches a
  matching Python ≥3.11 if you don't have one.)
- The repo checked out at `/home/bk/code/defense` (adjust `REPO` below if different).

Install the Python runtime deps (no GPU/ML libs needed in stub mode) and activate
the environment:

```bash
cd /home/bk/code/defense
uv sync                      # core runtime → .venv (use --extra dev to also get pytest/ruff)
source .venv/bin/activate    # so the bare `python`/`uvicorn` commands below use .venv
```

> All dependencies and their version constraints now live in `pyproject.toml`
> (e.g. `starlette<0.51`, which FastAPI 0.128 needs, and the `<8` pin on
> `prometheus-fastapi-instrumentator` whose 8.x pulls an incompatible Starlette).
> If you'd rather not activate the venv, prefix each command below with `uv run`
> (e.g. `uv run python -m services.workers.stage1_nlp`).

---

## 2. Start the infrastructure (Docker)

```bash
cd /home/bk/code/defense/deploy
docker compose up -d postgres redis clickhouse minio
```

Wait until Postgres and ClickHouse report healthy:

```bash
for i in $(seq 1 30); do
  pg=$(docker inspect -f '{{.State.Health.Status}}' deploy-postgres-1 2>/dev/null)
  ch=$(docker inspect -f '{{.State.Health.Status}}' deploy-clickhouse-1 2>/dev/null)
  echo "pg=$pg ch=$ch"; [ "$pg" = healthy ] && [ "$ch" = healthy ] && break; sleep 3
done
```

**Note on ports:** this compose file does **not** publish the Postgres/ClickHouse
host ports (host 5432/9000 are commonly taken). Host services therefore reach the
datastores by **container IP** (resolved dynamically in step 4), not via
`localhost`. Redis/MinIO are reachable either way.

Postgres auto-runs `deploy/init-db.sql` on first boot — this enables the
`pgvector` extension and creates `analysis_results.embedding` (the semantic-search
vector column that replaced Qdrant). Create the ClickHouse tables (not auto-created):

```bash
docker exec -i deploy-clickhouse-1 clickhouse-client \
  --user defense --password defense --database defense --multiquery \
  < /home/bk/code/defense/services/workers/assembler/clickhouse_init.sql
```

(The MinIO `defense` bucket is created automatically by the assembler on startup.)

---

## 3. Start the LLM backend (Ollama — real, no GPU required)

In stub mode the NLP runs without weights, but Stage-2 still calls a real
OpenAI-compatible chat endpoint. We use **Ollama**, which already has models
pulled on this device and serves an OpenAI-compatible API on `:11434`.

```bash
ollama serve >/tmp/ollama.log 2>&1 &     # skip if already running (systemd, etc.)
ollama list                              # confirm qwen2.5:7b and qwen3-vl:4b are present
curl -s http://localhost:11434/v1/models # -> {"object":"list","data":[...]}
```

The default role→model map (set in [§4](#4-configure-the-environment)) is:
`llm_a`/`llm_b` → `qwen2.5:7b`, `vlm` → `qwen3-vl:4b`. Any model from
`ollama list` works — point the `LLM_*_LOCAL_MODEL` vars at it (tag included).

> To use a cloud backend instead, skip this and set `LLM_BACKEND=groq` +
> `GROQ_API_KEY=…`. See [§7](#7-switching-the-llm-backend).

---

## 4. Configure the environment

Resolve the container IPs and export the service config. Run this in the shell
you'll launch the workers from (and `source` it again in each new shell):

```bash
cd /home/bk/code/defense
export REPO=/home/bk/code/defense
ipof() { docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "deploy-$1-1"; }

export DATABASE_URL="postgresql+asyncpg://defense:defense@$(ipof postgres):5432/defense"
export REDIS_URL="redis://$(ipof redis):6379/0"
export CLICKHOUSE_URL="clickhouse://defense:defense@$(ipof clickhouse):9000/defense"
export MINIO_ENDPOINT="http://$(ipof minio):9000"
export MINIO_ACCESS_KEY=minioadmin MINIO_SECRET_KEY=minioadmin MINIO_BUCKET=defense

export LLM_BACKEND=local
export LOCAL_LLM_BASE_URL=http://localhost:11434/v1   # Ollama OpenAI-compatible API
export LOCAL_LLM_API_KEY=ollama
export LLM_A_LOCAL_MODEL=qwen2.5:7b  # text classify/summarize/insight
export LLM_B_LOCAL_MODEL=qwen2.5:7b  # heavier text role (unused by this pipeline)
export VLM_LOCAL_MODEL=qwen3-vl:4b   # image posts (role=vlm)
export MODEL_STUB_MODE=true          # cheap NLP/vision stubs, no model weights
export JWT_SECRET=demo
export LOG_LEVEL=INFO
export PYTHONPATH=$REPO
```

This is the **minimal set** for the core pipeline (5 workers + API). For the
complete list of every variable the system reads — defaults, which service uses
it, and the extras needed for the agents/MCP stack — see
[§12](#12-environment-variable-reference-every-variable).

**Alternative — a `.env` file instead of `export`.** The services use
`pydantic-settings`, which auto-loads a `.env` file from the current working
directory. So you can drop the same keys (without the `export` keyword, and
using the lower-cased names is also accepted) into `/home/bk/code/defense/.env`
and launch the workers from the repo root — no need to re-`source` in every
shell. The container-IP values still have to be filled in, e.g.:

```bash
cd $REPO
cat > .env <<EOF
DATABASE_URL=postgresql+asyncpg://defense:defense@$(ipof postgres):5432/defense
REDIS_URL=redis://$(ipof redis):6379/0
CLICKHOUSE_URL=clickhouse://defense:defense@$(ipof clickhouse):9000/defense
MINIO_ENDPOINT=http://$(ipof minio):9000
MINIO_ACCESS_KEY=minioadmin
MINIO_SECRET_KEY=minioadmin
MINIO_BUCKET=defense
LLM_BACKEND=local
LOCAL_LLM_BASE_URL=http://localhost:11434/v1
LOCAL_LLM_API_KEY=ollama
LLM_A_LOCAL_MODEL=qwen2.5:7b
LLM_B_LOCAL_MODEL=qwen2.5:7b
VLM_LOCAL_MODEL=qwen3-vl:4b
MODEL_STUB_MODE=true
JWT_SECRET=demo
LOG_LEVEL=INFO
EOF
```

> Exported shell vars override `.env`. `PYTHONPATH` is _not_ read from `.env`
> (it's a Python interpreter setting) — still `export PYTHONPATH=$REPO` (or run
> with `PYTHONPATH=$REPO python -m …`).

---

## 5. Start the services and run the batch

Launch the five workers + the API (each in its own background process). They read
config from the env you exported above, so launch them from that shell:

```bash
cd $REPO
python -m services.workers.stage1_nlp        > /tmp/stage1.log    2>&1 &
python -m services.workers.router            > /tmp/router.log    2>&1 &
python -m services.workers.stage2_llm        > /tmp/stage2.log    2>&1 &
( cd $REPO/services/workers/assembler && python __main__.py ) > /tmp/assembler.log 2>&1 &
python -m services.ingestion                 > /tmp/ingestion.log 2>&1 &
( cd $REPO/services/api && uvicorn main:app --host 127.0.0.1 --port 8001 --log-level warning ) > /tmp/api.log 2>&1 &
```

> The API runs on **:8001** here (host :8000 is often taken). Health check:
> `curl -s http://127.0.0.1:8001/v1/health` → `{"status":"ok",...}`.

Push the 50 sample posts and watch them drain into Postgres:

```bash
cd $REPO
python - <<'PY'
import json, httpx
posts = json.load(open("posts_with_details.json"))
r = httpx.post("http://127.0.0.1:8001/v1/posts/upload",
               headers={"X-API-Key": "demo"}, json={"posts": posts}, timeout=120)
print("upload:", r.status_code, r.json().get("job_id"))
open("/tmp/jobid", "w").write(r.json()["job_id"])
PY

for i in $(seq 1 40); do
  n=$(docker exec deploy-postgres-1 psql -U defense -d defense -tAc "select count(*) from analysis_results")
  echo "analysis_results=$n"; [ "$n" = "50" ] && break; sleep 3
done
```

Auth note: any non-empty `X-API-Key` header is accepted in dev. A signed JWT
(HS256 with `JWT_SECRET`) also works.

---

## 6. Verify

```bash
# Row counts across every store (expect 50 / 50 / 50 / 50)
echo "PG analysis : $(docker exec deploy-postgres-1 psql -U defense -d defense -tAc 'select count(*) from analysis_results')"
echo "PG vectors  : $(docker exec deploy-postgres-1 psql -U defense -d defense -tAc 'select count(*) from analysis_results where embedding is not null')"
echo "CH events   : $(docker exec deploy-clickhouse-1 clickhouse-client --user defense --password defense -q 'select count() from defense.analysis_events')"
echo "MinIO objs  : (via API below)"

# (a) Validate every STORED canonical result against the output JSON Schema.
#     analysis_results.result is the source of truth (the assembler validates it
#     before persisting); the API GET returns a slimmer projection of it.
python - <<'PY'
import json, subprocess, sys; sys.path.insert(0, ".")
from libs.schemas import validate_output
out = subprocess.run(
    ["docker","exec","deploy-postgres-1","psql","-U","defense","-d","defense",
     "-tAc","select result::text from analysis_results"],
    capture_output=True, text=True).stdout
rows = [r for r in out.splitlines() if r.strip()]
bad = sum(0 if validate_output(json.loads(r))[0] else 1 for r in rows)
print(f"stored results: {len(rows)} | schema-invalid: {bad}")
PY

# (b) Read results back over the API (functional read check).
python - <<'PY'
import httpx
job = open("/tmp/jobid").read().strip()
d = httpx.get(f"http://127.0.0.1:8001/v1/analysis/{job}",
              params={"include": "results", "limit": 100},
              headers={"X-API-Key": "demo"}, timeout=60).json()
print("GET /v1/analysis results:", len(d.get("results") or []))
s = httpx.get("http://127.0.0.1:8001/v1/search", params={"q": "bangladesh"},
              headers={"X-API-Key": "demo"}, timeout=30).json()
print("GET /v1/search hits:", s.get("total"))
PY
```

Expected: 50 in each store, **0 schema-invalid** stored results, the API returns
50 result rows and search returns hits. Worker logs in `/tmp/*.log` should show no
`error` / `build_failed` / `persist_failed` lines.

Semantic search (`/v1/search?q=…&semantic=true`) also returns ranked hits now —
it runs a pgvector cosine query over `analysis_results.embedding`. In stub mode
the vectors are deterministic hashes (results are stable, not meaning-based);
real semantic quality needs `MODEL_STUB_MODE=false` + `uv sync --extra ml`.

The **chatbot** endpoint uses the live LLM backend (whatever §7's toggle points
at), so it's a quick end-to-end check that Ollama/Groq is reachable:

```bash
curl -s -X POST http://127.0.0.1:8001/v1/chat \
  -H "X-API-Key: demo" -H "Content-Type: application/json" \
  -d '{"message":"In one sentence, what is this platform for?"}' | python -m json.tool
# → {"reply":"…","backend":"local","model":"qwen2.5:7b","usage":{…}}
# Add "backend":"groq" to force Groq for one call; POST /v1/chat/stream streams tokens (SSE).
```

---

## 7. Switching the LLM backend

Stub mode only affects the **NLP/vision** models (`MODEL_STUB_MODE`). Stage-2 always
calls a real OpenAI-compatible endpoint. The default (§3/§4) is local Ollama; to
change it:

- **Launcher flags (`run_all.py`):** `--groq` starts the stack with Stage-2 +
  agents + Chat on Groq Cloud (needs `GROQ_API_KEY`; `--fast` is a synonym);
  `--ollama` forces local Ollama (the default) and clears any Groq override left
  in Redis by a prior run. Omit both for the local default.
- **Runtime toggle (no restart):** the dashboard's **LLM chip** (header) — or
  `curl -X PUT :8001/v1/config/llm -H 'X-API-Key: demo' -d '{"backend":"groq"}'` —
  sets a Redis override that Stage-2 picks up per message. `{"backend": null}`
  clears it. Groq still needs `GROQ_API_KEY` in the worker env.

- **Different Ollama model:** `export LLM_A_LOCAL_MODEL=gemma4:e4b` (or any tag from
  `ollama list`); `ollama pull <model>` first if it isn't listed.
- **Groq (cloud):** `export LLM_BACKEND=groq GROQ_API_KEY=sk-…`.
- **Self-hosted vLLM:** `export LLM_BACKEND=local LOCAL_LLM_BASE_URL=http://<vllm>:8000/v1`
  and set `LLM_*_LOCAL_MODEL` to the served model id.

To run the **real** NLP/vision models too, set `MODEL_STUB_MODE=false` and install
the ML deps with `uv sync --extra ml` (fastText, transformers, torch,
sentence-transformers, Pillow, etc.) — needs a GPU for reasonable throughput.

---

## 8. Teardown

```bash
# stop host services (workers + API). Leave `ollama serve` running.
pkill -f "[p]ython -m services" ; pkill -f "[p]ython __main__.py"
pkill -f "[u]vicorn main:app"

# stop infra (keep data volumes)
cd /home/bk/code/defense/deploy && docker compose down
# add -v to also delete the Postgres/ClickHouse/MinIO volumes
```

To reset state between runs without a full teardown:

```bash
docker exec deploy-redis-1 redis-cli FLUSHALL
docker exec deploy-postgres-1 psql -U defense -d defense -c "TRUNCATE analysis_results, posts CASCADE;"
docker exec deploy-clickhouse-1 clickhouse-client --user defense --password defense -q "TRUNCATE TABLE defense.analysis_events"
# then restart the five workers (they recreate the Redis consumer groups on boot)
```

---

## 9. Docker Compose build (Mode B)

The Mode B images **build now**. Every application service (and the three MCP
servers) builds with the **repo root as build context**
(`build: { context: .., dockerfile: services/<path>/Dockerfile }` in
`deploy/docker-compose.yml`), so the Dockerfiles can `COPY libs` and the
`services/`/`mcp/` packages. Each `CMD` matches the code's actual import style:
module-path entries (`python -m services.workers.router`,
`uvicorn services.agents.main:app`) for package-relative imports, and a
service-dir `WORKDIR` (`api`, `assembler`, `retrieval-mcp`, `ingest-mcp`) for
cwd-relative imports. The previously missing Dockerfiles (`ingestion`,
`worker_router`, `worker_assembler`) now exist.

Run it:

```bash
cd /home/bk/code/defense/deploy
docker compose up -d --build
```

Config (hostnames, creds, LLM settings) comes from `deploy/.env`, which every
app service loads via `env_file`. The API is published on host **:8000**
(health: `curl http://localhost:8000/v1/health`).

Notes:

- **stage1 is stub-mode-only by default.** Its image installs
  `services/workers/stage1_nlp/requirements-stub.txt` (no torch/transformers —
  multi-GB) and the compose file pins `MODEL_STUB_MODE=true`. For real ML,
  switch the Dockerfile to the full `requirements.txt` (plus
  `libgomp1`/`tesseract-ocr` apt packages — see the comment in that
  Dockerfile). `retrieval-mcp` likewise runs with `RETRIEVAL_MCP_STUB=true`
  (real vector search needs sentence-transformers in the image).
- **Ollama runs on the host**, not in compose. Containers reach it as
  `http://host.docker.internal:11434/v1` (`LOCAL_LLM_BASE_URL` in
  `deploy/.env`); on Linux the `worker_stage2` and `agents` services map that
  name via `extra_hosts: ["host.docker.internal:host-gateway"]`.
- The ClickHouse tables still need to be created once, exactly as in
  [§2](#2-start-the-infrastructure-docker).

---

## 10. Troubleshooting

- **`role "defense" does not exist` / connection refused on 5432, or "address
  already in use" on 9000/8000:** your host already runs Postgres/ClickHouse/etc.
  on those ports. That's exactly why infra ports aren't published and host services
  use container IPs (step 4). Keep the API on :8001.
- **Worker exits immediately on a log line** (`'PrintLogger' object has no
attribute 'name'`): you're on stale code — the logging fix is already in the repo;
  re-pull.
- **`asyncpg … can't subtract offset-naive and offset-aware datetimes`** right after
  a schema change: asyncpg cached the old column type — **restart the API/worker**
  so it opens fresh connections.
- **Posts upload returns 202 but nothing appears in Postgres:** check
  `/tmp/ingestion.log`. A `dedup_skipped` means the content hash was already seen —
  `redis-cli --scan --pattern 'dedup:*'` and delete those keys, or `FLUSHALL`.
- **The pipeline keeps processing posts on startup (or the Pipeline tab shows
  "in-flight/processing N") even though you didn't load any:** the Redis Stream
  queues persist across runs. Consumers read new entries (`XREADGROUP ">"`), so
  anything a previous run enqueued-but-never-delivered resumes the moment the
  workers restart; and any delivered-but-unacked entries stay in the group's
  pending list (no worker reclaims them) and show as "in-flight" forever. With
  `run_all.py` this is handled: **`--manual-load`** (and `--no-load`) reset each
  stage's consumer group so it has **0 backlog and 0 in-flight** — the stream
  entries are kept in Redis, the workers just start past them; **`--reset`** wipes
  everything. By hand, per stream, destroy + recreate the group at the tail, e.g.
  `redis-cli XGROUP DESTROY nlp:stage1:queue stage1-nlp-group` then
  `redis-cli XGROUP CREATE nlp:stage1:queue stage1-nlp-group '$' MKSTREAM`
  (repeat for `ingestion:queue`/`ingestion-workers`,
  `router:queue`/`router-workers`, `llm:stage2:queue`/`stage2-llm-workers`,
  `assembler:queue`/`assembler-group`), or `FLUSHALL` to clear everything.
- **`/v1/agents/query` returns 502/timeout:** the agents service or an MCP server
  isn't up — see [§11](#11-run-the-full-stack--agents--mcp--dashboard). Check
  `/tmp/agents.log` and `/tmp/*_mcp.log`. The agents service also needs the same
  `LLM_BACKEND`/`LOCAL_LLM_*` env as the workers (launch it from the §4 shell).
- **Dashboard shows "Cannot reach API" / CORS errors for `localhost:8000`:** the
  browser cached an old `app.js`. **Hard-refresh** (Ctrl-Shift-R). The dashboard
  targets `http://127.0.0.1:8001` by default (see
  [§11b](#11-run-the-full-stack--agents--mcp--dashboard)).

---

## 11. Run the full stack — agents + MCP + dashboard

Sections 2–6 run the **core pipeline** (ingest → analysis → stores → read API).
The optional pieces below add the **agent layer** (`/v1/agents/query`) and the
**dashboard** UI. Run these from the **same shell** as [§4](#4-configure-the-environment)
so they inherit `DATABASE_URL`, `REDIS_URL`, the `LLM_*` vars, and `PYTHONPATH`.

### 11a. MCP servers + agents service

The agents service calls three MCP servers over HTTP. Ports are fixed by the
agents client defaults: analytics **8110** (compose-internal 8100; host port 8110 since :8100 is often taken), retrieval **8101**, ingest **8102**,
agents **8010**. Export the loopback URLs, then launch each from its own dir
(they use cwd-relative imports, like the assembler):

```bash
cd $REPO
# Where the agents service finds each MCP server (host loopback)
export ANALYTICS_MCP_URL=http://127.0.0.1:8110
export RETRIEVAL_MCP_URL=http://127.0.0.1:8101
export INGEST_MCP_URL=http://127.0.0.1:8102
export AGENTS_SERVICE_URL=http://127.0.0.1:8010   # the API proxies /v1/agents here

# analytics-mcp talks to ClickHouse via its own CH_* vars (NOT CLICKHOUSE_URL)
export CLICKHOUSE_HOST=$(ipof clickhouse) CLICKHOUSE_PORT=9000 \
       CLICKHOUSE_DB=defense CLICKHOUSE_USER=defense CLICKHOUSE_PASSWORD=defense

# Dev: stub modes so the MCP servers need no ML deps / embedding model.
# (retrieval-mcp's real path queries pgvector and needs sentence-transformers.)
export ANALYTICS_MCP_STUB=true RETRIEVAL_MCP_STUB=true

# Each MCP server is a FastMCP app exposing the streamable-HTTP endpoint at /mcp.
# analytics_mcp launches by module path from $REPO:
uvicorn mcp_servers.analytics_mcp.server:app --host 127.0.0.1 --port 8110 --log-level warning > /tmp/analytics_mcp.log 2>&1 &
( cd $REPO/mcp_servers/retrieval_mcp && uvicorn server:app --host 127.0.0.1 --port 8101 --log-level warning ) > /tmp/retrieval_mcp.log 2>&1 &
( cd $REPO/mcp_servers/ingest_mcp    && uvicorn server:app --host 127.0.0.1 --port 8102 --log-level warning ) > /tmp/ingest_mcp.log 2>&1 &
# agents uses package-relative imports — module path from $REPO:
uvicorn services.agents.main:app --host 127.0.0.1 --port 8010 --log-level warning > /tmp/agents.log 2>&1 &
```

Health-check each, then ask an agent a question through the API:

```bash
for p in 8110 8101 8102 8010; do echo -n ":$p "; curl -s http://127.0.0.1:$p/health; echo; done

curl -s -X POST http://127.0.0.1:8001/v1/agents/query \
  -H "X-API-Key: demo" -H "Content-Type: application/json" \
  -d '{"agent_type":"analyst","query":"What are people saying about Bangladesh politics?"}' \
  | python -m json.tool
```

> `agent_type` is one of `analyst`, `coverage`, `alerting`. The agents service
> runs the LLM agentic loop, so it uses the §4 `LLM_BACKEND=local` + Ollama env.
> To exercise **real** pgvector semantic search via retrieval-mcp, drop
> `RETRIEVAL_MCP_STUB`, run `uv sync --extra ml` (which includes
> sentence-transformers), and make the embedder output match `EMBEDDING_DIM`
> (see [§12](#12-environment-variable-reference-every-variable)).

### 11b. Dashboard

The dashboard is static HTML/JS. It already targets the dev API on
`http://127.0.0.1:8001` by default (`dashboard/app.js`), so just serve it:

```bash
cd $REPO/dashboard
python -m http.server 8080    # open http://127.0.0.1:8080
```

To point it at a different API (e.g. Mode B on :8000), set `window.API_BASE` in
`index.html` before the `app.js` `<script>` tag — there's a commented example
near the top of the file. If you change `app.js`, hard-refresh the browser.

Log in with any non-empty API key (e.g. `demo`) — see the auth note in [§5](#5-start-the-services-and-run-the-batch).

To stop the full-stack processes, the [§8](#8-teardown) `pkill` lines already
match the workers/API; add `pkill -f "[u]vicorn server:app"` and
`pkill -f "[u]vicorn main:app"` for the MCP/agents processes (and `pkill -f
"http.server 8080"` for the dashboard).

---

## 12. Environment variable reference (every variable)

How to set: **export** them in the launch shell (§4), or put them in a `.env`
file at the repo root (§4 "Alternative"). Exported vars win over `.env`.
"Required" = no usable default for a real run; "Default" = what the code falls
back to if unset.

### Core infrastructure — needed by API + all workers + assembler

| Variable           | Purpose                                                     | Required?               | Example / default                                           |
| ------------------ | ----------------------------------------------------------- | ----------------------- | ----------------------------------------------------------- |
| `DATABASE_URL`     | Postgres DSN (asyncpg). Also holds the pgvector embeddings. | **required**            | `postgresql+asyncpg://defense:defense@<pg-ip>:5432/defense` |
| `REDIS_URL`        | Redis streams + pub/sub + caches.                           | **required**            | `redis://<redis-ip>:6379/0`                                 |
| `CLICKHOUSE_URL`   | ClickHouse DSN (assembler analytics writes).                | **required**            | `clickhouse://defense:defense@<ch-ip>:9000/defense`         |
| `MINIO_ENDPOINT`   | MinIO/S3 endpoint URL (raw-result blobs).                   | **required**            | `http://<minio-ip>:9000`                                    |
| `MINIO_ACCESS_KEY` | MinIO access key.                                           | **required**            | `minioadmin`                                                |
| `MINIO_SECRET_KEY` | MinIO secret key.                                           | **required**            | `minioadmin`                                                |
| `MINIO_BUCKET`     | Object bucket (auto-created).                               | default `defense`       | `defense`                                                   |
| `PYTHONPATH`       | Must include the repo root so `libs`/`services` import.     | **required (host run)** | `/home/bk/code/defense`                                     |
| `JWT_SECRET`       | HS256 secret for JWT auth.                                  | default `change-me`     | `demo`                                                      |
| `LOG_LEVEL`        | Log verbosity.                                              | default `INFO`          | `INFO`                                                      |

### Vector search (pgvector)

| Variable        | Purpose                                                                                                | Required?     | Example / default |
| --------------- | ------------------------------------------------------------------------------------------------------ | ------------- | ----------------- |
| `EMBEDDING_DIM` | Dim of the `analysis_results.embedding` column; must match the DB column **and** the Stage-1 embedder. | default `768` | `768`             |

### LLM / VLM backend (Stage-2 + agents + chat)

| Variable             | Purpose                                             | Required?                                           | Example / default           |
| -------------------- | --------------------------------------------------- | --------------------------------------------------- | --------------------------- |
| `LLM_BACKEND`        | `local` (Ollama/vLLM) or `groq`.                    | default `local` (matches docs); groq needs API key  | `local`                     |
| `LOCAL_LLM_BASE_URL` | OpenAI-compatible base URL for the local backend.   | default `http://localhost:11434/v1` (Ollama)        | `http://localhost:11434/v1` |
| `LOCAL_LLM_API_KEY`  | Key for the local endpoint (any string for Ollama). | default `ollama`                                    | `ollama`                    |
| `LLM_A_LOCAL_MODEL`  | Text model — classify / summarize / insight.        | default `qwen2.5:7b`                                | `qwen2.5:7b`                |
| `LLM_B_LOCAL_MODEL`  | Heavier text role (unused by this pipeline).        | default `qwen2.5:7b`                                | `qwen2.5:7b`                |
| `VLM_LOCAL_MODEL`    | Vision model for image posts (`role=vlm`).          | default `qwen3-vl:4b`                               | `qwen3-vl:4b`               |
| `GROQ_API_KEY`       | Groq Cloud key — only when `LLM_BACKEND=groq`.      | required for groq                                   | `gsk_…`                     |
| `LLM_A_GROQ_MODEL`   | Groq text-A model.                                  | default `llama-3.1-8b-instant`                      | —                           |
| `LLM_B_GROQ_MODEL`   | Groq text-B model.                                  | default `llama-3.3-70b-versatile`                   | —                           |
| `VLM_GROQ_MODEL`     | Groq vision model.                                  | default `meta-llama/llama-4-scout-17b-16e-instruct` | —                           |

> The LLM client reads the `LOCAL_LLM_*` and `LLM_*_LOCAL_MODEL` vars **directly**
> with the Ollama defaults above. Set them explicitly (as §4 does) rather than
> relying on defaults if you've changed models.

### Stage-1 NLP / vision models

| Variable          | Purpose                                                                                     | Required?             | Example / default |
| ----------------- | ------------------------------------------------------------------------------------------- | --------------------- | ----------------- |
| `MODEL_STUB_MODE` | `true` = deterministic heuristic stubs (no weights, no GPU); `false` = load real ML models. | default `true`        | `true`            |
| `EMBEDDING_MODEL` | Real sentence-embedding model (only when `MODEL_STUB_MODE=false`); must output `EMBEDDING_DIM` dims. Shared by Stage-1, the API and retrieval-mcp via `libs/embeddings.py`. | default `paraphrase-multilingual-mpnet-base-v2` (768-dim) | — |
| `SENTIMENT_MODEL` | Default sentiment checkpoint (XLM-R) — the `xlmr` option / fallback for every language. Must have a 3-class sentiment head. | default `cardiffnlp/twitter-xlm-roberta-base-sentiment` | — |
| `BANGLABERT_SENTIMENT_MODEL` / `BANGLISHBERT_SENTIMENT_MODEL` / `MBERT_SENTIMENT_MODEL` | Sentiment-**fine-tuned** checkpoints that activate the BanglaBERT / BanglishBERT / mBERT options. Unset ⇒ the option stays unavailable and the language router falls back to XLM-R. Auto-route: Bangla-script→BanglaBERT, Banglish/mixed→BanglishBERT, else→XLM-R. Force one at runtime via `PUT /v1/config/nlp` (dashboard "NLP" chip). | unset | — |

### Agents + MCP stack (only for §11)

| Variable                                                    | Purpose                                                                          | Required?                                  | Example / default                 |
| ----------------------------------------------------------- | -------------------------------------------------------------------------------- | ------------------------------------------ | --------------------------------- |
| `AGENTS_SERVICE_URL`                                        | API → agents service.                                                            | default `http://agents:8010`               | `http://127.0.0.1:8010`           |
| `ANALYTICS_MCP_URL`                                         | agents → analytics-mcp.                                                          | default `http://analytics-mcp:8100`        | `http://127.0.0.1:8110`           |
| `RETRIEVAL_MCP_URL`                                         | agents → retrieval-mcp.                                                          | default `http://retrieval-mcp:8101`        | `http://127.0.0.1:8101`           |
| `INGEST_MCP_URL`                                            | agents → ingest-mcp.                                                             | default `http://ingest-mcp:8102`           | `http://127.0.0.1:8102`           |
| `AGENT_SYNC_TIMEOUT`                                        | Seconds the API waits for a sync agent result.                                   | default `28`                               | `28`                              |
| `PUBLIC_BASE_URL`                                           | Base URL the agents service uses when building links.                            | default empty                              | `http://127.0.0.1:8010`           |
| `RETRIEVAL_MCP_STUB`                                        | retrieval-mcp: skip vector search, return by recency.                            | default `false`                            | `true`                            |
| `ANALYTICS_MCP_STUB`                                        | analytics-mcp: return synthetic analytics.                                       | default `false`                            | `true`                            |
| `MODEL_STUB_MODE` / `EMBEDDING_MODEL`                       | retrieval-mcp query embedding now uses the shared `libs/embeddings.py` (same vars as Stage-1 above) — `SENTENCE_TRANSFORMER_MODEL` is gone. | see Stage-1 section                        | —                                 |
| `CLICKHOUSE_HOST`                                           | analytics-mcp ClickHouse host (note: **not** `CLICKHOUSE_URL`).                  | default `localhost`                        | `<ch-ip>`                         |
| `CLICKHOUSE_PORT`                                           | analytics-mcp ClickHouse native TCP port.                                        | default `9000`                             | `9000`                            |
| `CLICKHOUSE_DB` / `CLICKHOUSE_USER` / `CLICKHOUSE_PASSWORD` | analytics-mcp ClickHouse db/creds.                                               | defaults `defense` / `default` / _(empty)_ | `defense` / `defense` / `defense` |
| `PORT`                                                      | Bind port for an MCP/agents uvicorn process (overrides the per-service default). | per-service                                | `8101`                            |

### Upstream source (optional — ingest-mcp pulling new data)

| Variable           | Purpose                                             | Required?     | Example / default    |
| ------------------ | --------------------------------------------------- | ------------- | -------------------- |
| `UPSTREAM_API_URL` | Upstream data-source API the ingest-mcp pulls from. | default empty | `http://upstream/v1` |
| `UPSTREAM_API_KEY` | Upstream API key.                                   | default empty | —                    |

> `HOSTNAME` is read by the workers as their stream consumer name; it's set
> automatically by the OS/container — don't set it yourself.

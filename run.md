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
> (e.g. `uv run python -m defense.services.workers.stage1_nlp`).

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
  < /home/bk/code/defense/src/defense/services/workers/assembler/clickhouse_init.sql
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
python -m defense.services.workers.stage1_nlp        > /tmp/stage1.log    2>&1 &
python -m defense.services.workers.router            > /tmp/router.log    2>&1 &
python -m defense.services.workers.stage2_llm        > /tmp/stage2.log    2>&1 &
( cd src/defense/services/workers/assembler && python __main__.py ) > /tmp/assembler.log 2>&1 &
python -m defense.services.ingestion                 > /tmp/ingestion.log 2>&1 &
python -m uvicorn defense.services.api.main:app --host 127.0.0.1 --port 8001 --log-level warning > /tmp/api.log 2>&1 &
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

Auth note: any non-empty `X-API-Key` header is accepted in dev (real key storage
is still open — PROJECT_ASSESSMENT §5.6). A signed JWT (HS256 with `JWT_SECRET`)
also works, on **every** transport: a credential that looks like a JWT is
verified as one whether it arrives in `Authorization`, `X-API-Key`, or the
`?api_key=` query parameter that `EventSource` needs for SSE. It used to be that
only the `Authorization` header was parsed as a token, so an **expired or forged**
token authenticated on all four SSE streams. Expect a 401 on a stale token now —
that is the fix working.

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
  sets a Redis override. `{"backend": null}` clears it. Groq still needs
  `GROQ_API_KEY` in the worker env.

  **Selecting `groq` requires an admin role** (`admin`/`owner`/`operator`), because
  this switch is global — it routes *every* tenant's analysis off-box. A caller
  whose own tenant is `privacy_locked` is refused outright. Locked tenants are
  unaffected by whatever the switch says: the API resolves each job's backend
  (request > toggle > env), applies the lock where the tenant is known, and stamps
  the decision into the job envelope, which both stages honour. Before
  PROJECT_ASSESSMENT §13.5 this endpoint had no policy or role check and the
  workers read the global key directly, so a locked tenant's content followed the
  toggle to Groq.

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
(`build: { context: .., dockerfile: src/defense/services/<path>/Dockerfile }` in
`deploy/docker-compose.yml`), so the Dockerfiles can `COPY libs` and the
`src/defense/services/`/`mcp/` packages. Each `CMD` matches the code's actual import style:
module-path entries (`python -m defense.services.workers.router`,
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
  `src/defense/services/workers/stage1_nlp/requirements-stub.txt` (no torch/transformers —
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
( cd $REPO/src/defense/mcp_servers/retrieval_mcp && uvicorn server:app --host 127.0.0.1 --port 8101 --log-level warning ) > /tmp/retrieval_mcp.log 2>&1 &
( cd $REPO/src/defense/mcp_servers/ingest_mcp    && uvicorn server:app --host 127.0.0.1 --port 8102 --log-level warning ) > /tmp/ingest_mcp.log 2>&1 &
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
npm install && npm run dev -- --port 8080    # open http://127.0.0.1:8080
```

To point it at a different API (e.g. Mode B on :8000), set `window.API_BASE` in
`index.html` before the `app.js` `<script>` tag — there's a commented example
near the top of the file. If you change `app.js`, hard-refresh the browser.

Log in with any non-empty API key (e.g. `demo`) — see the auth note in [§5](#5-start-the-services-and-run-the-batch).

To stop the full-stack processes, the [§8](#8-teardown) `pkill` lines already
match the workers/API; add `pkill -f "[u]vicorn server:app"` and
`pkill -f "[u]vicorn main:app"` for the MCP/agents processes (and `pkill -f
"vite"` for the dashboard).

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
| `MINIO_ENDPOINT`   | MinIO/S3 endpoint URL (raw-result blobs, and the base for resolving relative `photoUrls`). **Must include the scheme** — the root `.env` shipped `minio:9000` with none, and httpx raised `UnsupportedProtocol` before a byte was fetched, which the vision path then reported as a neutral verdict. A missing scheme is now logged and `http://` assumed. | **required**            | `http://<minio-ip>:9000`                                    |
| `MINIO_ACCESS_KEY` | MinIO access key.                                           | **required**            | `minioadmin`                                                |
| `MINIO_SECRET_KEY` | MinIO secret key.                                           | **required**            | `minioadmin`                                                |
| `MINIO_BUCKET`     | Object bucket (auto-created).                               | default `defense`       | `defense`                                                   |
| `PYTHONPATH`       | Must include the repo root so `libs`/`services` import.     | **required (host run)** | `/home/bk/code/defense`                                     |
| `JWT_SECRET`       | HS256 secret, read per call by **both** the issuer and the verifier via `src/defense/libs/common/config.py` (so it can be rotated without a restart, and issuer/verifier cannot drift). A fingerprint is logged at boot in each. | default `change-me`     | `demo`                                                      |
| `APP_ENV`          | Deployment environment. Outside `dev`/`test`/`ci` the API **refuses to start** while `JWT_SECRET` is still a placeholder that ships in this repo. | default `dev`           | `dev`                                                       |
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
| `STAGE1_LOCAL_MODEL` | Stage-1 Fast-NLP model (`role=stage1`).             | default `gemma3:4b`                                 | `gemma3:4b`                 |
| `STAGE2_LOCAL_MODEL` | Stage-2 **classification** (post-type, insight, comment stance). | default `qwen2.5:7b`                   | `qwen2.5:7b`                |
| `SUMMARY_LOCAL_MODEL`| Stage-2 **summarization** (post + comment summary). Separate from `stage2` because classification wants a cheap constrained model and summarization wants a fluent one. Pick it with `python -m eval.bakeoff_summary`. | default `qwen2.5:7b` | `gemma4:26b`                |
| `VLM_LOCAL_MODEL`    | Vision model for image posts (`role=vlm`). Only used when an image is actually fetched — currently never (§5.2). | default `qwen3-vl:4b`     | `qwen3-vl:4b`               |
| `GROQ_API_KEY`       | Groq Cloud key — only when `LLM_BACKEND=groq`.      | required for groq                                   | `gsk_…`                     |
| `SUMMARY_GROQ_MODEL` | Groq summarization model.                           | default `llama-3.3-70b-versatile`                   | —                           |
| `LLM_A_GROQ_MODEL`   | Groq text-A model.                                  | default `llama-3.1-8b-instant`                      | —                           |
| `LLM_B_GROQ_MODEL`   | Groq text-B model.                                  | default `llama-3.3-70b-versatile`                   | —                           |
| `VLM_GROQ_MODEL`     | Groq vision model.                                  | default `meta-llama/llama-4-scout-17b-16e-instruct` | —                           |

> The LLM client reads the `LOCAL_LLM_*` and `LLM_*_LOCAL_MODEL` vars **directly**
> with the Ollama defaults above. Set them explicitly (as §4 does) rather than
> relying on defaults if you've changed models.

### Comment LLM coverage & cost (the dominant runtime knob)

85–96% of LLM calls are **comment-level**, so these matter more for wall-clock
and cost than the router threshold does ([PROJECT_ASSESSMENT.md](PROJECT_ASSESSMENT.md) §6.8).

| Variable | Purpose | Required? | Example / default |
| --- | --- | --- | --- |
| `ROUTER_COMMENT_TOP_N` | **How much of the thread Stage 2 analyses.** `0` (default) = **every comment with text**; every voter reads that same set — the seven cheap heads, the dedup cache and the LLM. A positive N keeps only the N most-reacted comments and bounds per-post cost at `ceil(N / COMMENT_STANCE_BATCH)` LLM calls plus ~0.92 s × N of classifier CPU, however large the thread. Uncapped, a 2,857-comment post is ~44 min of CPU and ~115 stance batches — set a cap for a quick demo, and quote it when you do. | default `0` | `100` for a fast local demo |
| `STAGE1_LLM_COMMENT_MAX` | Cap on comments per post given a Stage-1 LLM label. **0 = no cap** (every non-emoji comment). Stage 1 is *not* bounded by `ROUTER_COMMENT_TOP_N` — it runs before the router. | default `0` | `60` for a fast demo |
| `COMMENT_STANCE_MAX_PER_POST` | A second, tighter cap on the LLM stance pass **inside** the router's set. Leave at `0`: a positive value gives the LLM fewer comments than the seven cheap heads got, which is the hole in the per-comment comparison that `ROUTER_COMMENT_TOP_N` exists to avoid. | default `0` | `0` |
| `STAGE1_LLM_BATCH` | Comments per LLM call. Bigger = fewer calls, longer prompts, coarser retries. | default `25` | `25` |
| `STAGE1_LLM_CONCURRENCY` | Batches in flight per post. The loop used to be sequential, which is why the caps existed. | default `3` | `3` |
| `COMMENT_STANCE_BATCH` / `COMMENT_STANCE_CONCURRENCY` | The same two knobs for Stage 2. | default `25` / `3` | — |
| `COMMENT_LAUGH_SENTIMENT` | How 🤣😂😆 score: `negative` (mockery — right for this corpus), `positive`, or `neutral`. Drives **both** the sentiment and emotion tables, which used to disagree. | default `negative` | `negative` |
| `STAGE1_OCR_SENTIMENT` | Sentiment-analyse OCR text on null-caption image posts. Off while no image bytes are reachable (§5.2). | default `false` | `false` |

> **At the defaults, everything with text is analysed by everything.** Stage 1
> labels every non-emoji comment, and the Stage-2 ensemble (7 heads + LLM) reads
> every comment with text. The two remaining gaps are inherent, not caps:
> emoji-only comments and bare links have nothing for a model to read, so they end
> up `uncertain` at zero voters (Stage 1's keyword label does not vote), and they
> are counted in `reaction_only`.
>
> **If you set a positive `ROUTER_COMMENT_TOP_N`, quote it.** The result then says
> what was skipped — `comment_analysis.ensemble.analysed` / `not_analysed`,
> `llm_share` against the whole thread beside `llm_share_analysed` against the
> selection, and `stage2_selected: false` on every comment no model read — and the
> post-level `sentiment_breakdown` fills with `uncertain` in proportion.

### The seven Stage-2 comment classifiers

The cheap half of the ensemble. Each slot is a checkpoint; the **voter name** it
answers as is fixed in code (`Settings.stage2_classifier_names`) because the names
are keys in `parallel_labels` and renaming one would orphan stored rows and the
dashboard column that reads them. Set a slot to `""` to drop that voter.

| Variable | Voter | Default checkpoint |
| --- | --- | --- |
| `STAGE2_CLASSIFIER_1` | `xlmr` | `tabularisai/multilingual-sentiment-analysis` (DistilBERT-multilingual, 5-class) |
| `STAGE2_CLASSIFIER_2` | `distilbert` | `lxyuan/distilbert-base-multilingual-cased-sentiments-student` |
| `STAGE2_CLASSIFIER_3` | `twitter_xlmr` | `cardiffnlp/twitter-xlm-roberta-base-sentiment-multilingual` |
| `STAGE2_CLASSIFIER_4` | `banglabert` | `ADn-001/banglabert-sentnob-sentiment` |
| `STAGE2_CLASSIFIER_5` | `bengali_sentiment_bert` | `ahs95/banglabert-sentiment-analysis` |
| `STAGE2_CLASSIFIER_6` | `mbert` | `nlptown/bert-base-multilingual-uncased-sentiment` (1–5 stars) |
| `STAGE2_CLASSIFIER_7` | `modernbert` | `clapAI/modernBERT-base-multilingual-sentiment` |
| `STAGE2_CLASSIFIERS_ENABLED` | — | `true`. Off leaves the **LLM as the only labeller** — Stage 1's keyword label does not vote — so every comment the LLM misses reports `uncertain` at zero voters. |
| `STAGE2_CLASSIFIER_DEVICE` | — | `auto` (GPU then CPU), `cpu`, or `cuda`. **`cpu` is right when the GPU is serving the LLM** — a 4 GB card running qwen2.5:7b has ~285 MB left, which fits one head, not seven. |

**Fetch them once, and check they vote:**

```bash
uv run python deploy/prefetch_classifiers.py           # download + verify
uv run python deploy/prefetch_classifiers.py --check   # verify only
```

`MODEL_STUB_MODE=true` means *download nothing*, so until you run this, Stage 2
skips every uncached head and logs
`stage2_cheap_voters voted=0 declared=7 silent=[…]` at WARNING. That log line is
the one to read before quoting an agreement number: a post labelled by 2 of 7
voters is a degraded run, and `unanimous_share` computed over the survivors cannot
tell you that by itself. The script also runs each head on a Bangla/English/
Banglish probe, because a checkpoint can download perfectly and still never cast a
vote — a base encoder emits `LABEL_0`/`LABEL_1`, which maps to nothing, and four of
the roster's original five entries were exactly that or absent from the Hub.

### Auth & tenancy

| Variable | Purpose | Required? | Example / default |
| --- | --- | --- | --- |
| `APP_ENV` | Environment name. Outside `dev`/`test`/`ci` the API refuses a placeholder `JWT_SECRET`, rejects unknown API keys, and refuses unverified logins — i.e. it fails **closed**. | default `dev` | `dev` |
| `JWT_EXPIRE_HOURS` | Token lifetime. Shortened from 24 to 1 now that `/v1/auth/refresh` exists. | default `1` | `1` |
| `ALLOW_UNKNOWN_API_KEYS` | Accept any non-empty API key (the old MVP behaviour). Defaults ON only for a dev `APP_ENV`. | default: on in dev | `false` |
| `ALLOW_ANY_LOGIN` | Issue a token without verifying credentials when no `users` rows exist. Defaults ON only for a dev `APP_ENV`, and logs loudly every time it fires. | default: on in dev | `false` |
| `SSE_TICKET_TTL` | Lifetime of a single-use `EventSource` ticket, in seconds. | default `60` | `60` |

> **To leave dev behaviour behind**, seed the tables and set `APP_ENV`:
>
> ```sql
> -- api_keys stores only the SHA-256 hash; see libs.auth.generate_api_key()
> INSERT INTO api_keys (key_hash, tenant_id, label) VALUES ('<sha256>', 'acme', 'dashboard');
> -- users stores pbkdf2_sha256$…; see libs.auth.hash_password()
> INSERT INTO users (username, password_hash, tenant_id) VALUES ('alice', '<hash>', 'acme');
> ```
>
> `tenant_id` then comes from those rows rather than from anything a client sends,
> which is what makes the privacy-locked-tenant policy enforceable.

### Target stance (watchlist)

| Variable | Purpose | Required? | Example / default |
| --- | --- | --- | --- |
| `STANCE_TARGETS_FILE` | Watchlist path. **Absent file = feature off**, and the pipeline behaves exactly as before. A malformed file fails loudly at load. | default `config/stance_targets.yml` | — |

See [stance_targets.md](stance_targets.md). The shipped file contains one
placeholder entity under `neutral:` — its contents are an editorial choice, and
the watchlist is a **stated bias model**, not a measurement.

### Embeddings & semantic search

| Variable | Purpose | Required? | Example / default |
| --- | --- | --- | --- |
| `EMBEDDING_ALLOW_STUB` | Permit persisting the deterministic hash-seeded stub vector. **Set `false` before demoing semantic search or cluster reports** — kNN over stub rows returns arbitrary neighbours with scores that look exactly as plausible as real ones. Rows are marked `embedding_is_stub` either way. | default `true` | `false` |

`embedding_is_stub` is **reported by Stage 1 and carried** to the column, not
inferred downstream: the stub is the same `EMBEDDING_DIM` size as a real vector,
so a dimension check cannot tell them apart — and that is exactly what the
persistence layer used to do, recording every stub in the default configuration
as a real semantic vector (PROJECT_ASSESSMENT §13.2).

### Near-duplicate reuse

| Variable | Purpose | Required? | Example / default |
| --- | --- | --- | --- |
| `NEAR_DUP_DEDUP` | Reuse a prior analysis for a post whose caption is within `NEAR_DUP_THRESHOLD` cosine of one already analysed, skipping Stage 1 and Stage 2. | default `true` | `false` |
| `NEAR_DUP_THRESHOLD` | Cosine similarity required to count as a near-duplicate. | default `0.97` | `0.99` |

In stub mode only *identical* captions match — identical text hashes to an
identical vector, so cosine is exactly 1.0 and any repost takes this path. The
result is **composed, not copied**: identity, engagement, reactions and
timestamps come from the new post, only the post-level analysis is reused,
`processing.reused_from` records the source, and the new post's **comment thread
is reported unanalysed** rather than inheriting labels for comments nobody read.
Set `NEAR_DUP_DEDUP=false` for any run whose per-post *latency* numbers you intend
to quote — a reused post does no stage work.

### Usage & cost counters

| Variable | Purpose | Required? | Example / default |
| --- | --- | --- | --- |
| `LLM_USAGE_TRACKING_DISABLED` | Stop writing the Redis usage counters `GET /v1/usage` reads. For offline evals and benchmarks that must not pollute the cost figures. | default off | `1` |

The counters are written by `LLMClient` itself, so **every** caller is counted —
both pipeline stages, `/v1/chat`, report narratives and cluster summaries, and
agent runs. They were written by the Stage-2 worker alone until §13.4, which left
five callers spending tokens nothing counted. `lane_split` reports five lanes
(`post`, `comment`, `stage1`, `interactive`, `agent`) and `pipeline_tokens` sums
the first three, so a chatbot session cannot inflate the per-post cost figure.

### Token budgets & truncation

| Variable | Purpose | Required? | Example / default |
| --- | --- | --- | --- |
| `SUMMARY_MAX_TOKENS` | Post-summary ceiling. Bangla costs far more tokens per character than English, so a low ceiling truncates Bangla and spares English. | default `1024` | `1024` |
| `COMMENT_SUMMARY_MAX_TOKENS` | Comment-summary ceiling. | default `640` | `640` |
| `INSIGHT_MAX_TOKENS` | Insight-task ceiling. | default `768` | `768` |
| `LLM_MAX_CONTINUATIONS` | How many times a reply that stopped at the ceiling is re-asked and concatenated. Whatever state it ends in is reported as `truncated`, flagged in the output, and never cached. | default `2` | `2` |
| `LLM_CACHE_DISABLED` | Bypass the 7-day Stage-2 response cache. **Set this for evaluation runs** so a model comparison cannot read back cached answers. | unset | `1` |

### Stage-1 NLP / vision models

| Variable          | Purpose                                                                                     | Required?             | Example / default |
| ----------------- | ------------------------------------------------------------------------------------------- | --------------------- | ----------------- |
| `MODEL_STUB_MODE` | `true` = deterministic heuristic stubs (no weights, no GPU); `false` = load real ML models. | default `true`        | `true`            |
| `EMBEDDING_MODEL` | Real sentence-embedding model (only when `MODEL_STUB_MODE=false`); must output `EMBEDDING_DIM` dims. Shared by Stage-1, the API and retrieval-mcp via `src/defense/libs/embeddings.py`. | default `paraphrase-multilingual-mpnet-base-v2` (768-dim) | — |
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
| `MODEL_STUB_MODE` / `EMBEDDING_MODEL`                       | retrieval-mcp query embedding now uses the shared `src/defense/libs/embeddings.py` (same vars as Stage-1 above) — `SENTENCE_TRANSFORMER_MODEL` is gone. | see Stage-1 section                        | —                                 |
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

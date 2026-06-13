# easy_run.md — one-command quickstart (with the dashboard UI)

Run the whole pipeline, load the 50 sample posts, and open the **dashboard in
your browser** — with a single command. Local dev, no GPU: Stage-1 NLP runs on the
**gemma3:4b** LLM and Stage-2 on **qwen2.5:7b** (both via Ollama); embeddings and
vision stay stubbed. For the deep reference (every env var, scaling, full
troubleshooting) see [run.md](run.md).

Pipeline: `API → ingestion → stage1 NLP → router → stage2 LLM → assembler`
→ Postgres (+pgvector) + ClickHouse + MinIO. The **dashboard** is a static web UI
that talks only to the API.

What ends up running:

| Piece                                        | Where                             | Needed for the UI?                              |
| -------------------------------------------- | --------------------------------- | ----------------------------------------------- |
| Datastores (Postgres/Redis/ClickHouse/MinIO) | Docker                            | ✅ yes                                          |
| 5 workers + API                              | host, API on **:8001**            | ✅ yes                                          |
| Ollama (Stage-1 + Stage-2 LLMs + VLM)        | host, **:11434**                  | ✅ yes                                          |
| **Dashboard**                                | host, **:8080** → open in browser | ✅ this is the UI                               |
| Agents + 3 MCP servers                       | host, :8010 / :8110, :8101–8102   | ⛔ optional ([§4](#4-optional-the-agent-layer)) |

---

## 1. Prerequisites (one-time)

- **Docker** (with `compose`)
- **[uv](https://docs.astral.sh/uv/)** — `curl -LsSf https://astral.sh/uv/install.sh | sh`
- **[Ollama](https://ollama.com/)** with the three models pulled — Stage 1 and
  Stage 2 run on **different** models:

  ```bash
  ollama pull gemma3:4b      # Stage-1 Fast NLP (sentiment/emotion/topics/… + comments)
  ollama pull qwen2.5:7b     # Stage-2 summary / insight / comment stance
  ollama pull qwen3-vl:4b    # VLM — image-grounded summaries
  ```

Repo assumed at `/home/bk/code/defense`.

---

## 2. Run it — one command 🚀

```bash
cd /home/bk/code/defense
uv run run_all.py
```

That's it. The script brings everything up, waits for it to be healthy, loads the
50 posts, serves the UI, and prints the URLs. **Leave it running** — press
**Ctrl-C** when you're done and it stops everything it started.

> First run is slower than you might expect: Stage-1 NLP (`gemma3:4b`) and Stage-2
> (`qwen2.5:7b`) make real Ollama calls per post **on CPU**, so analysing the 50
> posts can take several minutes (the model weights also load on first call). The
> dashboard is usable as soon as the first results land. To go faster, point the
> LLM backend at Groq from the dashboard, or set `STAGE1_LLM=false` for stub NLP.

<details>
<summary>What it does, in order</summary>

1. `uv sync` — installs deps into `.venv`
2. `docker compose up` the datastores → waits until healthy → creates the ClickHouse tables
3. checks **Ollama** (starts it if installed but not running)
4. launches the **5 workers + API** (on :8001), waits for `/v1/health`
5. uploads the **50 sample posts** and waits for all 50 to be analysed
6. **serves the dashboard on :8080** (it already targets the dev API on :8001)

</details>

**Flags:**

```bash
uv run run_all.py --with-agents   # also start the agents + MCP layer (§4)
uv run run_all.py --reset         # wipe prior data, then reload the 50 posts
uv run run_all.py --no-load       # skip pushing the sample posts
uv run run_all.py --no-dashboard  # don't serve the UI
uv run run_all.py --down          # on Ctrl-C, also `docker compose down`
```

---

## 3. Use the dashboard 🎨

Open **<http://127.0.0.1:8080>** in your browser.

**Log in:** put any non-empty value in the **API key** field — e.g. `demo` — and
submit. (Dev mode accepts any key.) Four tabs:

- **Posts** — the ingested posts
- **Analysis Jobs** — submit / track analysis runs
- **Reports** — generate & read reports (headline metrics, topic clusters, **and
  embedding clusters with one LLM summary per cluster**)
- **Search** — semantic + keyword search over the analyzed posts
- **Pipeline** — **real-time data flow**: each stage (Ingestion → Stage-1 → Router
  → Stage-2 → Assembler) shows live backlog (waiting) · in-flight (processing) ·
  dead-lettered (failed), streamed over SSE; plus the payload Inspector

**Header chips (click to open settings, switch at runtime):**

- **LLM: …** — the LLM backend for **both** Stage 1 and Stage 2
  (`Local (Ollama)` ⇄ `Groq Cloud`); the switch applies to both stages at runtime.
- **NLP: …** — the Stage-1 **sentiment model**, used only when Stage 1 runs the
  small-model suite (`STAGE1_LLM=false`). `Auto-route` picks by detected language
  (Bangla→BanglaBERT, Banglish→BanglishBERT, else→XLM-R). By default `STAGE1_LLM=true`,
  so Stage-1 NLP runs on the **gemma3:4b** LLM and this selector doesn't apply.

---

## 4. (Optional) The agent layer

The dashboard doesn't need this — it powers the `/v1/agents/query` API
(natural-language Q&A over the corpus). Start it together with everything else:

```bash
uv run run_all.py --with-agents
```

Then ask an agent a question:

```bash
curl -s -X POST http://127.0.0.1:8001/v1/agents/query \
  -H "X-API-Key: demo" -H "Content-Type: application/json" \
  -d '{"agent_type":"analyst","query":"What are people saying about Bangladesh politics?"}' \
  | python -m json.tool
```

`agent_type` is one of `analyst`, `coverage`, `alerting`.

---

## 5. (Optional) Runtime knobs & advanced extras

The defaults work out of the box; these only matter if you want to tune or go
beyond stub mode. Set env vars before `uv run run_all.py` (or in the manual §B
block). Full reference in [run.md](run.md).

- **Stage-1 LLM** (on by default): `STAGE1_LLM=true` makes the Fast-NLP workers
  compute their NLP on the `stage1` model (`STAGE1_LOCAL_MODEL`, default
  `gemma3:4b`); Stage 2 uses `STAGE2_LOCAL_MODEL` (default `qwen2.5:7b`). Set
  `STAGE1_LLM=false` to fall back to the small-model suite (or its stub). Cap the
  premium per-comment LLM pass with `STAGE1_LLM_COMMENT_MAX` (default 60).
- **Rate limiting** (on by default, 120 req/min per API key on analysis/report/agent
  calls): `RATE_LIMIT_ENABLED=false` to turn off in dev, or `RATE_LIMIT_PER_MIN=…`.
- **Near-duplicate reuse** (on by default): a post within cosine `NEAR_DUP_THRESHOLD`
  (0.97) of an already-analyzed one reuses that result and skips Stage-1/2.
  `NEAR_DUP_DEDUP=false` to disable. (In stub mode only *identical* captions match.)
- **Dead-letter queues**: failed messages retry then land on `<stream>:dlq`
  (`STAGE1_MAX_RETRIES`, `ASSEMBLER_MAX_RETRIES`).
- **Tracing (OpenTelemetry → Jaeger/Loki)**: `uv sync --extra obs`, then
  `OTEL_ENABLED=true` and `( cd deploy && docker compose up -d jaeger loki promtail )`.
  Jaeger UI on **<http://127.0.0.1:16686>**. Off by default (no-op without the extra).
- **Kafka bus (prod)**: `uv sync --extra kafka` + `BUS_BACKEND=kafka`. The MVP
  default is Redis Streams; the local quickstart always uses Redis.

---

## 6. If something's off

- **A service didn't come up** → read its log: `/tmp/<name>.log`
  (`api`, `stage1`, `router`, `stage2`, `assembler`, `ingestion`, `dashboard`,
  and `analytics_mcp` / `retrieval_mcp` / `ingest_mcp` / `agents`).
- **"Port already in use"** → something from a previous run is still up. Stop
  leftovers, then re-run:

  ```bash
  pkill -f "[p]ython -m services" ; pkill -f "[p]ython __main__.py"
  pkill -f "[u]vicorn" ; pkill -f "http.server 8080"
  ```

- **Dashboard says "Cannot reach API" / browser shows CORS errors for `localhost:8000`** →
  your browser cached an old `app.js`. **Hard-refresh** (Ctrl-Shift-R). The dashboard
  targets `http://127.0.0.1:8001` by default; confirm `curl http://127.0.0.1:8001/v1/health` works.
- **Want to start clean** → `uv run run_all.py --reset` (wipes Postgres + Redis, reloads posts).
- **Ollama missing/empty** → `ollama pull gemma3:4b qwen2.5:7b qwen3-vl:4b`;
  Stage-1 NLP (gemma3:4b) and Stage-2 (qwen2.5:7b) both need it.

Full troubleshooting + every knob is in **[run.md](run.md)**.

---

## Appendix — run it by hand (no script)

The script just automates these steps; run them yourself for full control or
debugging. **Do §A–§D in one terminal** so the env and venv carry across.

**A) Infra + deps + Ollama**

```bash
cd /home/bk/code/defense
( cd deploy && docker compose up -d postgres redis clickhouse minio )
uv sync && source .venv/bin/activate
ollama serve >/tmp/ollama.log 2>&1 &     # skip if already running

# wait for the DBs, then create the ClickHouse tables
for i in $(seq 1 30); do
  pg=$(docker inspect -f '{{.State.Health.Status}}' deploy-postgres-1 2>/dev/null)
  ch=$(docker inspect -f '{{.State.Health.Status}}' deploy-clickhouse-1 2>/dev/null)
  echo "pg=$pg ch=$ch"; [ "$pg" = healthy ] && [ "$ch" = healthy ] && break; sleep 3
done
docker exec -i deploy-clickhouse-1 clickhouse-client \
  --user defense --password defense --database defense --multiquery \
  < /home/bk/code/defense/services/workers/assembler/clickhouse_init.sql
```

**B) Environment** (resolves container IPs — keep this shell)

```bash
export REPO=/home/bk/code/defense
ipof() { docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "deploy-$1-1"; }

export DATABASE_URL="postgresql+asyncpg://defense:defense@$(ipof postgres):5432/defense"
export REDIS_URL="redis://$(ipof redis):6379/0"
export CLICKHOUSE_URL="clickhouse://defense:defense@$(ipof clickhouse):9000/defense"
export MINIO_ENDPOINT="http://$(ipof minio):9000"
export MINIO_ACCESS_KEY=minioadmin MINIO_SECRET_KEY=minioadmin MINIO_BUCKET=defense
export LLM_BACKEND=local LOCAL_LLM_BASE_URL=http://localhost:11434/v1 LOCAL_LLM_API_KEY=ollama
# Stage 1 and Stage 2 run on different models; STAGE1_LLM=true puts Stage-1 NLP on the LLM.
export STAGE1_LLM=true STAGE1_LOCAL_MODEL=gemma3:4b STAGE2_LOCAL_MODEL=qwen2.5:7b
export LLM_A_LOCAL_MODEL=qwen2.5:7b VLM_LOCAL_MODEL=qwen3-vl:4b
export MODEL_STUB_MODE=true JWT_SECRET=demo PYTHONPATH=$REPO
```

**C) Workers + API**

```bash
cd $REPO
python -m services.workers.stage1_nlp        > /tmp/stage1.log    2>&1 &
python -m services.workers.router            > /tmp/router.log    2>&1 &
python -m services.workers.stage2_llm        > /tmp/stage2.log    2>&1 &
( cd $REPO/services/workers/assembler && python __main__.py ) > /tmp/assembler.log 2>&1 &
python -m services.ingestion                 > /tmp/ingestion.log 2>&1 &
( cd $REPO/services/api && uvicorn main:app --host 127.0.0.1 --port 8001 --log-level warning ) > /tmp/api.log 2>&1 &
sleep 2 && curl -s http://127.0.0.1:8001/v1/health
```

**D) Push the 50 posts**

```bash
cd $REPO
python - <<'PY'
import json, httpx
posts = json.load(open("posts_with_details.json"))
r = httpx.post("http://127.0.0.1:8001/v1/posts/upload",
               headers={"X-API-Key": "demo"}, json={"posts": posts}, timeout=120)
print("upload:", r.status_code, r.json().get("job_id"))
PY
for i in $(seq 1 40); do
  n=$(docker exec deploy-postgres-1 psql -U defense -d defense -tAc "select count(*) from analysis_results")
  echo "analysis_results=$n"; [ "$n" = "50" ] && break; sleep 3
done
```

**E) Dashboard**

```bash
cd $REPO/dashboard
python -m http.server 8080      # open http://127.0.0.1:8080
```

The dashboard targets the dev API on `http://127.0.0.1:8001` by default
(`dashboard/app.js`). To point it elsewhere, set `window.API_BASE` in `index.html`
before the `app.js` `<script>` tag (there's a commented example near the top).

**Stop (manual):**

```bash
pkill -f "[p]ython -m services" ; pkill -f "[p]ython __main__.py"
pkill -f "[u]vicorn main:app"   ; pkill -f "http.server 8080"
( cd /home/bk/code/defense/deploy && docker compose down )   # add -v to wipe data
```

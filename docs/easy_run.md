# easy_run.md — one-command quickstart (with the dashboard UI)

Run the whole pipeline, load the 50 sample posts, and open the **dashboard in
your browser** — with a single command:

```bash
uv run run_all.py --with-agents --reset
```

Local dev, no GPU: Stage-1 NLP runs on the **gemma3:4b** LLM and Stage-2 on
**qwen2.5:7b** (both via Ollama); embeddings and vision stay stubbed. One-time
prerequisites are in [§1](#1-prerequisites-one-time); the command itself is
[§2](#2-run-it--one-command). For the deep reference (every env var, scaling,
full troubleshooting) see [run.md](run.md).

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
| Agents + 3 MCP servers                       | host, :8010 / :8110, :8101–8102   | ⛔ optional — included by `--with-agents` ([§4](#4-optional-the-agent-layer)) |

---

## 1. Prerequisites (one-time)

- **Docker** (with `compose`)
- **[uv](https://docs.astral.sh/uv/)** — `curl -LsSf https://astral.sh/uv/install.sh | sh`
- **[Ollama](https://ollama.com/)** with four models pulled — the pipeline stages
  and the agents run on **different** models:

  ```bash
  # ~12 GB total — Stage-1 NLP, Stage-2 text, the VLM, and the agents
  ollama pull gemma3:4b && ollama pull qwen2.5:7b && ollama pull qwen3-vl:4b \
    && ollama pull llama3.1:8b

  # Then derive the 16k-context tag the agents actually ask for. There is no
  # preflight for it, so without this every agent call comes back as Ollama's
  # `model "llama3.1:8b-16k" not found`.
  ollama create llama3.1:8b-16k -f config/Modelfile.llama31-16k
  ```

  | Model | Used for |
  | ----- | -------- |
  | `gemma3:4b` | Stage-1 Fast NLP — sentiment / emotion / topics + every comment |
  | `qwen2.5:7b` | Stage-2 — summary, insight, comment stance; also the Chat tab |
  | `qwen3-vl:4b` | VLM — image-grounded summaries (unexercised: no image bytes are reachable) |
  | `llama3.1:8b-16k` | All nine MCP agents (`AGENT_LOCAL_MODEL`) |

  **Why the derived tag.** `ollama serve` runs with no `OLLAMA_CONTEXT_LENGTH`, so
  it defaults to a **4,096-token** window and *silently discards* a longer prompt,
  oldest message first — which is the system prompt and then your question. One
  `semantic_search` result is bigger than that, so plain `llama3.1:8b` answers
  agent questions from the tail of a JSON payload and reports success.
  [`config/Modelfile.llama31-16k`](../config/Modelfile.llama31-16k) sets
  `num_ctx 16384`. Setting `OLLAMA_CONTEXT_LENGTH=16384` on the ollama service
  instead is fine — it covers the pipeline models too — but then set
  `AGENT_LOCAL_MODEL=llama3.1:8b` so it stops looking for the derived tag.

- **(Optional) the seven small sentiment heads** — the cheap half of the Stage-2
  comment ensemble, ~3 GB of HF checkpoints on CPU. Everything runs without them
  (the ensemble degrades to the LLM alone and says so), so this is a second-run
  step, not a prerequisite:

  ```bash
  uv run python deploy/prefetch_classifiers.py     # downloads, then proves each head votes
  ```

  Until you do, the Stage-2 log says `stage2_cheap_voters voted=0 declared=7` and
  the per-comment table in the dashboard shows `—` in those seven columns. That is
  the honest rendering of a head that never ran — not a bug.

Repo assumed at `/home/bk/code/defense`.

---

## 2. Run it — one command 🚀

### The whole thing, from a clean slate

```bash
cd /home/bk/code/defense && uv run run_all.py --with-agents --reset
```

That one line is the **full run**, in this order: install deps → start the four
datastore containers and wait for health → create the ClickHouse tables → **wipe
any previous data** → start the five workers and the API → start the agent layer
and its three MCP servers → serve the dashboard → upload and analyse the 50 sample
posts. Then it prints every URL and stays in the foreground.

| Up after that command | Where |
| --------------------- | ----- |
| **Dashboard** (this is the UI) | **<http://127.0.0.1:8080>** |
| API | <http://127.0.0.1:8001> · docs at `/docs` |
| Agents · analytics-mcp · retrieval-mcp · ingest-mcp | :8010 · :8110 · :8101 · :8102 |
| Postgres · Redis · ClickHouse · MinIO | Docker (MinIO console :9001) |
| Ollama | :11434 — on your host, **not** in Docker (§1) |

**Leave it running.** Press **Ctrl-C** when you're done and it stops everything it
started. Drop `--reset` to keep data from previous runs; drop `--with-agents` if
you don't need the Agents tab.

> Nothing is containerised except the four datastores — the workers, API,
> dashboard, agents and Ollama all run on your host. See
> [deployment.md](deployment.md) §2b if you want the everything-in-Docker mode
> instead (it exists, but nothing here is verified against it).

### Just the core (no agent layer)

```bash
cd /home/bk/code/defense
uv run run_all.py
```

Same thing minus the agents + MCP servers, and without wiping prior data.

> First run is slower than you might expect: Stage-1 NLP (`gemma3:4b`) and Stage-2
> (`qwen2.5:7b`) make real Ollama calls per post **on CPU**, so analysing the 50
> posts can take several minutes (the model weights also load on first call). The
> dashboard is usable as soon as the first results land. To go faster, run with
> `--groq` (or point the LLM backend at Groq from the dashboard), or set
> `STAGE1_LLM=false` for stub NLP.

<details>
<summary>What it does, in order</summary>

1. `uv sync` — installs deps into `.venv`
2. `docker compose up` the datastores → waits until healthy → creates the ClickHouse tables
3. with `--reset`: wipes Redis + Postgres + ClickHouse **before anything starts**, so no
   worker ever sees the old data
4. checks **Ollama** (starts it if installed but not running)
5. launches the **5 workers + API** (on :8001), waits for `/v1/health`
6. with `--with-agents`: starts the 3 MCP servers (:8110, :8101, :8102) and the agents service (:8010)
7. **serves the dashboard on :8080** (it already targets the dev API on :8001) — **the UI is up now**
8. uploads the **50 sample posts** and waits for all 50 to be analysed — the dashboard is
   already open, so you watch them populate live (skip this with `--manual-load` and push them yourself)

</details>

**Flags:**

```bash
uv run run_all.py --with-agents   # also start the agents + MCP layer (§4)
uv run run_all.py --groq          # run LLM work on Groq Cloud instead of Ollama — much faster (needs GROQ_API_KEY)
uv run run_all.py --ollama        # force local Ollama (the default) & clear any leftover Groq override
uv run run_all.py --reset         # wipe prior data, then reload the 50 posts
uv run run_all.py --no-load       # skip pushing the sample posts
uv run run_all.py --manual-load   # UI up first, no auto-load — push the posts yourself via the API
uv run run_all.py --no-dashboard  # don't serve the UI
uv run run_all.py --down          # on Ctrl-C, also `docker compose down`
```

`--groq`/`--ollama` pick the LLM backend for Stage-2 + agents + the Chat tab; omit
both to use the default (local Ollama). `--fast` is an accepted synonym for `--groq`.
You can also switch at runtime from the dashboard's **LLM** chip. Groq needs
`GROQ_API_KEY` in `.env` or the environment.

The dashboard is now served **before** the sample posts load, so the UI is up
right away regardless. With **`--manual-load`** nothing is auto-loaded and any
**leftover queued work from a previous run is quieted** — each stage's consumer
group is reset to **0 waiting / 0 in-flight** (the stream entries stay in Redis,
the workers just start past them), so the Pipeline tab sits idle and nothing runs
in the background. You push data yourself whenever you want, either from the
**Posts** tab (drop `posts_with_details.json` on the upload box) or via the API:

```bash
# body must be {"posts": [...]}; the file is a bare array, so wrap it:
curl -s -X POST http://127.0.0.1:8001/v1/posts/upload \
  -H "X-API-Key: demo" -H "Content-Type: application/json" \
  -d "$(python -c "import json;print(json.dumps({'posts':json.load(open('posts_with_details.json'))}))")"
```

> **Note:** `--manual-load` deletes nothing — it just parks the leftover queue
> backlog (skipped, still in Redis) and keeps results already stored in
> Postgres/ClickHouse, so the dashboard still shows posts analysed on earlier runs.
> For a truly blank slate, add **`--reset`** (`uv run run_all.py --manual-load
> --reset`) to wipe the datastores and queues.

(`--no-load` behaves the same but is the quiet "don't load anything" variant.)

---

## 3. Use the dashboard 🎨

Open **<http://127.0.0.1:8080>** in your browser.

**Log in:** put any non-empty value in the **API key** field — e.g. `demo` — and
submit. (Dev mode accepts any key.) The tabs:

- **Overview** — usage/cost counters + corpus charts
- **Posts** — the ingested posts, searchable by **post id / platform id / URL /
  campaign** or by caption, summary, topic and keyword. An id search checks the
  whole corpus, not just the rows on screen, so "no post has that id" and "not on
  this page" are different answers; the id cell has a copy button because it shows
  only the first 8 characters
- **Analysis Jobs** — submit / track analysis runs, and control them:
  **Stop** (no further post is analysed; the ones already inside a stage finish,
  so the counter may tick up once or twice more), **Resume** (re-queues *only*
  the posts an interrupted job never finished — this is what to press after a
  power cut, and it continues at 30/300 rather than re-paying for the first 30),
  **Re-run**, and **Delete** (removes the job record; the posts and their
  analysis results are kept). A job that has written no progress for five minutes
  is badged **stalled** rather than left reading "running" forever
- **Reports** — generate & read reports (headline metrics, topic clusters, **and
  embedding clusters with one LLM summary per cluster**)
- **Search** — semantic + keyword search over the analyzed posts
- **Agents** — natural-language Q&A over the corpus (needs `--with-agents`, §4)
- **Chat** — a **free-form chatbot** backed by the same LLM (`/v1/chat/stream`).
  Ask it anything: answers stream in live and render as Markdown (bold, lists,
  code blocks), each with a Copy button; an empty chat offers clickable prompt
  suggestions. In the header, a **backend** selector (Auto / Local / Groq —
  **Auto** follows the LLM toggle) and a **model** dropdown let you choose exactly
  which model answers (the dropdown lists the backend's available models, e.g.
  `gemma3:4b` / `qwen2.5:7b` locally; leave it on *Default* for the configured
  one). It's a general assistant with **no access to your posts** — use **Agents**
  for that.
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

The dashboard doesn't need this — it powers the `/v1/agents/query` API and the
**Agents** tab (natural-language Q&A over the corpus). **The full-run command in
[§2](#2-run-it--one-command) already includes it**; this section is what you
need if you started without it, or want to know what it adds.

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

`agent_type` is one of `analyst`, `coverage`, `alerting`, `stance`, `comparator`, `toxicity`, `narrative`, `quality`, `reporter` — `GET /v1/agents/types` returns the live list.

---

## 5. (Optional) Runtime knobs & advanced extras

The defaults work out of the box; these only matter if you want to tune or go
beyond stub mode. Set env vars before `uv run run_all.py` (or in the manual §B
block). Full reference in [run.md](run.md).

- **Stage-1 LLM** (on by default): `STAGE1_LLM=true` makes the Fast-NLP workers
  compute their NLP on the `stage1` model (`STAGE1_LOCAL_MODEL`, default
  `gemma3:4b`); Stage-2 classification uses `STAGE2_LOCAL_MODEL` (default
  `qwen2.5:7b`) and Stage-2 **summarization** uses `SUMMARY_LOCAL_MODEL` (also
  `qwen2.5:7b` until a bake-off picks a winner — `python -m eval.bakeoff_summary`).
  Set `STAGE1_LLM=false` to fall back to the small-model suite (or its stub).
- **Comment LLM coverage** — ⚠ **this is the big runtime knob.**
  `STAGE1_LLM_COMMENT_MAX` and `COMMENT_STANCE_MAX_PER_POST` both default to **0
  = every non-emoji comment gets an LLM label**. That is ~350 LLM calls for the
  43-post corpus and is the honest setting, but on local CPU Ollama it is slow.
  For a quick demo set both to something small (e.g. `60` / `40`, the old
  defaults) — and if you do, say so when quoting coverage, because it drops the
  LLM-labelled share from ~100% to ~29%. Tune throughput with
  `STAGE1_LLM_BATCH` (25 comments/call) and `STAGE1_LLM_CONCURRENCY` (3 batches
  in flight). Emoji-only comments (2.8%) are always skipped — no text to read.
- **Rate limiting** (on by default, 120 req/min per API key on analysis/report/agent
  calls): `RATE_LIMIT_ENABLED=false` to turn off in dev, or `RATE_LIMIT_PER_MIN=…`.
- **Target stance / watchlist** (off unless configured): edit
  `config/stance_targets.yml` to list entities and the pipeline reports stance
  *toward each of them* in a separate `target_stances` field. Costs **no extra LLM
  calls** — matched entities ride inside the Stage-2 stance call that already
  runs. Absent file = feature off. See [stance_targets.md](stance_targets.md).
- **Auth is permissive in dev, closed elsewhere.** With `APP_ENV=dev` (the
  default) any non-empty API key works and any login succeeds, exactly as before.
  Set `APP_ENV=production` and the API refuses unknown keys, unverified logins and
  a placeholder `JWT_SECRET` — so seed `api_keys`/`users` first (see
  [run.md](run.md) §"Auth & tenancy"). Streams should use
  `POST /v1/auth/sse-ticket` rather than putting a credential in the URL.
- **Semantic search is stub-backed in stub mode.** Every result carries
  `embedding_is_stub`; a stub vector is a hash, so kNN returns arbitrary
  neighbours. `EMBEDDING_ALLOW_STUB=false` refuses the write instead.
  The flag is reported by Stage 1 and carried to the column, so it stays correct
  when you flip `MODEL_STUB_MODE=false` to demo against a corpus analysed in stub
  mode. It was derived from the vector's *dimension* until §13.2 — and the stub
  is the same 768 dims as a real vector, so every row read as real.
- **Near-duplicate reuse** (**off by default** — `NEAR_DUP_DEDUP=true` to enable):
  a post within cosine `NEAR_DUP_THRESHOLD` (**0.95**) of an already-analyzed one
  reuses that result and skips Stage-1/2. (In stub mode only *identical* captions match —
  which, since identical text hashes to an identical vector, means cosine 1.0 and a
  guaranteed hit.) The result is **composed, not copied** (§13.3): identity,
  engagement and reactions come from the new post, only the post-level analysis is
  reused, the comment thread is reported unanalysed, and `processing.reused_from`
  records the source. It goes through the assembler, so all three stores are
  written. Keep it off (the default) for any run whose per-post *latency* numbers
  you intend to quote — a reused post does no stage work.
- **Dead-letter queues**: failed messages retry then land on `<stream>:dlq`
  (`STAGE1_MAX_RETRIES`, `ASSEMBLER_MAX_RETRIES`).
- **Tracing (OpenTelemetry → Jaeger/Loki)**: `uv sync --extra obs`, then
  `OTEL_ENABLED=true` and `( cd deploy && docker compose up -d jaeger loki promtail )`.
  Jaeger UI on **<http://127.0.0.1:16686>**. Off by default (no-op without the extra).
- **Kafka bus (prod)**: `uv sync --extra kafka` + `BUS_BACKEND=kafka`. The MVP
  default is Redis Streams; the local quickstart always uses Redis.

---

## 5b. Run the tests

```bash
uv run pytest -q                            # backend, ~50 s, needs nothing running
cd dashboard && npm test -- --run           # dashboard unit tests
cd dashboard && npm run test:e2e            # headless browser
```

Safe to run while the stack is up: a plain `pytest` cannot touch your datastores
or your log view — both are opt-in, and one of the opt-ins wipes Redis/Postgres/
ClickHouse. Details, targeted invocations and the gated groups:
**[testing.md](testing.md)**.

---

## 6. If something's off

- **A service didn't come up** → read its log: `/tmp/<name>.log`
  (`api`, `stage1`, `router`, `stage2`, `assembler`, `ingestion`, `dashboard`,
  and `analytics_mcp` / `retrieval_mcp` / `ingest_mcp` / `agents`).
- **"Port already in use"** → something from a previous run is still up. Stop
  leftovers, then re-run:

  ```bash
  pkill -f "[p]ython -m defense" ; pkill -f "[p]ython __main__.py"
  pkill -f "[u]vicorn" ; pkill -f "vite"
  ```

- **Dashboard says "Cannot reach API" / browser shows CORS errors for `localhost:8000`** →
  your browser cached an old `app.js`. **Hard-refresh** (Ctrl-Shift-R). The dashboard
  targets `http://127.0.0.1:8001` by default; confirm `curl http://127.0.0.1:8001/v1/health` works.
- **Want to start clean** → `uv run run_all.py --reset` (wipes Postgres + Redis, reloads posts).
- **Ollama missing/empty** → `ollama pull gemma3:4b qwen2.5:7b qwen3-vl:4b llama3.1:8b`;
  Stage-1 NLP (gemma3:4b) and Stage-2 (qwen2.5:7b) both need it.
- **`model "llama3.1:8b-16k" not found`** → the derived tag was never created:
  `ollama create llama3.1:8b-16k -f config/Modelfile.llama31-16k`.
- **An agent answers something you did not ask** → the context window truncated
  your question. Check you are on `llama3.1:8b-16k`, not plain `llama3.1:8b`.

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
  < /home/bk/code/defense/src/defense/services/workers/assembler/clickhouse_init.sql
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
export MODEL_STUB_MODE=true JWT_SECRET=demo APP_ENV=dev PYTHONPATH=$REPO
```

**C) Workers + API**

```bash
cd $REPO
python -m defense.services.workers.stage1_nlp        > /tmp/stage1.log    2>&1 &
python -m defense.services.workers.router            > /tmp/router.log    2>&1 &
python -m defense.services.workers.stage2_llm        > /tmp/stage2.log    2>&1 &
( cd $REPO/src/defense/services/workers/assembler && python __main__.py ) > /tmp/assembler.log 2>&1 &
python -m defense.services.ingestion                 > /tmp/ingestion.log 2>&1 &
python -m uvicorn defense.services.api.main:app --host 127.0.0.1 --port 8001 --log-level warning > /tmp/api.log 2>&1 &
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
npm install
npm run dev -- --port 8080      # open http://127.0.0.1:8080
```

The dashboard targets the dev API on `http://127.0.0.1:8001` by default
(`API_BASE` in [`dashboard/src/utils/api.js`](../dashboard/src/utils/api.js)).

**Stop (manual):**

```bash
pkill -f "[p]ython -m defense" ; pkill -f "[p]ython __main__.py"
pkill -f "[u]vicorn"           ; pkill -f "vite"
( cd /home/bk/code/defense/deploy && docker compose down )   # add -v to wipe data
```

---

## Next, by subject

Once it is running, the per-component documents explain what you are watching —
each ends in a table saying which of its claims are measured:

[PIPELINE.md](PIPELINE.md) · [ROUTER.md](ROUTER.md) (the routing rate you will see
in the logs) · [STAGE2_LLM.md](STAGE2_LLM.md) (why a run is slow, and which knob
bounds it) · [JOBS.md](JOBS.md) (stop and resume) ·
[LLM_BACKENDS.md](LLM_BACKENDS.md) (the local ⇄ Groq toggle) ·
[DASHBOARD_UI.md](DASHBOARD_UI.md) · [SYSTEM_MONITOR.md](SYSTEM_MONITOR.md) ·
and [run.md](run.md) for the full reference.

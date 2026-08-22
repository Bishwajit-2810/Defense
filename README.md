# Social Media "Smart Layer" — Design Documentation

**Run the whole thing with one command:**

```bash
uv run run_all.py --with-agents --reset
```

Datastores in Docker, everything else on your host; dashboard on
**<http://127.0.0.1:8080>**, API on :8001. Needs Docker, [uv](https://docs.astral.sh/uv/)
and Ollama with three models pulled — see [easy_run.md](easy_run.md) §1.

---

A production-grade, multilingual (Bangla + English + **Banglish**) AI
**microservice** that sits on top of an **existing social-media monitoring
platform**: it pulls a **post-with-details** payload (post **with its comments
embedded**, plus `engagement`, `reactionBreakdown`, and `sampleShares`) from that
platform's REST API, analyzes it in its **own separate database**, and returns
structured JSON — a post summary in the original language, **post sentiment**,
**per-comment sentiment** over the thread, topics, intents, entities, brand
mentions — for downstream projects to consume. It scales from 1k → 10k → 100k
threads per batch.

> **Modality scope (current).** Post sentiment is a **text** measurement today.
> The image path (SigLIP zero-shot + OCR) is implemented but unexercised: the
> dataset's 69 `photoUrls` are relative object-storage keys and the objects are
> not in MinIO, so no image bytes are reachable in any runnable configuration.
> Fusion weights renormalise over the terms that actually carry a model verdict,
> so the absent image term no longer silently shrinks the text signal, and a
> failed image fetch now reports as a failure instead of as a neutral verdict.
> The working corpus is [posts_with_details.json](posts_with_details.json) —
> all 50 posts, 10,272 comments, which is exactly what the ingestion path
> uploads. The 7 `null`-caption `PHOTO` posts have no *post* text to analyse,
> but they carry 1,307 perfectly analysable **comments**, so they are no longer
> excluded corpus-wide: the caption filter now lives in the one script that
> summarises captions. `python -m eval.make_text_corpus` still writes the
> 43-post subset for reproducing pre-existing numbers.
> See [data_contract.md](data_contract.md) §4 and
> [PROJECT_ASSESSMENT.md](PROJECT_ASSESSMENT.md) §5.2.

> **Input contract:** the real upstream **post-with-details** schema (embedded
> comments + engagement + reactions + shares), the integration model (pull + our
> own DB, no write-back), platform detection, and field mapping are documented in
> [data_contract.md](data_contract.md) — the source of truth, with a real sample
> in [posts_with_details.json](posts_with_details.json).

Platform is derived from each post's URL host (Facebook in the current sample;
others supported). The upstream's coarse **post** `sentiment`/`viralPotential` are
kept as a **baseline** while the smart layer **recomputes** richer sentiment;
**comment sentiment is empty upstream and OCR is no longer shipped — both are our
job** (see [data_contract.md](data_contract.md) §4).

**First target (in order):** post **text sentiment** → _(image sentiment — a
visual model on the photo, plus OCR — implemented but unexercised, see above)_ →
fuse (cross-check the crowd `reactionBreakdown`) → a **post summary grounded on
caption** → **per-comment sentiment** over the **embedded** comment thread
(reported with coverage, since only a stored sample of comments ships). See
[data_contract.md](data_contract.md) §4.

It is built around a **smart routing layer** ("thinking layer") that decides how
much intelligence each unit of work needs. The claim is deliberately narrow and
measured: **cheap NLP filters which _comments_ and which _posts_ deserve an
LLM.** Both halves matter, and the second is no longer the bigger one —

Measured on the 43-post working corpus, cold cache, 25 comments per batch:

| | Keyword stub | Stage-1 LLM (shipped) |
| --- | --- | --- |
| Posts routed to Stage 2 | 32 / 43 (**74%**) | 7 / 43 (**16%**) |
| Post-level LLM calls | 124 (15%) | 22 (4%) |
| Comment-level LLM calls | 689 (85%) | 507 (96%) |
| Total calls per run | 813 | 529 |
| Share the router gate governs | 55% | **30%** |

Reproduce with `python -m eval.measure_routing_rate`, which prints the routing
rate, the comment volume and the post-vs-comment split. Emoji-only comments
(2.8%) never enter a batch — there is no text in them to read.

Two things follow, and both are worth stating plainly:

- **The routing rate measures Stage-1 quality, not cost efficiency.** A Stage 1
  that types a post confidently bypasses Stage 2, so the rate *falls as Stage 1
  improves* — same code, two engines, two rates.
- **The better Stage 1 gets, the less the gate governs.** Comment labelling runs
  for every post, routed or not, so improving Stage 1 shrinks the gate's share of
  spend (55% → 30%) without shrinking the bill. What sets the bill is how many
  comments exist.

See [PROJECT_ASSESSMENT.md](PROJECT_ASSESSMENT.md) §4.6 and §6.8.

That LLM runs behind a **pluggable, runtime-switchable backend — `local`
(self-hosted vLLM/Ollama) or `groq` (Groq Cloud API)** — so the operator can
choose no-egress/no-bill local serving or fastest/zero-GPU Groq, and switch
anytime. Summarization and classification run on **separate roles**, so the
fluent (expensive) model is spent only on the task that needs prose, once per
post. `GET /v1/usage` reports tokens and cost **per backend and model**, with
local priced at zero — its marginal token cost genuinely is zero, and a single
blended rate was wrong for both backends in opposite directions. The hard goals:
**fast, cost-effective, efficient, and accurate on Bangla/Banglish** (with
fine-tuning hooks the owner can drive).

The **backend is Python + FastAPI** throughout and the **dashboard is React 19 +
Vite + Tailwind** (`dashboard/`, charts via chart.js). The original vanilla
HTML/CSS/JS dashboard the design docs specify is kept at `dashboard_legacy/`;
where a doc still says "plain HTML/CSS/JS, no build step", that is the superseded
decision. Above the per-post pipeline sits a selective
**agentic insight layer** — **nine** AI agents on a dedicated `agent` LLM role that
reach data through **MCP servers** (`analytics` / `retrieval` / `ingest`) for
analyst Q&A, watchlist stance, toxicity and narrative deep-dives, quality audits
and grounded reports — **corpus-tier only, never per post**
(see [architecture.md](architecture.md) §11).

This folder answers the system-design request described by the owner in
[what.txt](what.txt) — an upstream platform scrapes 1,000+ real-time
Bangla/English/Banglish posts **with their comments**; this smart layer pulls the
**post-with-details** payload from that platform's REST API and returns structured
JSON out. For the input contract see [data_contract.md](data_contract.md); for
concrete input→output, see [examples.md](examples.md).

> **Single-file master plan:** [masterplan.md](masterplan.md) consolidates every
> document below into one self-contained read (overview, architecture,
> alternatives, models, infrastructure, cost, API, examples, deployment, roadmap),
> kept in sync with the per-topic docs. Use the focused docs for one subject;
> use the master plan for the whole picture.

## Document index

| Document                                             | What it covers                                                                                                                                                                    |
| ---------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| [FEATURES.md](FEATURES.md)                           | **Feature list** — every capability, what it does, and whether it is **measured**, works-but-unmeasured, **unexercised**, or planned. Start here for "what does this actually do?"     |
| [masterplan.md](masterplan.md)                       | **Single-file master plan** — every document below consolidated into one self-contained read                                                                                      |
| [data_contract.md](data_contract.md)                 | **Upstream input contract** — real post-with-details schema (embedded comments + engagement + reactions + shares), integration (pull + own DB), platform detection, field mapping |
| [architecture.md](architecture.md)                   | Recommended high- and low-level architecture, the hybrid NLP→LLM pipeline, data flow, service breakdown, security                                                                 |
| [possible_architecture.md](possible_architecture.md) | Alternatives considered and tradeoffs (queues, databases, deployment, service mesh)                                                                                               |
| [models.md](models.md)                               | AI/NLP model selection per task, Bangla/Banglish support, RAG evaluation, fine-tuning strategy                                                                                    |
| [infrastructure.md](infrastructure.md)               | GPU sizing, monitoring stack, caching, scaling                                                                                                                                    |
| [cost_estimation.md](cost_estimation.md)             | Monthly cost estimates for MVP / Production / Enterprise                                                                                                                          |
| [api_design.md](api_design.md)                       | REST API contracts: ingest the post-with-details payload, get structured JSON                                                                                                     |
| [endpoints.md](endpoints.md)                         | Hands-on: the output JSON (annotated example) + curl commands to test every endpoint and reshape the JSON                                                                         |
| [examples.md](examples.md)                           | Real Facebook posts from [posts_with_details.json](posts_with_details.json), with embedded comments analyzed → full output JSON                                                   |
| [deployment.md](deployment.md)                       | Docker Compose vs Kubernetes, full K8s deployment plan                                                                                                                            |
| [evaluation.md](evaluation.md)                       | **Evaluation plan** — how we score each task, the summary, the agents, and system properties; gold sets, gates, drift                                                             |
| [plan.md](plan.md)                                   | Phased implementation roadmap and milestones                                                                                                                                      |
| [HOWTO.md](HOWTO.md)                                 | **Build guide for coding agents** — golden rules, ordered tasks with definition-of-done, repo layout; how to actually implement this design                                       |
| [stance_targets.md](stance_targets.md)               | **Watchlist-driven target stance** — the project's novelty item: per-entity stance over a configurable, alias-aware watchlist for code-mixed Bangla/Banglish. **Built**; watchlist contents and validation outstanding.  |
| [PROJECT_ASSESSMENT.md](PROJECT_ASSESSMENT.md)       | **Capstone / paper readiness review** — six independent audit passes, every finding with its evidence class (measured / probed / read), what was fixed, and what is still open       |
| [run.md](run.md) · [easy_run.md](easy_run.md)        | Running it: the one-command quickstart, then the deep reference (every env var, scaling, troubleshooting)                                                                            |
| [testing.md](testing.md)                             | **Running the tests** — the three suites and their commands, what a plain `pytest` deliberately skips (including the one that would wipe your datastores), and what a green run does *not* prove |
| [env.example.md](env.example.md)                     | **Every environment variable**, with its default and what happens if you change it — including the dead keys kept only because older docs mention them                               |
| [AGENTIC_RAG_NOVELTY.md](AGENTIC_RAG_NOVELTY.md)     | The agentic-RAG layer as a research contribution: what is novel, what is assembly, and which claims are measured                                                                     |
| [RAG_STATE_AND_ROADMAP.md](RAG_STATE_AND_ROADMAP.md) | Current state of retrieval + the agents, per-agent behaviour, and what is still missing to call it RAG rather than SQL-with-an-LLM-on-top                                            |
| [DASHBOARD_UI.md](DASHBOARD_UI.md)                   | **Dashboard UI** — React 19 + Vite + Tailwind architecture, all 11 tabs in depth, component hierarchy, SSE real-time streams, authentication flow, build & development                |
| [SYSTEM_MONITOR.md](SYSTEM_MONITOR.md)               | **System Monitor & Observability** — hardware telemetry (CPU/GPU/RAM), pipeline monitoring, structured logging (Loki), metrics (Prometheus/Grafana), tracing (OpenTelemetry/Jaeger)     |
| [AGENTS.md](AGENTS.md)                               | **Agents & Runner** — all 9 agent profiles, multi-turn tool execution engine, prompt injection hardening, citation verification, budget enforcement, chat routing                       |
| [MCP_SERVERS.md](MCP_SERVERS.md)                     | **MCP Servers** — analytics-mcp (ClickHouse), retrieval-mcp (pgvector), ingest-mcp (Redis) — all 18 tools, schemas, stub modes, agent-to-tool mapping, deployment                      |
| [dashboard/README.md](dashboard/README.md)           | The React dashboard: dev server, build, tests                                                                                                                                       |

Background: the original system-design request has been reconciled with
[what.txt](what.txt), which is now the authoritative source and supersedes it.
(`social_media_llm_architecture_prompt.md` was removed in commit `e9fba98`; the
link is dropped rather than left dangling.)

**What it does, feature by feature:** [FEATURES.md](FEATURES.md) — with each
capability marked measured / unmeasured / unexercised / planned, so nothing on a
slide is stated more strongly than the evidence supports.

**Current implementation status** is in
[PROJECT_ASSESSMENT.md](PROJECT_ASSESSMENT.md) — read its status header first.
The design documents in the table above describe the *intended* system; where
the two disagree, the assessment is what actually runs. Claims that are
implemented-but-unexercised (the image modality) or measured-and-different-from-
target (the routing rate) are flagged inline in each document.

## TL;DR of the recommendation

- **Unit of analysis = post + its comment thread.** The upstream returns a post
  **with its comments embedded** (a stored sample of the total). The service
  analyzes the whole thread and emits
  one JSON object per thread (see [architecture.md](architecture.md) §6,
  [data_contract.md](data_contract.md)). Processing order: **post sentiment first,
  then comments**.
- **Hybrid analysis pipeline.** Cheap, fast NLP models (fastText, transformer
  classifiers, spaCy/GLiNER) run over the post and every comment. An LLM is invoked
  **selectively** — for the original-language summary, insight, and the
  low-confidence / unclassified / high-toxicity / long-code-mixed cases the router's
  six gates single out ([rules.py](src/defense/services/workers/router/rules.py)). This is the
  central cost-control idea, and the routing rate it produces is reported from the
  router's own counters rather than assumed.
- **The router makes two decisions, not one.** The six gates decide **post-level**
  work. Separately, the router picks **which comments** Stage 2 analyses — the
  by default **every comment with text** (`ROUTER_COMMENT_TOP_N=0`) — and every
  Stage-2 voter reads that one set, so the post-level breakdown and the
  per-comment table always describe the same comments. Setting a positive cap
  keeps only the top-N by reaction count and bounds per-post comment cost at
  `ceil(N/25)` LLM calls; the comments below the cut are still kept and persisted,
  and the result reports how many (`ensemble.not_analysed`) rather than implying
  full coverage.
- **Comment sentiment is an ensemble, not a model.** Each analysed comment
  collects up to **eight** verdicts — seven small sentiment heads batched on CPU
  (`STAGE2_CLASSIFIER_1..7`) and the context-aware LLM stance pass, the only
  labeller that sees the post. One combiner writes the final label, and the
  dashboard shows all eight side by side. **Only a model may label a comment:**
  Stage 1's emoji + keyword rule is not a voter, because it answers on every
  comment (mostly with the deterministic hash stub) and a voter that can never
  abstain makes the agreement numbers unfalsifiable. A head that fails to load
  **abstains**; it is never counted as a neutral, `label_voters` says how many
  actually spoke, and a comment no model read is reported as `uncertain` rather
  than given a label.
- **Two LLM roles, a pluggable backend (local ⇄ Groq), switchable at runtime.**
  The selective stage uses **LLM-A** (fast 7B/8B) for per-post refinement and
  **LLM-B** (larger 14B/32B) for cluster summarization, insight, and grounded
  reports. Each role is served by the chosen backend: **`local`** (self-hosted
  vLLM in production, Ollama for local dev — no per-token bill, no data egress;
  the default) or **`groq`** (Groq Cloud API — fastest inference, zero GPU ops,
  per-token cost). Both speak an OpenAI-compatible API, so switching is a
  config/flag change (`run_all.py --groq` / `--ollama`, the dashboard **LLM**
  chip, or a per-request `backend`), and you can run hybrid/failover.
  Privacy-sensitive tenants are pinned to `local` **on every path**: the API
  resolves each job's backend, applies the lock where the tenant is known, and
  stamps the decision into the job envelope, so a global toggle cannot route a
  locked tenant's content off-box (PROJECT_ASSESSMENT §13.5).
- **Queue-based, horizontally scalable.** API → ingestion → message bus →
  stateless GPU/CPU workers → result store. Workers scale independently per
  stage.
- **Queue: Kafka for Production/Enterprise, Redis Streams for the MVP.** Start
  simple, migrate when throughput and replay/retention demand it.
- **Jobs can be stopped and resumed, which the queue shape dictates.** A job is
  N messages across the stage streams, not a process, so **stop** is a flag every
  stage checks as it picks a message up — bounding the cost of a stop at one post
  per stage instead of the rest of the batch. **Resume** exists for the failure
  the pipeline cannot detect on its own: a host that loses power at post 30 of
  300 leaves a row still reading `running` and no counters, so the remainder is
  recomputed from Postgres and only those 270 are re-queued, with progress
  continuing at 30/300 (architecture.md §3, api_design.md §3a).
- **Storage split by access pattern:** PostgreSQL + pgvector (operational + jobs +
  vectors/semantic search/dedup via the `analysis_results.embedding` `vector(768)`
  column), ClickHouse (analytics/aggregations), Redis (cache + dedup + rate
  limits), object storage (raw payloads + reports).
- **Agentic insight layer (MCP + AI agents).** A selective, corpus-tier layer of
  **nine** agents (FastAPI orchestrator on the dedicated `agent` role) reaches data
  via **MCP servers** (`analytics`/`retrieval`/`ingest`) for analyst Q&A, grounded
  reports, watchlist stance, and coverage deep-dives — gated, cached,
  budget-capped, never per post. The runner is hardened against the ways a small
  local model fails on real payloads: tool results are capped so one cannot
  displace the system prompt, byte-identical repeat calls are refused, and a run
  only reports `completed` if the answer actually answers something.
- **Free-form chatbot (`POST /v1/chat`, `/v1/chat/stream`).** Ask the platform
  LLM anything from the API or the dashboard **Chat** tab; replies stream token
  by token (SSE) and use whichever backend the toggle points at (local ⇄ Groq),
  overridable per request and subject to the same tenant privacy policy.
- **Cost telemetry that covers every caller.** `GET /v1/usage` counts tokens per
  backend and model, and splits spend across five lanes (`post`, `comment`,
  `stage1`, `interactive`, `agent`). `pipeline_tokens` isolates the per-post
  figure from per-question chat and agent spend, so the cost claim cannot be
  inflated by however much anyone used the chatbot.
- **Backend FastAPI; dashboard React 19 + Vite + Tailwind** (`dashboard/`; the
  original vanilla build is preserved in `dashboard_legacy/`).
- **Deployment:** Docker Compose for MVP, Kubernetes (with KEDA autoscaling on
  queue depth) for Production and beyond.

Read [architecture.md](architecture.md) first.

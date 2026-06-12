# Social Media "Smart Layer" — Design Documentation

A production-grade, multilingual (Bangla + English + **Banglish**) AI
**microservice** that sits on top of an **existing social-media monitoring
platform**: it pulls a **post-with-details** payload (post **with its comments
embedded**, plus `engagement`, `reactionBreakdown`, and `sampleShares`) from that
platform's REST API, analyzes it in its **own separate database**, and returns
structured JSON — a post summary in the original language, **multimodal sentiment
(text + image)**, **per-comment sentiment** over the thread, topics, intents,
entities, brand mentions — for downstream projects to consume. It scales from
1k → 10k → 100k threads per batch.

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

**First target (multimodal, in order):** post **text sentiment** → **image
sentiment** (a visual model on the photo; we also OCR it) → fuse (cross-check the
crowd `reactionBreakdown`) → a **post summary grounded on caption + image/OCR** →
**per-comment sentiment** over the **embedded** comment thread (reported with
coverage, since only a stored sample of comments ships). Most posts carry an image
and some have no caption, so the image is not optional — see
[data_contract.md](data_contract.md) §4.

It is built around a **smart routing layer** ("thinking layer") that decides, per
thread, how much intelligence each one needs: cheap NLP models do ~90–95% of the
work, and a selective LLM handles only the summarization/insight work. That LLM
runs behind a **pluggable, runtime-switchable backend — `local` (self-hosted
vLLM) or `groq` (Groq Cloud API)** — so the operator can choose no-egress/no-bill
local serving or fastest/zero-GPU Groq, and switch anytime. The hard goals:
**fast, cost-effective, efficient, and accurate on Bangla/Banglish** (with
fine-tuning hooks the owner can drive).

The **backend is Python + FastAPI** throughout and the **dashboard is plain
HTML/CSS/JS** (no framework). Above the per-post pipeline sits a selective
**agentic insight layer** — AI agents (on the same LLM-B backend) that reach data
through **MCP servers** (`analytics` / `retrieval` / `ingest`) for analyst Q&A,
grounded reports, and targeted deep-dives — **corpus-tier only, never per post**
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

Background: [social_media_llm_architecture_prompt.md](social_media_llm_architecture_prompt.md)
is the original system-design request. It has been reconciled with
[what.txt](what.txt), which is now the authoritative source and supersedes it.

## TL;DR of the recommendation

- **Unit of analysis = post + its comment thread.** The upstream returns a post
  **with its comments embedded** (a stored sample of the total). The service
  analyzes the whole thread and emits
  one JSON object per thread (see [architecture.md](architecture.md) §6,
  [data_contract.md](data_contract.md)). Processing order: **post sentiment first,
  then comments**.
- **Hybrid analysis pipeline.** Cheap, fast NLP models (fastText, transformer
  classifiers, spaCy/GLiNER) run over the post and every comment and handle
  ~90–95% of the work. An LLM is invoked **selectively** only for the
  original-language summary, insight, and low-confidence/ambiguous cases. This is
  the central cost-control idea.
- **Two LLM roles, a pluggable backend (local ⇄ Groq), switchable at runtime.**
  The selective stage uses **LLM-A** (fast 7B/8B) for per-post refinement and
  **LLM-B** (larger 14B/32B) for cluster summarization, insight, and grounded
  reports. Each role is served by the chosen backend: **`local`** (self-hosted
  vLLM in production, Ollama for local dev — no per-token bill, no data egress;
  the default) or **`groq`** (Groq Cloud API — fastest inference, zero GPU ops,
  per-token cost). Both speak an OpenAI-compatible API, so switching is a
  config/flag change, and you can run hybrid/failover. Pin privacy-sensitive
  tenants to `local`.
- **Queue-based, horizontally scalable.** API → ingestion → message bus →
  stateless GPU/CPU workers → result store. Workers scale independently per
  stage.
- **Queue: Kafka for Production/Enterprise, Redis Streams for the MVP.** Start
  simple, migrate when throughput and replay/retention demand it.
- **Storage split by access pattern:** PostgreSQL + pgvector (operational + jobs +
  vectors/semantic search/dedup via the `analysis_results.embedding` `vector(768)`
  column), ClickHouse (analytics/aggregations), Redis (cache + dedup + rate
  limits), object storage (raw payloads + reports).
- **Agentic insight layer (MCP + AI agents).** A selective, corpus-tier agent
  layer (FastAPI orchestrator on LLM-B) reaches data via **MCP servers**
  (`analytics`/`retrieval`/`ingest`) for analyst Q&A, grounded reports, and
  coverage deep-dives — gated, cached, budget-capped, never per post.
- **Backend FastAPI; dashboard plain HTML/CSS/JS.**
- **Deployment:** Docker Compose for MVP, Kubernetes (with KEDA autoscaling on
  queue depth) for Production and beyond.

Read [architecture.md](architecture.md) first.

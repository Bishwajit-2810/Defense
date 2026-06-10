# Social Media "Smart Layer" — Design Documentation

A production-grade, multilingual (Bangla + English + **Banglish**) AI
**microservice** that sits on top of an **existing social-media monitoring
platform**: it pulls scraped **posts** (Facebook, Telegram, X, Instagram, …) and
their **comment threads** from that platform's REST APIs, analyzes them in its
**own separate database**, and returns structured JSON — a post summary in the
original language, **multimodal sentiment (text + image)**, topics, intents,
entities, brand mentions, and per-thread comment analysis — for downstream
projects to consume. It scales from 1k → 10k → 100k threads per batch.

> **Input contract:** the real upstream **Post API** / **Comment API** schemas,
> the integration model (pull + our own DB, no write-back), platform detection,
> and the field mapping are documented in
> [data_contract.md](data_contract.md) — the source of truth, with a real sample
> in [social_posts.json](social_posts.json).

The platform derives each post's source from its URL host (so Facebook, Telegram,
X, Instagram and others are all first-class), and the upstream's own coarse
`sentiment`/`viralPotential` are kept as a **baseline** while the smart layer
**recomputes** richer sentiment of its own (see [data_contract.md](data_contract.md) §4).

**First target (multimodal, in order):** post **text sentiment** → **image
sentiment** (a visual model on the photo, when present) → fuse → a **post summary
grounded on caption + image/OCR** → **comments**. ~80% of posts carry an image and
~half have no caption, so the image is not optional — see
[data_contract.md](data_contract.md) §4.

It is built around a **smart routing layer** ("thinking layer") that decides, per
thread, how much intelligence each one needs: cheap NLP models do ~90–95% of the
work, and a selective LLM handles only the summarization/insight work. That LLM
runs behind a **pluggable, runtime-switchable backend — `local` (self-hosted
vLLM) or `groq` (Groq Cloud API)** — so the operator can choose no-egress/no-bill
local serving or fastest/zero-GPU Groq, and switch anytime. The hard goals:
**fast, cost-effective, efficient, and accurate on Bangla/Banglish** (with
fine-tuning hooks the owner can drive).

This folder answers the system-design request described by the owner in
[what.txt](what.txt) — an upstream platform scrapes 1,000+ real-time
Bangla/English/Banglish posts (with comment threads); this smart layer pulls them
from that platform's Post/Comment APIs and returns structured JSON out. For the
input contract see [data_contract.md](data_contract.md); for concrete
input→output, see [examples.md](examples.md).

> **Single-file master plan:** [masterplan.md](masterplan.md) consolidates every
> document below into one self-contained read (overview, architecture,
> alternatives, models, infrastructure, cost, API, examples, deployment, roadmap),
> kept in sync with the per-topic docs. Use the focused docs for one subject;
> use the master plan for the whole picture.

## Document index

| Document                                             | What it covers                                                                                                    |
| ---------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------- |
| [masterplan.md](masterplan.md)                       | **Single-file master plan** — every document below consolidated into one self-contained read                      |
| [data_contract.md](data_contract.md)                 | **Upstream input contract** — real Post API / Comment API schemas, integration (pull + own DB), platform detection, field mapping |
| [architecture.md](architecture.md)                   | Recommended high- and low-level architecture, the hybrid NLP→LLM pipeline, data flow, service breakdown, security |
| [possible_architecture.md](possible_architecture.md) | Alternatives considered and tradeoffs (queues, databases, deployment, service mesh)                               |
| [models.md](models.md)                               | AI/NLP model selection per task, Bangla/Banglish support, RAG evaluation, fine-tuning strategy                    |
| [infrastructure.md](infrastructure.md)               | GPU sizing, monitoring stack, caching, scaling                                                                    |
| [cost_estimation.md](cost_estimation.md)             | Monthly cost estimates for MVP / Production / Enterprise                                                          |
| [api_design.md](api_design.md)                       | REST API contracts: ingest post+comment threads, get structured JSON                                              |
| [examples.md](examples.md)                           | The two real threads from [what.txt](what.txt) (Bangla complaint, English brand page) → full output JSON          |
| [deployment.md](deployment.md)                       | Docker Compose vs Kubernetes, full K8s deployment plan                                                            |
| [plan.md](plan.md)                                   | Phased implementation roadmap and milestones                                                                      |

Background: [social_media_llm_architecture_prompt.md](social_media_llm_architecture_prompt.md)
is the original system-design request. It has been reconciled with
[what.txt](what.txt), which is now the authoritative source and supersedes it.

## TL;DR of the recommendation

- **Unit of analysis = post + its comment thread.** A post (from the upstream
  Post API) plus its comments/replies (pulled separately from the Comment API and
  joined by the post's unique id). The service analyzes the whole thread and emits
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
  vLLM — no per-token bill, no data egress; the default) or **`groq`** (Groq Cloud
  API — fastest inference, zero GPU ops, per-token cost). Both speak an
  OpenAI-compatible API, so switching is a config/flag change, and you can run
  hybrid/failover. Pin privacy-sensitive tenants to `local`.
- **Queue-based, horizontally scalable.** API → ingestion → message bus →
  stateless GPU/CPU workers → result store. Workers scale independently per
  stage.
- **Queue: Kafka for Production/Enterprise, Redis Streams for the MVP.** Start
  simple, migrate when throughput and replay/retention demand it.
- **Storage split by access pattern:** PostgreSQL (operational + jobs),
  ClickHouse (analytics/aggregations), Qdrant (vectors/semantic search/dedup),
  Redis (cache + dedup + rate limits), object storage (raw payloads + reports).
- **Deployment:** Docker Compose for MVP, Kubernetes (with KEDA autoscaling on
  queue depth) for Production and beyond.

Read [architecture.md](architecture.md) first.

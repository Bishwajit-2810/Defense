# Social Media "Smart Layer" — Design Documentation

A production-grade, multilingual (Bangla + English + **Banglish**) AI
**microservice** that takes scraped Facebook/Instagram **posts with their comment
threads** and returns structured JSON — a post summary in the original language,
sentiment, topics, intents, entities, brand mentions, and per-thread comment
analysis — for downstream projects to consume. It scales from 1k → 10k → 100k
threads per batch.

It is built around a **smart routing layer** ("thinking layer") that decides, per
thread, how much intelligence each one needs: cheap NLP models do ~90–95% of the
work, and **two local open-source LLMs (no external/paid API)** handle only the
selective summarization/insight work. The hard goals: **fast, cost-effective,
efficient, and accurate on Bangla/Banglish** (with fine-tuning hooks the owner
can drive).

This folder answers the system-design request described by the owner in
[what.txt](what.txt) — a scraper feeds 1,000+ real-time Bangla/English/Banglish
posts (with comment threads) in, and this smart layer returns structured JSON
out. For concrete input→output, see [examples.md](examples.md).

## Document index

| Document                                             | What it covers                                                                                                    |
| ---------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------- |
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

- **Unit of analysis = post + its comment thread.** A scraped item is a parent
  post plus its nested comments/replies. The service analyzes the whole thread and
  emits one JSON object per thread (see [architecture.md](architecture.md) §6).
- **Hybrid analysis pipeline.** Cheap, fast NLP models (fastText, transformer
  classifiers, spaCy/GLiNER) run over the post and every comment and handle
  ~90–95% of the work. An LLM is invoked **selectively** only for the
  original-language summary, insight, and low-confidence/ambiguous cases. This is
  the central cost-control idea.
- **Two local LLMs, no external/paid API.** The selective stage runs entirely on
  self-hosted models: **LLM-A** (fast 7B/8B) for per-post refinement and
  **LLM-B** (larger 14B/32B) for cluster summarization, insight, and grounded
  reports. Everything stays on our own GPUs — no per-token bill, no data egress.
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

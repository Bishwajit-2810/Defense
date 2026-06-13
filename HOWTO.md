# HOWTO — Build Guide for Coding Agents

This is the **execution guide**: how to build the smart layer described in these
docs, in order, with acceptance checks an autonomous coding agent can verify.
The other docs say _what/why_; this says _what to do next_.

- **Design source of truth:** [architecture.md](architecture.md) (esp. §6 output
  schema, §3 data flow, §5 routing, §11 agents/MCP).
- **Input contract:** [data_contract.md](data_contract.md) + real sample
  [posts_with_details.json](posts_with_details.json).
- **Models:** [models.md](models.md). **Roadmap:** [plan.md](plan.md).
  **API:** [api_design.md](api_design.md). **Eval:** [evaluation.md](evaluation.md).
  **Deploy:** [deployment.md](deployment.md). **Consolidated:** [masterplan.md](masterplan.md).

> **How to use this file:** work top to bottom. Do **Phase 0 first** (it locks the
> two contracts everything else depends on). Each task has a **DoD** (definition of
> done) — do not mark a task complete until its DoD passes. Never violate the
> **Golden rules** below; if a task seems to require it, stop and re-read the
> referenced doc.

---

## 0. Golden rules (invariants — never break these)

1. **Don't send every post to an LLM.** Cheap NLP/vision handles ~all posts; the
   LLM/VLM runs **selectively** (target: single-digit % of posts). The Router gates
   it on confidence + task flags. ([architecture.md](architecture.md) §1, §5)
2. **Input is one `post-with-details` payload** — post **with `comments[]`
   embedded**, plus `engagement`, `reactionBreakdown`, `sampleShares`. There is **no
   separate Comment API**. ([data_contract.md](data_contract.md))
3. **We are a read-only consumer with our OWN database.** Pull from upstream; **never
   write back** to it. Key everything by the upstream CUID `id` (idempotent upsert).
4. **Recompute, keep baseline.** Recompute our own sentiment; keep upstream
   `sentiment`/`viralPotential` as `baseline_sentiment`/`baseline_viral_potential`.
   **Never overwrite** the baseline.
5. **Comment sentiment is OURS.** Upstream ships comment `sentiment: null` and
   `category: "NEUTRAL"` (placeholder) — compute per-comment sentiment ourselves.
6. **OCR is OURS.** The payload does **not** ship `photoOcrTexts`; run OCR on
   `photoUrls` (PaddleOCR/Tesseract or the VLM).
7. **Comments are a stored sample.** `engagement.storedCommentRows` is **usually far
   below** `commentCount` (occasionally `≥` it for low-comment posts —
   [data_contract.md](data_contract.md) §2). Analyze the stored sample
   (`analyzed == storedCommentRows`) and **report `coverage` (`analyzed/commentCount`,
   which may exceed 100% on those low-comment posts)** — never imply full-thread
   coverage.
8. **Multimodal sentiment.** Fuse `text_sentiment` (caption) + `image_sentiment`
   (visual model on the photo) → `overall_sentiment`/`sentiment_score`; cross-check
   against `reactionBreakdown`. `image_*` is `null` for text-only; `text_sentiment`
   is `null` for `null`-caption posts.
9. **`media_type` ≠ `post_type`.** `media_type` = upstream `postType`
   (TEXT/PHOTO/PHOTO_TEXT); `post_type` = our semantic class (complaint/news/…).
10. **Summary is grounded** on caption + OCR + image; written in the post's own
    language; record `post_summary_grounding`. `post_summary_source` = `"vlm"` for
    image posts, `"llm"` for text-only.
11. **Pluggable LLM backend.** All Stage-2/agent LLM calls go through one
    OpenAI-compatible client switchable by `LLM_BACKEND=local|groq` (+ per-request
    override, + per-tenant policy). **Privacy-locked tenants stay `local`.**
12. **Agents are corpus-tier ONLY — never per post.** The agentic layer (§11) is
    Phase 2; it does analyst Q&A / reports / deep-dives, gated + cached +
    budget-capped.
13. **Backend = FastAPI. Dashboard = plain HTML/CSS/JS** (vanilla, no framework).
14. **Validate every output against the JSON Schema** (Task 0.2) before persisting.
15. **Platform is derived from the `url` host** — never hardcode "facebook".

---

## 1. Stack to use (pin these)

| Concern      | Use                                                                                                                                                     |
| ------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Language/API | **Python 3.11+ / FastAPI** (async) for all services                                                                                                     |
| Workers      | Python consumers (Celery or Ray); micro-batch on GPU                                                                                                    |
| NLP          | fastText (lang) · XLM-R/mBERT + BanglaBERT (sentiment/emotion/topic/intent/tox) · GLiNER/spaCy (NER) · KeyBERT (keywords) · paraphrase-multilingual-mpnet-base-v2 / 768-dim (embeddings) |
| Vision       | **SigLIP/CLIP** zero-shot (image sentiment) · **PaddleOCR/Tesseract** (OCR, bn+en)                                                                      |
| LLM/VLM      | Stage-2 via **vLLM** (`local`: Qwen2.5-7B/32B-Instruct + Qwen2.5-VL-7B) ⇄ **Groq** (`groq`: Llama text + a vision model). One OpenAI-compatible client. |
| Serving      | Triton/ONNX/CTranslate2 for the NLP fleet                                                                                                               |
| Bus          | **Redis Streams** (MVP) → **Kafka** (Prod), partitioned by `hash(post_id)`                                                                              |
| Stores       | **PostgreSQL + pgvector** (ops/jobs/results + vectors) · **ClickHouse** (analytics) · **Redis** (cache/dedup) · **S3/MinIO** (raw payloads, reports)    |
| Agents/tools | Agent orchestrator (FastAPI) + **MCP servers** (FastAPI + MCP SDK) — Phase 2                                                                            |
| Frontend     | **Plain HTML + CSS + JavaScript** served static                                                                                                         |
| Deploy       | Docker Compose (MVP) → Kubernetes + KEDA (Prod) — see [deployment.md](deployment.md)                                                                    |
| Config       | `LLM_BACKEND`, `GROQ_API_KEY`, per-tenant policy, model IDs — all in config/secrets                                                                     |

Suggested monorepo layout:

```text
/services
  /api            FastAPI: gateway routes, ingestion, reporting, auth, /v1/agents
  /ingestion      upstream client (pull post-with-details) → normalize → enqueue
  /workers
    /stage1_nlp   text suite + vision (image sentiment) + OCR
    /router       confidence gate + task flags
    /stage2_llm   backend-agnostic LLM/VLM worker (summaries/insight)
    /assembler    merge → JSON-Schema validate → persist (PG+pgvector/CH/object)
  /agents         orchestrator + agent definitions (Phase 2)
  /mcp            analytics-mcp, retrieval-mcp, ingest-mcp (Phase 2)
/libs
  /schemas        input + output JSON Schemas (the contracts)
  /llm            OpenAI-compatible backend client (local⇄groq) + policy
  /common         normalization, platform-from-url, hashing, config
/dashboard        plain HTML/CSS/JS
/eval             gold sets + harness (see evaluation.md)
/deploy           docker-compose.yml, k8s manifests
```

---

## 2. Phase 0 — Foundations (do this first)

**Task 0.1 — Repo skeleton + local stack.** Create the layout above and a
`docker-compose.yml` (Postgres + pgvector, Redis, ClickHouse, MinIO, a stub FastAPI,
Prometheus/Grafana). The Postgres image is `pgvector/pgvector:pg16`; `deploy/init-db.sql`
runs `CREATE EXTENSION vector` to enable the extension. **DoD:** `docker compose up`
brings everything healthy; `GET /v1/health` returns 200.

**Task 0.2 — Lock the two contracts (`/libs/schemas`).**

- **Input schema** = the post-with-details object ([data_contract.md](data_contract.md)
  §1/§1.1/§1.2/§2). Validate against `posts_with_details.json` (all 50 must pass).
- **Output schema** = [architecture.md](architecture.md) §6 (post_id, campaign_id,
  platform, platform_post_id, media_type, language, post_type, post_summary,
  post_summary_lang, post_summary_grounding, overall_sentiment, sentiment_score,
  text_sentiment, image_sentiment, baseline_sentiment, baseline_viral_potential,
  emotion, intents, topics, entities, brand_mentions, keywords, toxicity_score,
  hate_speech_score, engagement{…,stored_comments}, reaction_breakdown, shares,
  image_analysis, comment_analysis{analyzed,coverage,sentiment_breakdown,themes,…},
  post_summary_source, confidence, processing{…}, created_at, scraped_at).
- **DoD:** both schemas exist; a validator lib rejects malformed objects; the 50
  sample posts validate as input.

**Task 0.3 — LLM backend client (`/libs/llm`).** One OpenAI-compatible client
selectable by `LLM_BACKEND=local|groq`, with role→model-ID mapping (LLM-A/LLM-B/VLM),
a per-request override, and **tenant policy enforcement** (a `local`-pinned tenant
can never be sent to `groq`). **DoD:** unit test flips backend via env + per-request
override; a policy-violating override is rejected.

**Task 0.4 — Common libs.** `platform_from_url(url)` (host→facebook/telegram/x/
instagram/other), Unicode-NFC normalization + Banglish script tagging, content-hash
over post+comments. **DoD:** unit tests incl. all sample hosts (sample is 100% FB —
do not hardcode it).

**Exit criterion:** one post flows `ingest → stub worker → Postgres →
GET /v1/analysis/{id}` emitting schema-valid JSON.

---

## 3. Phase 1 — MVP (the first target, in order)

Build the multimodal post-and-thread pipeline. **Order matters** (Golden rule 8;
[data_contract.md](data_contract.md) §4).

**Task 1.1 — Ingestion service.** Pull the post-with-details payload (by campaign /
time / id) **or** accept a pushed batch (`POST /v1/posts/upload`). Derive `platform`,
keep `baseline_*`, **run OCR on `photoUrls`**, normalize caption+OCR, take the
embedded comment thread, record coverage (`storedCommentRows`/`commentCount`),
content-hash dedup (Redis), upsert into our DB by CUID `id`, enqueue. **DoD:**
ingesting `posts_with_details.json` upserts 50 posts + their stored comments; re-run
upserts (no dupes); `platform` derived; `baseline_sentiment` populated; OCR text
present for image posts.

**Task 1.2 — Stage-1 NLP + vision worker.** Per post, **post first then comments**:

- _Text:_ lang/Banglish → `text_sentiment` (+emotion, topics, intent, toxicity/hate,
  NER, keywords, embedding) over caption + OCR text.
- _Vision (image posts):_ SigLIP/CLIP → `image_sentiment`; VLM/short description;
  OCR already done in 1.1.
- _Fuse:_ `text_sentiment` + `image_sentiment` → `overall_sentiment`/`sentiment_score`
  (text-weighted with caption; image+OCR-weighted for `null`-caption), **cross-check
  `reactionBreakdown`**.
- _Comments:_ run the **same text sentiment over each embedded comment** → thread
  `sentiment_breakdown` + themes (weight by `likes`); set `coverage`.
- Emit a **confidence** per field. **DoD:** on sample posts, image posts get
  populated `image_sentiment`; text-only get `null`; `text_sentiment` is `null` for
  `null`-caption posts; `comment_analysis.analyzed == storedCommentRows`;
  `reaction_breakdown` sums to `totalReactions`; baseline preserved.

**Task 1.3 — Router/Triage.** Confidence gates + task flags decide complete-vs-LLM.
**DoD:** measured LLM-routing rate is single-digit % on a sample batch; low-confidence
items route to Stage 2.

**Task 1.4 — Stage-2 LLM/VLM worker.** Backend-agnostic (Task 0.3). Produce
`post_summary` **grounded on caption+OCR+image** — **VLM** for image posts
(`post_summary_source:"vlm"`), text LLM otherwise (`"llm"`); in the post's own
language; set `post_summary_grounding`. Cache by
`(backend, model, task, content_hash)`. **DoD:** image-post summary references image
content; language matches; cache hit on repeat.

**Task 1.5 — Result assembler.** Merge Stage-1 + Stage-2 → canonical JSON →
**JSON-Schema validate (Task 0.2)** → fan out to **three backends**: Postgres
(result row + the `analysis_results.embedding` `vector(768)` column via pgvector),
ClickHouse (analytics row), MinIO (raw). The embedding upsert is idempotent, keyed by
`post_id`. **DoD:** every persisted object is schema-valid; analytics row queryable;
the `embedding` column is populated.

**Task 1.6 — Read APIs (FastAPI).** `POST /v1/ingest/sync`, `POST /v1/posts/upload`,
`POST /v1/analysis/run`, `GET /v1/analysis/{id}`, `GET /v1/reports` (basic),
`GET /v1/search`, auth (API key + JWT). Match [api_design.md](api_design.md).
**DoD:** contract tests pass; results match the §6 schema.

**Task 1.7 — Dashboard (plain HTML/CSS/JS).** Static page calling the read APIs: job
status, results table, per-post sentiment + comment breakdown + a `reaction_breakdown`
chart. **DoD:** loads from static host/CDN, no framework/build step, renders a real
result.

**Task 1.8 — Monitoring + eval harness.** Prometheus/Grafana/Loki; track LLM-routing
rate + cache hits. Stand up the eval harness + gold sets per
[evaluation.md](evaluation.md). **DoD:** per-task scorecard runs; ship-gate check
exists.

**Phase 1 exit:** 1,000-post batches reliably; LLM slice single-digit %; per-task
metrics baselined on the gold set ([evaluation.md](evaluation.md)); cost-per-1k
recorded.

---

## 4. Phase 2 — Production hardening + agentic insight layer

Scale + reliability ([plan.md](plan.md) Phase 2), then the agents.

- **Bus → Kafka** (partitioned, DLQ + replay); split services; **Kubernetes + KEDA**
  ([deployment.md](deployment.md)); add **LLM-B**; retries/circuit-breakers +
  **local↔groq failover**; cluster summarization; reporting on ClickHouse.
- **Agentic insight layer ([architecture.md](architecture.md) §11) — Phase 2, not MVP:**
  - **MCP servers** (FastAPI + MCP SDK, internal `ClusterIP`-only): `analytics-mcp`
    (ClickHouse/Postgres), `retrieval-mcp` (pgvector semantic search + fetch), `ingest-mcp` (trigger
    upstream pull / fetch more comments — writes only to OUR db).
  - **Agent orchestrator** (FastAPI, CPU-only) running on the LLM-B backend:
    **Insight/Analyst** (`POST /v1/agents/query` + report generation),
    **Coverage deep-dive** (low `coverage`/viral → `fetch_more_comments`),
    **Alerting** (scheduled, on reaction/sentiment spikes).
  - **DoD:** agents are corpus-tier only (never invoked per post), gated + cached +
    budget-capped; runs record backend/model/tools/tokens; reports are grounded +
    cited; tenant `local` policy honored.

**Phase 3 — Enterprise scale:** see [plan.md](plan.md) §Phase 3 (per-stage GPU
pools, data-layer scale-out, hybrid local+groq burst, continuous fine-tuning).

---

## 5. Definition of done (whole system)

- [ ] All 50 sample posts ingest, analyze, and emit **JSON-Schema-valid** output.
- [ ] Sentiment is **recomputed**; `baseline_*` preserved; **comment sentiment + OCR
      are ours**; `coverage` reported.
- [ ] Multimodal fields correct (image vs text-only null rules; fusion;
      `reaction_breakdown` cross-check; summary grounding + language).
- [ ] LLM-routing rate single-digit %; backend switch `local⇄groq` works; tenant
      policy enforced.
- [ ] Dashboard is plain HTML/CSS/JS; backend is FastAPI.
- [ ] Eval gates green on the per-language gold set ([evaluation.md](evaluation.md)).
- [ ] (Phase 2) Agents corpus-tier only; MCP servers internal + read-mostly;
      `ingest-mcp` never writes upstream.

When unsure about a field, a metric, or a tradeoff: **read the referenced doc
section — do not guess.**

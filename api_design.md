# API Design — REST Contracts

REST API for the smart-layer microservice: ingestion of **post + comment
threads**, batch processing, structured results, and reporting. JSON over HTTPS.
Auth via `Authorization: Bearer <JWT>` or `X-API-Key: <key>` (see
[architecture.md](architecture.md) §9). All endpoints are versioned under `/v1`.
For full real input→output examples see [examples.md](examples.md).

> **Two ingestion paths.** The **primary** path is a **pull** of the upstream
> platform's **post-with-details** payload (comments embedded) into our own database
> (§1a) — the real input contract is in [data_contract.md](data_contract.md). The **`/v1/posts/upload`**
> push path (§1b) remains for external/replay sources. These read/analysis/report
> endpoints below are what _our_ downstream consumers call.

> **This file is the contract as designed; [endpoints.md](endpoints.md) is the
> surface as built** — copy-pasteable curl for what the running app serves
> (**47 distinct `/v1` paths, 58 method+path pairs** as of 17 Aug 2026), including
> per-comment paging, chat history, the raw event stream and the report export
> paths. Where the two disagree, endpoints.md is the current one. In particular the
> `comment_analysis` block below predates the comment ensemble: the shipped shape
> adds `parallel_labels` per comment, plus `ensemble` and `stage2_selection` — see
> endpoints.md §1. The live list is always `GET /openapi.json` (or `/docs`).

Conventions:

- `202 Accepted` for async work (returns a job/analysis id to poll or subscribe).
- Idempotency via `Idempotency-Key` header on writes.
- Pagination via `?limit=&cursor=`. Errors use a consistent envelope.

---

## 1a. Ingestion (primary) — pull from upstream — `POST /v1/ingest/sync`

The primary input is a **pull** of the existing platform's **post-with-details**
payload (post **with its `comments[]` embedded**, plus `engagement`,
`reactionBreakdown`, `sampleShares`) into our own database. The real upstream
schema and integration model are in [data_contract.md](data_contract.md). This
endpoint **triggers** a pull for a selector (campaign / time window / id); the
service then fetches, copies, and analyzes — **post first, then its comments** —
with no write-back to upstream.

### Request

```http
POST /v1/ingest/sync
Content-Type: application/json
X-API-Key: sk_live_...
Idempotency-Key: 7d3c...-sync-001
```

```json
{
  "source": "upstream",
  "selector": {
    "campaign_id": "cmoldmxzr02d8fu22vhvrg23c",
    "posted_from": "2026-05-01T00:00:00",
    "posted_to": "2026-05-31T00:00:00"
  },
  "options": {
    "tasks": ["all"],
    "want_summary": true,
    "summary_lang": "auto",
    "llm_backend": "auto"
  }
}
```

The service reads records keyed by their CUID `id`, derives `platform` from each
`url` host, keeps upstream `sentiment`/`viralPotential` as `baseline_*`, **runs OCR
on `photoUrls`** (the payload no longer ships OCR text), and **recomputes** richer
sentiment for the post **and every embedded comment** (the upstream leaves comment
sentiment empty) — see [data_contract.md](data_contract.md) §4. Comments arrive
embedded as a stored sample (`engagement.storedCommentRows` of `commentCount`), so
analysis reports **coverage**. `summary_lang: "auto"` keeps the summary in the
post's detected language. Re-pulling the same `id` upserts (idempotent).

---

## 1b. Ingestion (push, optional) — `POST /v1/posts/upload`

For external/replay sources, callers may **push** post-with-details records
directly, instead of pulling from upstream. Records use the **same field names** as
the upstream payload ([data_contract.md](data_contract.md) §1) — including the
embedded `comments[]`, `engagement`, and `reactionBreakdown` — so a raw response can
be replayed verbatim.

### Request (inline batch)

```http
POST /v1/posts/upload
Content-Type: application/json
X-API-Key: sk_live_...
Idempotency-Key: 7d3c...-batch-001
```

```json
{
  "source": "inline",
  "posts": [
    {
      "id": "cmosjpp9305n0u9tskgmd1c4k",
      "campaignId": "cmoldmxzr02d8fu22vhvrg23c",
      "platformPostId": "4460219584209360",
      "url": "https://www.facebook.com/4460219584209360",
      "caption": "শাপলা চত্বরের সেই রাতের কথা ...",
      "photoUrls": [
        "posts/cmoldmxzr02d8fu22vhvrg23c/4460219584209360/18f4cbb26803.jpg"
      ],
      "postType": "PHOTO_TEXT",
      "postedAt": "2026-05-04T18:19:14",
      "scrapedAt": "2026-05-05T17:39:44.464",
      "sentiment": -0.85,
      "viralPotential": 0.78,
      "engagement": {
        "commentCount": 1562,
        "totalReactions": 84979,
        "shareCount": 3189,
        "storedCommentRows": 112,
        "storedReactionRows": 0,
        "reach": 0,
        "saves": 0,
        "impressions": 0
      },
      "reactionBreakdown": {
        "SAD": 65289,
        "LIKE": 18235,
        "LOVE": 682,
        "HAHA": 566,
        "CARE": 125,
        "WOW": 56,
        "ANGRY": 26
      },
      "sampleShares": [],
      "comments": [
        {
          "id": "cmosktkag038n8jv53z8hx4ea",
          "platformCommentId": "…",
          "parentId": null,
          "likes": 574,
          "replyCount": 14,
          "authorUsername": "Abdur Rahman Wisdom's",
          "sentiment": null,
          "category": "NEUTRAL",
          "text": "এই ছবিগুলো প্রমাণ করে যে পুলিশ আমাদের বন্ধু ছিল না কখনো।"
        }
      ]
    }
  ],
  "options": {
    "tasks": ["all"],
    "want_summary": true,
    "summary_lang": "auto",
    "llm_backend": "auto"
  }
}
```

`comments` carries the embedded thread (a stored sample of `engagement.commentCount`);
comment `sentiment` arrives `null` and is **computed by us**. Banglish comments
(romanized Bangla) are handled natively. `platform`, OCR, and `baseline_*` are
derived on ingest exactly as in the pull path.

`llm_backend` selects the LLM provider for this request: `"local"`
(self-hosted vLLM/Ollama), `"groq"` (Groq Cloud API), or `"auto"` (default — use
the server's configured backend and failover policy). A per-request override lets
callers pin sensitive data to `"local"` or send burst traffic to `"groq"` without
changing server config.

**How it is resolved and enforced.** The API resolves the backend at enqueue time
— `request option > runtime toggle (config:llm_backend) > LLM_BACKEND env` — and
applies the tenant policy to the **result**, then stamps that resolved value into
the job envelope. The pipeline workers have no database and no tenant, so this is
the only layer that can decide it. Two outcomes for a privacy-locked tenant, and
the asymmetry is deliberate:

* they **asked** for `groq` → `403 forbidden`; it is their own request, and it is
  refusable;
* the **toggle or the env default** says `groq` → silently pinned to `local`. An
  operator's global switch is not that tenant's choice, and the guarantee is
  "their content never leaves the local backend", not "they get an error".

Until PROJECT_ASSESSMENT §13.5 this option was policy-checked and then **read by
no worker** — the pipeline took its backend from the global Redis toggle — so the
403 was the override's only observable effect. See [models.md](models.md) §2.

### Request (large file)

```json
{
  "source": "object",
  "object_uri": "s3://uploads/tenant42/batch-2026-06-01.jsonl",
  "format": "jsonl",
  "options": { "tasks": ["all"], "want_summary": true }
}
```

### Response — `202 Accepted`

```json
{
  "job_id": "job_01HZX...",
  "accepted": 2,
  "duplicates_skipped": 0,
  "status": "queued",
  "status_url": "/v1/analysis/job_01HZX...",
  "created_at": "2026-06-01T12:00:00Z"
}
```

`options.tasks` selects analyses (e.g.
`["text_sentiment","image_sentiment","comment_sentiment","ner","toxicity"]` or
`["all"]`). `image_sentiment` runs the visual model on image posts (no-op for
text-only) — **currently a no-op everywhere**, because no image bytes are
reachable ([PROJECT_ASSESSMENT.md](PROJECT_ASSESSMENT.md) §5.2); the result
reports `image_analysis.vision_status` rather than a fabricated `neutral`.
`comment_sentiment` scores the embedded comment thread (the upstream ships none)
— since the caps were lifted this means **every non-emoji comment**, batched at
25 per LLM call with bounded concurrency. `want_summary`/`want_insight` opt into
the LLM tasks. Otherwise the router keeps work on the cheap path unless
confidence is low.

---

## 2. Batch processing — `POST /v1/analysis/run`

Trigger (or re-trigger) analysis for already-ingested posts, or run a saved
batch with specific options — useful for reprocessing after a model upgrade.

### Request

```json
{
  "selector": { "job_id": "job_01HZX..." },
  "options": {
    "tasks": [
      "text_sentiment",
      "image_sentiment",
      "comment_sentiment",
      "emotion",
      "topics",
      "ner",
      "toxicity"
    ],
    "want_summary": true,
    "want_cluster_summary": true,
    "model_profile": "default",
    "llm_backend": "auto"
  }
}
```

`selector` may instead be `{ "post_ids": [...] }` (upstream CUIDs),
`{ "campaign_id": "cmpe1djj…" }`, or
`{ "filter": { "platform": "telegram", "from": "...", "to": "..." } }`
(`platform` is the derived host value: `facebook` | `telegram` | `x` |
`instagram` | …).
`llm_backend` (`auto` | `local` | `groq`) overrides the provider for this run —
handy to reprocess a batch on a different backend (e.g. compare local vs Groq
output, or rerun on `groq` while LLM GPUs are down). Resolved and policy-checked
at enqueue time, then carried in the job envelope so the workers honour it; see
the note under `POST /v1/posts/upload` above.

### Response — `202 Accepted`

```json
{
  "analysis_id": "an_01J0A...",
  "post_count": 10000,
  "status": "queued",
  "estimated_llm_share": 0.07,
  "status_url": "/v1/analysis/an_01J0A..."
}
```

`estimated_llm_share` previews the expected fraction routed to the LLM — a
transparency feature tied to the hybrid design.

---

## 3. Results — `GET /v1/analysis/{id}`

Poll job/analysis status and fetch results. `{id}` is a `job_id` or `analysis_id`.

### Response — in progress

```json
{
  "id": "an_01J0A...",
  "status": "processing",
  "progress": {
    "total": 10000,
    "completed": 6400,
    "llm_used": 420,
    "failed": 3
  },
  "updated_at": "2026-06-01T12:05:00Z"
}
```

### Response — complete (with `?include=results&limit=2`)

```json
{
  "id": "an_01J0A...",
  "status": "completed",
  "progress": {
    "total": 10000,
    "completed": 9997,
    "llm_used": 680,
    "failed": 3
  },
  "results": [
    {
      "post_id": "cmouf3g7p0dnae4hkfuk6spet",
      "campaign_id": "cmold8r5301u8fu22m7flh3pc",
      "platform": "facebook",
      "platform_post_id": "122161870454710684",
      "media_type": "TEXT",
      "language": "bn",
      "language_mix": ["bn", "banglish", "en"],
      "language_confidence": 0.96,
      "post_type": "opinion",
      "post_summary": "ভারতে মুসলিমদের পরিস্থিতি নিয়ে একটি ক্ষুব্ধ মতামত পোস্ট; মন্তব্যেও ক্ষোভ ও উদ্বেগ প্রবল।",
      "post_summary_lang": "bn",
      "overall_sentiment": "negative",
      "sentiment_score": -0.8,
      "text_sentiment": { "label": "negative", "score": -0.8 },
      "image_sentiment": null,
      "baseline_sentiment": -0.85,
      "baseline_viral_potential": 0.78,
      "emotion": "anger",
      "intents": ["express_grievance", "inform"],
      "topics": ["india", "muslims", "politics"],
      "insight": "The grievance is framed as a national-identity issue rather than a policy one.",
      "entities": [
        { "type": "location", "value": "India", "confidence": 0.93 }
      ],
      "brand_mentions": [],
      "keywords": ["ভারত", "মুসলিম"],
      "toxicity_score": 0.34,
      "hate_speech_score": 0.21,
      "engagement": {
        "reactions": 26700,
        "comment_count": 6567,
        "share_count": 3136,
        "stored_comments": 607
      },
      "reaction_breakdown": {
        "SAD": 13047,
        "LIKE": 11219,
        "ANGRY": 1363,
        "HAHA": 948,
        "LOVE": 80,
        "WOW": 29,
        "CARE": 14
      },
      "comment_analysis": {
        "analyzed": 607,
        "coverage": "607/6567 stored",
        "coverage_anomaly": null,
        "sentiment_breakdown": {
          "positive": 41,
          "negative": 466,
          "neutral": 100
        },
        "sentiment_breakdown_substantive": {
          "positive": 36,
          "negative": 452,
          "neutral": 91
        },
        "reaction_only": 28,
        "provenance": {
          "total": 607,
          "inferred": 579,
          "heuristic": 28,
          "inferred_share": 0.9539,
          "by_method": { "llm": 579, "emoji": 28 }
        },
        "themes": [
          "anger at India's treatment of Muslims",
          "calls for awareness",
          "links shared"
        ]
      },
      "post_summary_source": "llm",
      "confidence": 0.9,
      "processing": {
        "unit": "post+thread",
        "stage1_ms": 120,
        "llm_used": true,
        "llm_role": "LLM-A",
        "llm_backend": "local",
        "llm_model": "Qwen2.5-7B-Instruct"
      },
      "created_at": "2026-05-06T16:45:03"
    }
  ],
  "next_cursor": "eyJvZmZzZXQiOjJ9"
}
```

The result object above is **abridged** — fields like `url`, `scraped_at`,
`post_summary_grounding`, `shares`, and (for image posts) `image_analysis` (per-image
visual sentiment + our OCR + description) and `representative_comments` are omitted
for brevity. (Here `media_type` is `TEXT`, so `image_sentiment` is `null`.) The full
schema and field semantics live in [architecture.md](architecture.md) §6; the
worked examples (real Facebook posts with their embedded comments analyzed) are in
[examples.md](examples.md).

Real-time alternative: `GET /v1/analysis/{id}/stream` (SSE) pushes per-post
results as they complete, for live dashboards.

---

## 4. Reporting — `GET /v1/reports`

List and fetch generated reports (trends, brand mentions, political analysis,
cluster insights). Reports are generated by the **Insight/Analyst agent** at the
_cluster/corpus_ level — a tool-using loop over MCP servers, grounded and cited
(see [architecture.md](architecture.md) §11).

### List — `GET /v1/reports?type=trend&from=2026-06-01&to=2026-06-07`

```json
{
  "reports": [
    {
      "report_id": "rep_88",
      "type": "trend",
      "title": "Weekly trend digest",
      "period": "2026-06-01..2026-06-07",
      "created_at": "2026-06-07T00:10:00Z"
    }
  ],
  "next_cursor": null
}
```

### Fetch — `GET /v1/reports/rep_88`

```json
{
  "report_id": "rep_88",
  "type": "trend",
  "period": "2026-06-01..2026-06-07",
  "summary": "Technology and education topics dominated; positive sentiment rose 12%...",
  "clusters": [
    {
      "cluster_id": "c1",
      "label": "AI in education",
      "post_count": 1840,
      "top_sentiment": "positive",
      "summary": "...",
      "sample_post_ids": ["cmq7orcjr2w78x80tufd0nza4"]
    }
  ],
  "metrics": { "total_posts": 10000, "languages": { "bn": 6200, "en": 3800 } },
  "generated_by": "insight_agent",
  "created_at": "2026-06-07T00:10:00Z"
}
```

### Generate — `POST /v1/reports`

```json
{
  "type": "brand_mentions",
  "filter": { "brand": "BrandX", "from": "...", "to": "..." },
  "options": { "grounded": true }
}
```

→ `202 Accepted` with `report_id` and `status_url`. `grounded: true` runs the
**Insight agent** (MCP retrieval + analytics tools + LLM-B) for citation-backed
output — see [models.md](models.md) §5 and [architecture.md](architecture.md) §11.

---

## 4a. Analyst Q&A (agentic) — `POST /v1/agents/query`

Ask a natural-language question over the analyzed corpus; an agent plans across the
MCP tools (analytics + retrieval, +VLM if images matter) and returns a grounded,
cited answer. Async (`202` + `status_url`) since it may make several tool/LLM calls;
budget-capped per run.

`agent_type` selects which of the **nine** agents runs — `analyst` (the default,
general-purpose one), `coverage`, `alerting`, `stance`, `comparator`, `toxicity`,
`narrative`, `quality`, `reporter`. Each has its own tool allowlist and default
budget; `GET /v1/agents/types` returns the live roster with both, so a client
should read it rather than hard-code a list. `options.max_tool_calls` overrides
the agent's default for one run.

### Request

```json
{
  "question": "What are people saying about the Shapla Chattar posts this month, and how is sentiment trending?",
  "filter": {
    "campaign_id": "cmoldmxzr02d8fu22vhvrg23c",
    "from": "2026-05-01",
    "to": "2026-05-31"
  },
  "options": {
    "max_tool_calls": 12,
    "llm_backend": "auto",
    "want_citations": true
  }
}
```

### Response (on completion)

```json
{
  "answer": "Sentiment is strongly negative (grief + anger); volume peaked around 4–5 May...",
  "citations": [
    {
      "post_id": "cmosjpp9305n0u9tskgmd1c4k",
      "quote": "এই ছবিগুলো প্রমাণ করে...",
      "kind": "comment"
    }
  ],
  "tools_used": [
    "analytics-mcp.trend_query",
    "retrieval-mcp.semantic_search",
    "retrieval-mcp.get_thread"
  ],
  "backend": "local",
  "model": "Qwen2.5-32B-Instruct",
  "usage": { "tool_calls": 7, "llm_tokens": 4200 }
}
```

`tools_used`/`backend`/`usage` make each agent run auditable (ties into `/v1/usage`).
Tenant `local`-pinning applies — a privacy-locked tenant's retrieved content never
egresses to Groq.

---

## 5. Supporting endpoints

| Endpoint                           | Purpose                                                                                |
| ---------------------------------- | -------------------------------------------------------------------------------------- |
| `POST /v1/auth/token`              | Verify credentials against the `users` table → JWT (`exp` 1h). `tenant_id`/`role` come from the row, never the request |
| `POST /v1/auth/refresh`            | Sliding session — exchange a valid token for a fresh one. What makes a 1-hour `exp` practical |
| `GET /v1/auth/me`                  | The authenticated principal. Lets a client distinguish "no token" from "expired token" |
| `POST /v1/auth/sse-ticket`         | Single-use, ~60s, hash-stored ticket for `EventSource` — which cannot send headers      |
| `GET /v1/health` / `GET /v1/ready` | Liveness / readiness probes                                                            |
| `GET /v1/usage`                    | Per-tenant usage + cost metering (posts, LLM calls, by backend incl. Groq tokens/cost) |
| `GET /v1/search?q=&semantic=true`  | Semantic/keyword search over analyzed posts (pgvector + ClickHouse)                    |
| `GET /v1/agents/{id}`              | Poll an agent run (analyst query / report) — status, answer, citations, usage          |
| `POST /v1/chat`                    | Free-form chatbot over the pipeline LLM (backend follows the toggle)                   |
| `POST /v1/chat/stream`             | Same as `/v1/chat` but streams the reply token-by-token (SSE)                          |
| `GET /v1/chat/models`              | Models available per backend (+ defaults) for the chat model picker                    |
| `DELETE /v1/posts/{id}`            | Data deletion (retention / GDPR-style)                                                 |

> This table is the **designed** surface. The app currently serves 47 distinct
> `/v1` paths; [endpoints.md](endpoints.md) §3b lists the ones not covered here —
> chat conversations, report export, pipeline stats, log streaming and the agent
> registry introspection routes.

> The **MCP servers** (`analytics-mcp`, `retrieval-mcp`, `ingest-mcp` — see
> [architecture.md](architecture.md) §11) are **internal** tool interfaces consumed
> by the agent orchestrator, not part of this public REST surface. They speak MCP
> (stdio/HTTP) and enforce the same auth/tenant scoping.

### Authentication, in one paragraph

Three credential types, and **a JWT-shaped credential is verified as a token on
every transport** — `Authorization`, `X-API-Key`, or the `?api_key=` query
parameter. Only the header used to be parsed as a token, so an expired or forged
token authenticated on all four SSE streams. **API keys** are looked up by
SHA-256 hash in `api_keys`, and `tenant_id` comes from that row rather than from
anything the client sent — which is what lets the privacy-locked-tenant policy
bind to API-key callers at all. **SSE tickets** are the preferred streaming
credential. Claims taken from a token body are allowlisted (`sub`, `tenant_id`,
`role`, `exp`, `iat`, `scope`) and `auth_method` is set server-side afterwards,
because the payload used to be spread last and any claim in the token won.

---

## 6. Error envelope

```json
{
  "error": {
    "code": "validation_error",
    "message": "post[1].caption exceeds max length",
    "request_id": "req_01J0...",
    "details": [{ "field": "posts[1].caption", "issue": "too_long" }]
  }
}
```

Standard codes: `unauthorized`, `forbidden` (an `llm_backend` override that
violates tenant data-residency policy, or a non-admin selecting `groq` on the
global `PUT /v1/config/llm` toggle), `validation_error`, `rate_limited` (with
`Retry-After` — also surfaced when the `groq` backend returns HTTP 429),
`not_found`, `conflict` (idempotency), `internal`. The service handles a saturated
or failed backend internally (failover/degrade per
[architecture.md](architecture.md) §8) rather than surfacing a raw upstream error
where possible.

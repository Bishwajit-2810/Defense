# API Design — REST Contracts

REST API for the smart-layer microservice: ingestion of **post + comment
threads**, batch processing, structured results, and reporting. JSON over HTTPS.
Auth via `Authorization: Bearer <JWT>` or `X-API-Key: <key>` (see
[architecture.md](architecture.md) §9). All endpoints are versioned under `/v1`.
For full real input→output examples see [examples.md](examples.md).

> **Two ingestion paths.** The **primary** path is a **pull** from the upstream
> platform's Post/Comment APIs into our own database (§1a) — the real input
> contract is in [data_contract.md](data_contract.md). The **`/v1/posts/upload`**
> push path (§1b) remains for external/replay sources. These read/analysis/report
> endpoints below are what *our* downstream consumers call.

Conventions:

- `202 Accepted` for async work (returns a job/analysis id to poll or subscribe).
- Idempotency via `Idempotency-Key` header on writes.
- Pagination via `?limit=&cursor=`. Errors use a consistent envelope.

---

## 1a. Ingestion (primary) — pull from upstream — `POST /v1/ingest/sync`

The primary input is a **pull** from the existing platform's **Post API** (and,
per post, its **Comment API**, joined by the post's unique `id`) into our own
database. The real upstream schemas, the join, and the integration model are in
[data_contract.md](data_contract.md). This endpoint **triggers** a pull for a
selector (campaign / time window / `status`); the service then fetches, copies,
and analyzes — **post sentiment first, then comments** — with no write-back to
upstream.

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
    "campaign_id": "cmpe1djj504zc4otgw94idx0v",
    "status": "NOT_ANALYZED",
    "posted_from": "2026-06-10T00:00:00",
    "posted_to": "2026-06-11T00:00:00"
  },
  "pull_comments": true,
  "options": {
    "tasks": ["all"],
    "want_summary": true,
    "summary_lang": "auto",
    "llm_backend": "auto"
  }
}
```

The service reads upstream records keyed by their CUID `id`, derives `platform`
from each `url` host, keeps upstream `sentiment`/`viralPotential` as `baseline_*`,
and **recomputes** richer sentiment ([data_contract.md](data_contract.md) §4).
`pull_comments: true` fetches each post's comments by `postId == id`; set it
`false` to run the **post-only** pass (the path available before the Comment API
is wired). `summary_lang: "auto"` keeps the summary in the post's detected
language. Re-pulling the same `id` upserts (idempotent), never duplicates.

---

## 1b. Ingestion (push, optional) — `POST /v1/posts/upload`

For external/replay sources, callers may **push** posts (and optional comments)
directly, instead of pulling from upstream. Records use the **same upstream field
names** as the Post API ([data_contract.md](data_contract.md) §1) so a raw Post-API
response can be replayed verbatim. Comments may be attached inline as a flat list
(each with `id`, `postId`, `parentId`) or pulled separately later.

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
      "id": "cmq7grn1cmplnt0xmpl0a1b2c",
      "campaignId": "cmpgrn1cmplnt0xmpl0camp01",
      "platformPostId": "1402233557981234",
      "url": "https://www.facebook.com/...",
      "caption": "গ্রিন গার্ডেন এ খাবারের দাম অনেক বেশি... একটা সিংগারা ২০ টাকা চাইল।",
      "photoUrls": [],
      "photoOcrTexts": [],
      "postType": "TEXT",
      "postedAt": "2026-06-10T05:47:00",
      "scrapedAt": "2026-06-10T06:26:40.838",
      "commentCount": 12,
      "shareCount": 4,
      "totalReactions": 48,
      "sentiment": -0.3,
      "viralPotential": 0.25,
      "status": "NOT_ANALYZED",
      "comments": [
        { "id": "cmcmt001", "postId": "cmq7grn1cmplnt0xmpl0a1b2c", "parentId": null,
          "text": "Green garden e sudhu polao 100 taka baire 30-40 takai e paua jay", "postedAt": "2026-06-10T07:00:00" },
        { "id": "cmcmt002", "postId": "cmq7grn1cmplnt0xmpl0a1b2c", "parentId": "cmcmt001",
          "text": "Ami agee breakfast kortam green garden e. Ekhn oitao baad disi.", "postedAt": "2026-06-10T07:30:00" }
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

`comments` is optional (a bare post with no thread is valid — the post pass runs
standalone). Banglish comments (romanized Bangla, as above) are handled natively.
`platform` and `baseline_*` are derived on ingest exactly as in the pull path.

`llm_backend` selects the Stage-2 LLM provider for this request: `"local"`
(self-hosted vLLM), `"groq"` (Groq Cloud API), or `"auto"` (default — use the
server's configured backend and failover policy). A per-request override lets
callers pin sensitive data to `"local"` or send burst traffic to `"groq"` without
changing server config. **Tenant policy wins:** a tenant pinned to `local` for
data-residency cannot be overridden to `groq` by a request (the override is
rejected with `forbidden`). See [models.md](models.md) §2.

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
`["text_sentiment","image_sentiment","ner","toxicity"]` or `["all"]`).
`image_sentiment` runs the visual model on image posts (no-op for text-only);
`want_summary`/`want_insight` opt into the LLM/VLM tasks (the summary is
image-grounded for photo posts). Otherwise the router keeps work on the cheap path
unless confidence is low.

---

## 2. Batch processing — `POST /v1/analysis/run`

Trigger (or re-trigger) analysis for already-ingested posts, or run a saved
batch with specific options — useful for reprocessing after a model upgrade.

### Request

```json
{
  "selector": { "job_id": "job_01HZX..." },
  "options": {
    "tasks": ["text_sentiment", "image_sentiment", "emotion", "topics", "ner", "toxicity"],
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
`llm_backend` (`auto` | `local` | `groq`) overrides the Stage-2 provider for this
run — handy to reprocess a batch on a different backend (e.g. compare local vs
Groq output, or rerun on `groq` while LLM GPUs are down), subject to tenant policy.

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
      "post_id": "cmq7grn1cmplnt0xmpl0a1b2c",
      "campaign_id": "cmpgrn1cmplnt0xmpl0camp01",
      "platform": "facebook",
      "platform_post_id": "1402233557981234",
      "media_type": "TEXT",
      "language": "bn",
      "language_mix": ["bn", "banglish", "en"],
      "language_confidence": 0.97,
      "post_type": "complaint",
      "post_summary": "গ্রিন গার্ডেন ও ট্রান্সপোর্টে খাবারের দাম বাইরের তুলনায় অনেক বেশি; পোস্টদাতা কেনা বন্ধ ও বয়কটের ডাক দিয়েছেন।",
      "post_summary_lang": "bn",
      "overall_sentiment": "negative",
      "sentiment_score": -0.64,
      "text_sentiment": { "label": "negative", "score": -0.64 },
      "image_sentiment": null,
      "baseline_sentiment": -0.3,
      "baseline_viral_potential": 0.25,
      "emotion": "anger",
      "intents": ["complaint", "call_to_action"],
      "topics": ["food pricing", "campus transport", "boycott"],
      "entities": [
        { "type": "organization", "value": "Green Garden", "confidence": 0.94 }
      ],
      "brand_mentions": [
        { "name": "Green Garden", "sentiment": "negative", "mentions": 9 }
      ],
      "keywords": ["দাম", "সিঙ্গারা", "boycott"],
      "toxicity_score": 0.07,
      "hate_speech_score": 0.01,
      "engagement": { "reactions": 48, "comment_count": 12 },
      "comment_analysis": {
        "analyzed": 12,
        "sentiment_breakdown": { "positive": 1, "negative": 9, "neutral": 2 },
        "themes": ["prices above market", "same quality cheaper outside", "boycott calls"]
      },
      "post_summary_source": "llm",
      "confidence": 0.92,
      "processing": { "unit": "post+thread", "stage1_ms": 58, "llm_used": true, "llm_role": "LLM-A", "llm_backend": "local", "llm_model": "Qwen2.5-7B-Instruct" },
      "created_at": "2026-06-01T10:00:00Z"
    }
  ],
  "next_cursor": "eyJvZmZzZXQiOjJ9"
}
```

The result object above is **abridged** — fields like `url`, `author`,
`scraped_at`, `upstream_status`, `post_summary_grounding`, `image_analysis` (per-image
visual sentiment + OCR + description, present for image posts), and
`comment_analysis.representative_comments` are omitted for brevity. (Here
`media_type` is `TEXT`, so `image_sentiment` is `null`.) The full schema and field
semantics live in
[architecture.md](architecture.md) §6; the worked examples (a Facebook Bangla
thread and an X English post) are in [examples.md](examples.md).

Real-time alternative: `GET /v1/analysis/{id}/stream` (SSE) pushes per-post
results as they complete, for live dashboards.

---

## 4. Reporting — `GET /v1/reports`

List and fetch generated reports (trends, brand mentions, political analysis,
cluster insights). Reports are LLM-generated at the _cluster/corpus_ level.

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
  "generated_by": "llm",
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

→ `202 Accepted` with `report_id` and `status_url`. `grounded: true` uses RAG
(Qdrant retrieval + LLM) for citation-backed output — see [models.md](models.md).

---

## 5. Supporting endpoints

| Endpoint                           | Purpose                                                                                |
| ---------------------------------- | -------------------------------------------------------------------------------------- |
| `POST /v1/auth/token`              | Exchange credentials/API key for a JWT                                                 |
| `GET /v1/health` / `GET /v1/ready` | Liveness / readiness probes                                                            |
| `GET /v1/usage`                    | Per-tenant usage + cost metering (posts, LLM calls, by backend incl. Groq tokens/cost) |
| `GET /v1/search?q=&semantic=true`  | Semantic/keyword search over analyzed posts (Qdrant + ClickHouse)                      |
| `DELETE /v1/posts/{id}`            | Data deletion (retention / GDPR-style)                                                 |

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

Standard codes: `unauthorized`, `forbidden` (incl. an `llm_backend` override that
violates tenant data-residency policy), `validation_error`, `rate_limited` (with
`Retry-After` — also surfaced when the `groq` backend returns HTTP 429),
`not_found`, `conflict` (idempotency), `internal`. The service handles a saturated
or failed backend internally (failover/degrade per
[architecture.md](architecture.md) §8) rather than surfacing a raw upstream error
where possible.

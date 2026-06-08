# API Design — REST Contracts

REST API for the smart-layer microservice: ingestion of scraped **post + comment
threads**, batch processing, structured results, and reporting. JSON over HTTPS.
Auth via `Authorization: Bearer <JWT>` or `X-API-Key: <key>` (see
[architecture.md](architecture.md) §9). All endpoints are versioned under `/v1`.
For full real input→output examples see [examples.md](examples.md).

Conventions:

- `202 Accepted` for async work (returns a job/analysis id to poll or subscribe).
- Idempotency via `Idempotency-Key` header on writes.
- Pagination via `?limit=&cursor=`. Errors use a consistent envelope.

---

## 1. Ingestion — `POST /v1/posts/upload`

Upload one or many **threads** inline (a parent post plus its comments/replies),
or register a large JSONL file already in object storage (presigned upload) for
batch. The comment tree may be nested; the service flattens it but preserves
`parent_id` so reply structure is recoverable.

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
      "post_id": "fb_12345",
      "platform": "facebook",
      "url": "https://facebook.com/...",
      "author": "TalentedOstrich6332",
      "text": "গ্রিন গার্ডেন এ খাবারের দাম অনেক বেশি... একটা সিংগারা ২০ টাকা চাইল।",
      "created_at": "2026-06-01T10:00:00Z",
      "engagement": { "reactions": 48, "comment_count": 12 },
      "comments": [
        {
          "comment_id": "c1",
          "parent_id": null,
          "author": "GenuineJackfruit1970",
          "text": "Green garden e sudhu polao 100 taka baire 30-40 takai e paua jay",
          "created_at": "2026-06-01T13:00:00Z"
        },
        {
          "comment_id": "c2",
          "parent_id": "c1",
          "author": "TalentedOstrich6332",
          "text": "Ami agee breakfast kortam green garden e. Ekhn oitao baad disi.",
          "created_at": "2026-06-01T13:30:00Z"
        }
      ],
      "meta": { "page_id": "p_99", "lang_hint": "bn" }
    }
  ],
  "options": { "tasks": ["all"], "want_summary": true, "summary_lang": "auto" }
}
```

`comments` is optional (a bare post with no thread is valid). `summary_lang:
"auto"` keeps the summary in the post's detected language; pass `"bn"`/`"en"` to
force it. Banglish comments (romanized Bangla, as above) are handled natively.

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

`options.tasks` selects analyses (e.g. `["sentiment","ner","toxicity"]` or
`["all"]`). `want_summary`/`want_insight` opt into LLM tasks; otherwise the
router keeps work on the cheap path unless confidence is low.

---

## 2. Batch processing — `POST /v1/analysis/run`

Trigger (or re-trigger) analysis for already-ingested posts, or run a saved
batch with specific options — useful for reprocessing after a model upgrade.

### Request

```json
{
  "selector": { "job_id": "job_01HZX..." },
  "options": {
    "tasks": ["sentiment", "emotion", "topics", "ner", "toxicity"],
    "want_summary": true,
    "want_cluster_summary": true,
    "model_profile": "default"
  }
}
```

`selector` may instead be `{ "post_ids": [...] }` or
`{ "filter": { "platform": "instagram", "from": "...", "to": "..." } }`.

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
      "post_id": "fb_12345",
      "platform": "facebook",
      "author": "TalentedOstrich6332",
      "language": "bn",
      "language_mix": ["bn", "banglish", "en"],
      "language_confidence": 0.97,
      "post_type": "complaint",
      "post_summary": "গ্রিন গার্ডেন ও ট্রান্সপোর্টে খাবারের দাম বাইরের তুলনায় অনেক বেশি; পোস্টদাতা কেনা বন্ধ ও বয়কটের ডাক দিয়েছেন।",
      "post_summary_lang": "bn",
      "overall_sentiment": "negative",
      "sentiment_score": -0.64,
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
      "processing": { "unit": "post+thread", "stage1_ms": 58, "llm_used": true, "llm_model": "LLM-A" },
      "created_at": "2026-06-01T10:00:00Z"
    }
  ],
  "next_cursor": "eyJvZmZzZXQiOjJ9"
}
```

The full schema and field semantics live in
[architecture.md](architecture.md) §6; two end-to-end worked examples (a complaint
thread and a brand-page thread) are in [examples.md](examples.md).

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
      "sample_post_ids": ["fb_12345"]
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

| Endpoint                           | Purpose                                                           |
| ---------------------------------- | ----------------------------------------------------------------- |
| `POST /v1/auth/token`              | Exchange credentials/API key for a JWT                            |
| `GET /v1/health` / `GET /v1/ready` | Liveness / readiness probes                                       |
| `GET /v1/usage`                    | Per-tenant usage + cost metering (posts, LLM calls)               |
| `GET /v1/search?q=&semantic=true`  | Semantic/keyword search over analyzed posts (Qdrant + ClickHouse) |
| `DELETE /v1/posts/{id}`            | Data deletion (retention / GDPR-style)                            |

---

## 6. Error envelope

```json
{
  "error": {
    "code": "validation_error",
    "message": "post[1].text exceeds max length",
    "request_id": "req_01J0...",
    "details": [{ "field": "posts[1].text", "issue": "too_long" }]
  }
}
```

Standard codes: `unauthorized`, `forbidden`, `validation_error`, `rate_limited`
(with `Retry-After`), `not_found`, `conflict` (idempotency), `internal`.

# endpoints.md — the output JSON & how to test every endpoint

This is the hands-on guide for **getting the custom analysis JSON out of the
system** (post summary, sentiment, topics, toxicity, comment analysis — the
full canonical result) and for **testing every API endpoint with curl**.

The canonical schema lives in
[libs/schemas/output_schema.json](libs/schemas/output_schema.json); the field
meanings are specified in [data_contract.md](data_contract.md) §4. This file
shows what actually comes over the wire and how to reshape it.

Everything below assumes the dev stack from [easy_run.md](easy_run.md):

```bash
uv run run_all.py --with-agents     # API on http://127.0.0.1:8001
export API=http://127.0.0.1:8001
export KEY="X-API-Key: demo"        # dev mode: any non-empty key works
```

---

## 1. The analysis result JSON (what every post becomes)

Every ingested post is transformed into **one `AnalysisResult` object**.
This is the JSON the whole system exists to produce:

```jsonc
{
  // ---- identity -------------------------------------------------------
  "post_id":          "cmoxb5qi102luv0toasaz5ofh",  // upstream post CUID
  "campaign_id":      "cmoldmxzr02d8fu22vhvrg23c",
  "platform":         "FACEBOOK",
  "platform_post_id": "1234567890",
  "media_type":       "PHOTO",                      // TEXT | PHOTO | VIDEO

  // ---- language & summary (Stage-2 LLM/VLM) ---------------------------
  "language":               "bn",                   // bn | en | banglish | mixed | und
  "post_type":              "political",            // complaint|news|opinion|promotion|humor|personal|political|religious|other
  "post_summary":           "পোস্টটি জ্বালানি তেলের মূল্যবৃদ্ধি নিয়ে সরকারের সমালোচনা করছে…",
  "post_summary_lang":      "bn",                   // language the summary was written in
  "post_summary_source":    "vlm",                  // "vlm" = image-grounded, "llm" = text-only, null = no summary
  "post_summary_grounding": "caption+ocr+image",    // which inputs grounded the summary

  // ---- sentiment (multimodal) ------------------------------------------
  "overall_sentiment":  "negative",                 // positive | negative | neutral | mixed
  "sentiment_score":    -0.62,                      // -1.0 … 1.0
  "text_sentiment":     "negative",
  "image_sentiment":    "neutral",                  // null for text-only posts
  "baseline_sentiment": -0.5,                       // upstream platform's own score (for comparison)

  // ---- classification ---------------------------------------------------
  "emotion":   { "anger": 0.55, "sadness": 0.2, "joy": 0.05 },
  "intents":   ["criticism", "mobilization"],
  "topics":    ["fuel prices", "government policy"],
  "entities":  [ { "type": "ORG", "value": "BPC" } ],
  "brand_mentions": [],
  "keywords":  ["তেল", "দাম", "সরকার"],
  "toxicity_score":    0.12,                        // 0.0 … 1.0
  "hate_speech_score": 0.02,

  // ---- engagement (passed through + computed) ---------------------------
  "engagement": {
    "comment_count":   1843,                        // platform total
    "stored_comments": 25,                          // embedded sample we analyzed
    "total_reactions": 5120,
    "share_count":     230
  },
  "reaction_breakdown": { "LIKE": 3200, "ANGRY": 1100, "HAHA": 520 },

  // ---- image analysis (photo posts only) --------------------------------
  "image_analysis": {
    "ocr_text":    "দাম বাড়লো আবার",
    "description": "A petrol pump price board",
    "sentiment":   { "label": "neutral", "score": 0.1 }
  },

  // ---- per-comment rollup ------------------------------------------------
  "comment_analysis": {
    "analyzed": 25,                                 // comments we actually analyzed
    "coverage": 0.0136,                             // analyzed / comment_count
    "sentiment_breakdown": { "positive": 3, "negative": 14, "neutral": 8 },
    "themes": ["price hike", "sarcasm"],
    "top_keywords": ["dam", "taka"],
    "representative_comments": [
      { "text": "Eto dam dile cholbo kemne", "lang": "banglish", "sentiment": "negative", "likes": 89 }
    ]
  },

  // ---- trust & audit ------------------------------------------------------
  "confidence": { "overall": 0.81, "sentiment": 0.86, "language": 0.95, "topics": 0.7 },
  "processing": {
    "stage1_ms":   142,
    "stage2_ms":   2210,
    "llm_used":    true,                            // false = Stage-1-only (cheap path)
    "llm_backend": "local",                         // local | groq
    "llm_model":   "qwen3-vl:4b",
    "schema_version": "1.0"
  },
  "created_at": "2026-06-12T10:00:00Z",
  "scraped_at": "2026-06-10T08:00:00Z"
}
```

Field-level rules worth knowing when consuming this JSON:

| Field | Rule |
| --- | --- |
| `post_summary*` | `null` when the router skipped Stage-2 (no LLM needed). Force a summary for every post with `options.want_summary: true` at upload/run time. |
| `post_summary_source` | `"vlm"` only when the image actually grounded the summary; a VLM failure degrades to text-only and reports `"llm"`. |
| `comment_analysis.coverage` | Honest fraction — we analyze the embedded sample (`stored_comments`), never pretend full coverage. |
| `processing.llm_used` | The cost lever: only routed posts ([router rules](services/workers/router/rules.py)) carry Stage-2 latency/cost. |

---

## 2. Get the JSON — main endpoints

### 2a. Push posts in → get a job

```bash
curl -s -X POST $API/v1/posts/upload -H "$KEY" -H "Content-Type: application/json" -d '{
  "posts": '"$(cat posts_with_details.json)"',
  "options": {
    "want_summary": true,        // LLM summary for EVERY post (else router decides)
    "target_lang":  "bn",        // force summary language (default: post language)
    "llm_backend":  "auto"       // "local" | "groq" | "auto" (tenant policy enforced)
  }
}'
# → 202 {"job_id": "…", "status": "pending", "count": 50, "status_url": "…/v1/jobs/…"}
```

`options` is how you customize the output JSON per request — it rides with
every post through the pipeline.

### 2b. Track the job (poll or stream)

```bash
# Poll — now includes a live progress object
curl -s -H "$KEY" $API/v1/analysis/<job_id> | python -m json.tool
# → {"status": "running", "progress": {"total": 50, "completed": 23, "failed": 0}, …}

# Or stream Server-Sent Events (one event per analyzed post, then "done")
curl -N "$API/v1/analysis/<job_id>/stream?api_key=demo"
# event: progress
# data: {"job_id":"…","post_id":"…","completed":24,"failed":0,"total":50,"overall_sentiment":"negative"}
# event: done
# data: {"completed":50,"failed":0,"total":50,…}
```

### 2c. Pull the result JSON

```bash
# Newest results across everything (backs the dashboard Posts tab)
curl -s -H "$KEY" "$API/v1/analysis/latest?limit=50" | python -m json.tool

# Filter by campaign
curl -s -H "$KEY" "$API/v1/analysis/latest?limit=50&campaign_id=<cid>"

# Results for one specific job
curl -s -H "$KEY" "$API/v1/analysis/<job_id>?include=results&limit=100"
```

All three return `{"results": [ <AnalysisResult>, … ]}` — the §1 JSON.

### 2d. Extract YOUR custom JSON shape

The API returns the full canonical object; cut it down to whatever shape you
need with `jq` (or any JSON tool). Examples:

```bash
# → [{id, summary, sentiment, score, topics}, …]
curl -s -H "$KEY" "$API/v1/analysis/latest?limit=50" | jq '[.results[] | {
  id:        .post_id,
  summary:   .post_summary,
  sentiment: .overall_sentiment,
  score:     .sentiment_score,
  topics:    .topics
}]'

# → only LLM-summarized negative posts, with comment stats
curl -s -H "$KEY" "$API/v1/analysis/latest?limit=200" | jq '[.results[]
  | select(.overall_sentiment == "negative" and .post_summary != null)
  | { id: .post_id, summary: .post_summary,
      negative_comments: .comment_analysis.sentiment_breakdown.negative,
      toxicity: .toxicity_score }]'

# → CSV: post_id, language, sentiment, summary
curl -s -H "$KEY" "$API/v1/analysis/latest?limit=200" \
  | jq -r '.results[] | [.post_id, .language, .overall_sentiment, (.post_summary // "")] | @csv'
```

---

## 3. Test the rest of the API

### Health / auth

```bash
curl -s $API/v1/health                       # {"status":"ok"} — no auth needed
curl -s $API/v1/ready                        # checks Postgres + Redis
curl -s -X POST $API/v1/auth/token -H "Content-Type: application/json" \
  -d '{"username":"demo","password":"demo"}' # → {"access_token": "<jwt>"}
```

### Search (keyword & semantic)

```bash
curl -s -H "$KEY" "$API/v1/search?q=politics&semantic=false&limit=10" | python -m json.tool
curl -s -H "$KEY" "$API/v1/search?q=fuel%20price%20anger&semantic=true&limit=10"
# → {"query":"…","semantic":true,"total":N,"results":[{"post_id","score","snippet","result":{<full §1 JSON>}}]}
```

### Reports (grounded = LLM-written executive summary)

```bash
curl -s -X POST $API/v1/reports -H "$KEY" -H "Content-Type: application/json" \
  -d '{"type":"trend","options":{"grounded":true}}' | python -m json.tool
# → {"report_id":"…","status":"done","summary":"<LLM-B narrative>","summary_source":"llm",
#    "clusters":[{"label":"fuel prices","size":12,"top_sentiment":"negative"},…],
#    "metrics":{"total_posts":50,"sentiment_breakdown":{…},"languages":{…}}}
# "summary_source":"aggregate" means the LLM was unreachable and the SQL summary was kept.

curl -s -H "$KEY" $API/v1/reports                  # list
curl -s -H "$KEY" $API/v1/reports/<report_id>      # fetch one
```

### Usage & cost telemetry

```bash
curl -s -H "$KEY" $API/v1/usage | python -m json.tool
# → {"posts_analyzed":50,"llm_calls":50,"llm_routing_rate":1.0,
#    "total_tokens":48211,"estimated_cost_usd":0.0964,
#    "llm_api_calls":117,"cache_hits":33,"cache_hit_rate":0.22}
```

### LLM backend (runtime switch)

```bash
curl -s -H "$KEY" $API/v1/config/llm                                   # current backend + models
curl -s -X PUT $API/v1/config/llm -H "$KEY" -H "Content-Type: application/json" \
  -d '{"backend":"groq"}'                                              # switch (needs GROQ_API_KEY)
curl -s -X PUT $API/v1/config/llm -H "$KEY" -H "Content-Type: application/json" \
  -d '{"backend":null}'                                                # back to default (local)
```

### Jobs list

```bash
curl -s -H "$KEY" "$API/v1/analysis?limit=20"      # recent ingest/analysis jobs
```

### Re-analyze already-ingested posts

```bash
curl -s -X POST $API/v1/analysis/run -H "$KEY" -H "Content-Type: application/json" -d '{
  "campaign_id": "<cid>",
  "options": {"want_summary": true, "want_insight": true, "target_lang": "en"}
}'
# → 202 {"analysis_id":"…","estimated_llm_share":1.0,"status_url":"…"}
```

### Agents (natural-language Q&A — needs `--with-agents`)

```bash
curl -s -X POST $API/v1/agents/query -H "$KEY" -H "Content-Type: application/json" \
  -d '{"agent_type":"analyst","query":"What are the dominant negative topics this week?"}' \
  | python -m json.tool
# 200 → {"run_id":"…","status":"done","answer":"…","citations":["<post_id>…"],"tools_used":[…]}
# 202 → still running; poll:
curl -s -H "$KEY" $API/v1/agents/<run_id>
curl -s -H "$KEY" "$API/v1/agents?limit=10"        # recent runs
```

### Delete a post (GDPR / cleanup)

```bash
curl -s -X DELETE -H "$KEY" $API/v1/posts/<post_id>
# → {"post_id":"…","deleted":{"comments":25,"analysis_results":1,"posts":1}}
```

### Tenant privacy policy (403 demo)

If `tenant_policies.privacy_locked = true` for the tenant, any request with
`options.llm_backend: "groq"` is rejected:

```bash
docker exec deploy-postgres-1 psql -U defense -d defense -c \
  "INSERT INTO tenant_policies (tenant_id, llm_backend, privacy_locked) \
   VALUES ('default','local',true) ON CONFLICT (tenant_id) DO UPDATE SET privacy_locked=true;"
curl -s -X POST $API/v1/analysis/run -H "$KEY" -H "Content-Type: application/json" \
  -d '{"campaign_id":"<cid>","options":{"llm_backend":"groq"}}'
# → 403 {"detail":"Tenant 'default' is privacy-locked: llm_backend='groq' is not permitted …"}
```

---

## 4. Interactive docs

FastAPI serves the full OpenAPI spec with a try-it-out UI:

- Swagger UI → <http://127.0.0.1:8001/docs>
- ReDoc → <http://127.0.0.1:8001/redoc>
- Raw spec → <http://127.0.0.1:8001/openapi.json>

The dashboard (<http://127.0.0.1:8080>) exercises all of the above visually —
Overview (usage/corpus stats), Posts, Jobs (live SSE progress), Reports,
Search, and Agents tabs.

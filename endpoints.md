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
  "post_summary_grounding": "caption",              // which inputs grounded the summary
  "post_summary_truncated": false,                  // true = hit the token ceiling even after auto-continuation; text trimmed to its last complete sentence

  // ---- sentiment -------------------------------------------------------
  // Fusion weights renormalise over the terms that carry a real model verdict,
  // so an absent image term does NOT shrink the text signal toward neutral.
  "overall_sentiment":  "negative",                 // positive | negative | neutral | mixed
  "sentiment_score":    -0.62,                      // -1.0 … 1.0
  "text_sentiment":     "negative",
  "image_sentiment":    null,                       // null for text-only posts AND whenever no vision model produced a verdict
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
    "coverage": 0.0136,                             // analyzed / comment_count, CLAMPED to 1.0
    "coverage_anomaly": null,                       // set when stored rows exceed the platform's reported commentCount
    "sentiment_breakdown": { "positive": 3, "negative": 14, "neutral": 8 },   // ALL comments
    "sentiment_breakdown_substantive": { "positive": 2, "negative": 13, "neutral": 6 },  // written comments only
    "reaction_only": 4,                             // emoji-only reactions — kept as signal, never sent to an LLM
    "method_breakdown": { "llm": 21, "emoji": 4 },  // only the methods that actually ran
    "target_stances": {                             // NEW — watchlist entities (stance_targets.md)
      "entity_a": {                                 // absent entirely if nobody mentioned it
        "display": "Entity A", "polarity": "neutral",
        "mentions": 12, "supportive": 3, "opposing": 8, "neutral": 1,
        "method": "llm",
        "aliases_matched": { "এন্টিটি এ": 9, "entity a": 3 }
      }
    },
    "provenance": {                                 // where these labels came from
      "total": 25, "inferred": 21, "heuristic": 4, "inferred_share": 0.84,
      "by_method": { "llm": 21, "emoji": 4 }
    },
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
| `post_summary_source` | `"vlm"` only when the image actually grounded the summary; a VLM failure degrades to text-only and reports `"llm"`. Currently always `"llm"` — no image bytes are reachable (PROJECT_ASSESSMENT §5.2). |
| `post_summary_truncated` | `true` when the model hit its token ceiling even after auto-continuation. The text is trimmed to its last complete sentence and **never cached**, so a later run can retry with a bigger budget. |
| `comment_analysis.coverage` | Honest fraction — we analyze the embedded sample (`stored_comments`), never pretend full coverage. **Clamped to 1.0**: five posts store more comments than the platform reports, which used to render as "267% coverage". The excess surfaces in `coverage_anomaly`. |
| `comment_analysis.provenance` | Where the labels came from. `inferred` = a model or LLM produced it; `heuristic` = the emoji/lexicon fast path or the deterministic stub. Quote this next to any sentiment chart — in stub mode most labels are not model output. |
| `comment_analysis.method` (per comment) | `llm` \| `model` \| `stub` \| `fast` \| `emoji` \| `failed`. `stub` is a hash of the text — deterministic and reproducible, and *not* sentiment. |
| `comment_analysis.kind` (per comment) | `substantive` \| `short` \| `emoji`. Emoji-only comments keep a sentiment but never enter an LLM batch. |
| `image_analysis.vision_status` | `ok` is the only value that licenses a claim about image sentiment; `stub` / `fetch_failed` / `model_unavailable` / `model_failed` are absences, not neutral verdicts. |
| `processing.role_models` | `{role: resolved model id}` — summarization and classification can run on different models, so a single `llm_model` cannot attribute the summary. |
| `processing.degraded_components` | Real-mode components that fell back to a heuristic because their model would not load. `nlp_engine` says which path was *intended*; this says what **ran**. **A non-empty list means no latency or accuracy figure from that run is quotable.** |
| `comment_analysis.target_stances` | Per-watchlist-entity stance rollup. A **separate measurement** from `sentiment_breakdown` — a comment can be positive in tone while opposing a listed entity, so the two must never be summed or merged. Entities nobody mentioned are **absent**, not zero-filled. |
| `comment_analysis.comments[].emotion_method` | `heuristic` \| `llm`. Comment emotion is the free emoji+lexicon heuristic at Stage 1 **even in real mode**; only comments Stage 2 re-labelled carry a model emotion. |
| `language_method` | `fasttext` \| `script_heuristic` \| `stub`. fastText is optional; without it the pipeline degrades to script detection rather than failing the post. |
| `processing.llm_used` | Only routed posts ([router rules](services/workers/router/rules.py)) carry Stage-2 latency/cost. Note this is **not** the whole cost lever any more: comment labelling runs for every post and is 85–96% of LLM calls (PROJECT_ASSESSMENT §6.8). |

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

# Log in. Credentials are verified against the `users` table when rows exist;
# with none, dev accepts anything and logs a loud warning (ALLOW_ANY_LOGIN).
TOKEN=$(curl -s -X POST $API/v1/auth/token -H "Content-Type: application/json" \
  -d '{"username":"demo","password":"demo"}' | python -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')

# Who am I — lets a client tell "no token" from "expired token".
curl -s -H "Authorization: Bearer $TOKEN" $API/v1/auth/me | python -m json.tool
# → {"sub":"demo","tenant_id":"default","role":"user","auth_method":"jwt", ...}

# Sliding session: exchange a valid token for a fresh one. `exp` is 1 hour
# (not 24) precisely because this exists.
curl -s -X POST -H "Authorization: Bearer $TOKEN" $API/v1/auth/refresh
```

#### Streaming: use a ticket, not your session credential

`EventSource` cannot set headers, so a streaming client needs *something* in the
URL. Putting the session credential there is the wrong something — URLs land in
proxy logs, browser history and `Referer` headers, and that credential is good
for an hour. A **ticket** is single-use, ~60 seconds, and stored hashed:

```bash
TICKET=$(curl -s -X POST -H "Authorization: Bearer $TOKEN" $API/v1/auth/sse-ticket \
  | python -c 'import sys,json;print(json.load(sys.stdin)["ticket"])')
curl -Ns "$API/v1/analysis/<job-id>/stream?ticket=$TICKET"
```

Redeeming deletes the ticket, so a leaked URL is worthless immediately after
first use. The legacy `?api_key=` parameter still works — but note that **any
JWT-shaped credential sent that way is now verified as a token**, including its
signature and expiry. An expired token there is a 401, which is the fix working.

### Authenticating with an API key

Keys live in `api_keys` as SHA-256 hashes, and the **tenant comes from the row** —
never from anything the client sends. That is what makes the privacy-locked-tenant
policy bind to API-key callers at all.

```bash
# Provision one (the raw key is shown once and never stored):
python - <<'EOF'
from libs.auth import generate_api_key
raw, digest = generate_api_key()
print("give this to the client:", raw)
print("INSERT INTO api_keys (key_hash, tenant_id, label) VALUES "
      f"('{digest}', 'acme', 'dashboard');")
EOF
```

An **unknown** key is accepted only in dev (`ALLOW_UNKNOWN_API_KEYS`, which
defaults on for `APP_ENV=dev` and off everywhere else) — so a deployment that
forgot to provision keys fails **closed** rather than open.

### Search (keyword & semantic)

```bash
curl -s -H "$KEY" "$API/v1/search?q=politics&semantic=false&limit=10" | python -m json.tool
curl -s -H "$KEY" "$API/v1/search?q=fuel%20price%20anger&semantic=true&limit=10"
# Each result carries `embedding_is_stub`. When true, the stored vector (or the
# query's) is a deterministic hash — kNN returns ARBITRARY neighbours with scores
# that look exactly as plausible as real ones. Do not render that as a semantic
# match. Set EMBEDDING_ALLOW_STUB=false to refuse the write outright, or load a
# real embedding model. (PROJECT_ASSESSMENT §5.9)
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
# → {"posts_analyzed":43,"llm_calls":7,"llm_routing_rate":0.163,
#    "total_tokens":48211,"estimated_cost_usd":0.0,
#    "llm_api_calls":529,"cache_hits":33,"cache_hit_rate":0.06,
#    "tokens_by_backend_model":{"local:qwen2.5:7b":41022,"local:gemma3:4b":7189},
#    "cost_by_backend_model":{"local:qwen2.5:7b":0.0,"local:gemma3:4b":0.0},
#    "lane_split":{"post":{"calls":22,"tokens":9100,"token_share":0.19,"call_share":0.04},
#                  "comment":{"calls":507,"tokens":39111,"token_share":0.81,"call_share":0.96}},
#    "scope_note":"All figures are system-wide."}
```

Cost is priced **per backend and model**: `local` is 0.0/token by definition (the
cost is GPU time, not tokens), Groq is per-model. One blended rate used to be
applied to every token, which was wrong for both backends in opposite directions.

`lane_split` is the number that bounds cost: **comment-level calls dominate**, so
the routing rate alone is not the cost story (PROJECT_ASSESSMENT §6.8).

`?campaign_id=` filters the **post counts only** — token, cost, cache and lane
figures come from process-wide counters. `scope_note` says which is which.

### LLM backend (runtime switch)

```bash
curl -s -H "$KEY" $API/v1/config/llm                                   # current backend + models
curl -s -X PUT $API/v1/config/llm -H "$KEY" -H "Content-Type: application/json" \
  -d '{"backend":"groq"}'                                              # switch (needs GROQ_API_KEY)
curl -s -X PUT $API/v1/config/llm -H "$KEY" -H "Content-Type: application/json" \
  -d '{"backend":null}'                                                # back to default (local)
```

### Chatbot (ask it anything — honours the LLM toggle above)

Free-form chat backed by the same LLM the pipeline uses. Backend is resolved as
`request.backend` → the runtime toggle (`/v1/config/llm`) → env default, so it
follows whatever `--groq`/`--ollama` (or the dashboard switch) selected. Pass
`backend:"local"|"groq"` to force one for a single call; a privacy-locked tenant
can't force `groq`. Pass `model:"<id>"` to pick a specific model (omit for the
backend's configured default).

```bash
# Single-turn (uses the current toggle)
curl -s -X POST $API/v1/chat -H "$KEY" -H "Content-Type: application/json" \
  -d '{"message":"Summarize the main themes in the fuel-price posts."}'
# → {"reply":"…","backend":"local","model":"qwen2.5:7b","usage":{"total_tokens":…}}

# Force a specific backend + model for this call
curl -s -X POST $API/v1/chat -H "$KEY" -H "Content-Type: application/json" \
  -d '{"message":"hi","backend":"local","model":"gemma3:4b"}'

# Multi-turn history + force Groq for this call
curl -s -X POST $API/v1/chat -H "$KEY" -H "Content-Type: application/json" -d '{
  "backend":"groq",
  "messages":[{"role":"user","content":"hi"},{"role":"assistant","content":"Hello!"},
              {"role":"user","content":"what can you do?"}]
}'

# Token streaming (Server-Sent Events)
curl -sN -X POST $API/v1/chat/stream -H "$KEY" -H "Content-Type: application/json" \
  -d '{"message":"write a haiku about monitoring"}'
# event: meta   → {"backend":"groq","model":"llama-3.3-70b-versatile"}
# event: delta  → {"content":"..."}   (many)
# event: done   → {"usage":{…}}

# List the models available per backend (populates the dashboard's model picker)
curl -s -H "$KEY" $API/v1/chat/models
# → {"active_backend":"local",
#    "backends":{"local":{"models":["gemma3:4b","qwen2.5:7b",…],"default":"qwen2.5:7b"},
#                "groq":{"models":[…],"default":"llama-3.3-70b-versatile"}}}
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

**Two things make this actually enforceable**, and neither was true before:

- `tenant_id` comes from the `api_keys` row (or from allowlisted, verified JWT
  claims) — **not** merged from a token body, which used to let a self-signed
  token name any tenant it liked.
- The check **fails closed**. It used to `return` on any database error, i.e.
  permit egress to Groq precisely when it could not verify the policy. An
  unreadable policy table is now a **503**, not a silent allow:

```bash
# With the policy table unreachable:
# → 503 {"detail":"Cannot verify the tenant's LLM-backend policy right now, so the
#         requested 'groq' backend is refused. Retry, or omit llm_backend …"}
```

A privacy guarantee that evaporates when the database hiccups is not a guarantee.

---

## 4. Interactive docs

FastAPI serves the full OpenAPI spec with a try-it-out UI:

- Swagger UI → <http://127.0.0.1:8001/docs>
- ReDoc → <http://127.0.0.1:8001/redoc>
- Raw spec → <http://127.0.0.1:8001/openapi.json>

The dashboard (<http://127.0.0.1:8080>) exercises all of the above visually —
Overview (usage/corpus stats), Posts, Jobs (live SSE progress), Reports,
Search, and Agents tabs.

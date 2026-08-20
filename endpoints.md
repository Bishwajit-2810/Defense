# endpoints.md — the output JSON & how to test every endpoint

This is the hands-on guide for **getting the custom analysis JSON out of the
system** (post summary, sentiment, topics, toxicity, comment analysis — the
full canonical result) and for **testing every API endpoint with curl**.

The canonical schema lives in
[src/defense/contracts/schemas/output_schema.json](src/defense/contracts/schemas/output_schema.json); the field
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
  "language_method":        "fasttext",             // which detector produced `language` (fasttext | script_heuristic)
  "post_text":              "তেলের দাম আবার বাড়ল…",   // the post's own caption, as ingested; null for image-only posts
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
  // Since schema 1.2 each component carries its OWN {label, score} — the
  // caption's score is not the fused overall one. null, not a bare label:
  // text_sentiment is null for a null-caption post, image_sentiment for a
  // text-only one or whenever no vision model produced a verdict.
  "text_sentiment":     { "label": "negative", "score": -0.71 },
  "image_sentiment":    null,
  "baseline_sentiment": -0.5,                       // upstream platform's own score (for comparison)

  // ---- classification ---------------------------------------------------
  // `emotion` is {primary, scores}, not a bare score map: `primary` is the
  // label consumers read, and it is never null (defaults to "neutral").
  "emotion":   { "primary": "anger", "scores": { "anger": 0.55, "sadness": 0.2, "joy": 0.05 } },
  "intents":   ["criticism", "mobilization"],
  "topics":    ["fuel prices", "government policy"],
  "insight":   "Anger is aimed at the pricing decision, not at the fuel shortage itself.",  // Stage-2 only; null when Stage 2 was skipped
  "entities":  [ { "text": "BPC", "label": "ORG", "confidence": 0.88 } ],
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
    ],
    // Which comments the ROUTER put in front of the models: top-N by reaction
    // count, text-bearing only. Absent on results written before 17 Aug 2026.
    "stage2_selection": {
      "strategy": "top_reactions", "limit": 100,
      "total": 2857,                                // every comment on the post
      "eligible": 2612,                             // …that had text to read
      "selected": 100, "skipped": 2512,
      "cutoff_likes": 7, "top_likes": 946           // the reaction count that made the cut
    },
    // How the eight labellers voted, once per thread. `comments` here is the
    // whole thread; `analysed` is the router's set. Reading one as the other is
    // how a deliberate cap looks like a half-failed stance pass.
    "ensemble": {
      "comments": 2857, "analysed": 100, "not_analysed": 2512,
      "voters": ["xlmr", "distilbert", "twitter_xlmr", "mbert", "llm"],
      "llm_labelled": 96, "llm_share": 0.0336, "llm_share_analysed": 0.96,
      "unanimous": 71, "unanimous_share": 0.71,     // ≥2 voters agreeing
      "single_voter": 18, "unread": 0,              // 1 labeller, and none at all
      "abstained": 11, "mean_agreement": 0.83,
      "deduplicated": 4, "duplicate_share": 0.04,   // exact-text twins reusing a verdict
      "escalation_reasons": { "llm_all": 96, "near_duplicate": 4, "no_text": 245, "below_top_n": 2512 },
      "mode": "all", "capped_out": 0,
      "selection": { "…": "same object as comment_analysis.stage2_selection" }
    }
  },

  // ---- trust & audit ------------------------------------------------------
  "confidence": { "overall": 0.81, "sentiment": 0.86, "language": 0.95, "topics": 0.7 },
  "processing": {
    "stage1_ms":   142,
    "stage2_ms":   2210,
    "llm_used":    true,                            // false = Stage-1-only (cheap path)
    "llm_backend": "local",                         // local | groq
    "llm_model":   "qwen3-vl:4b",
    // {role: resolved model id} — summarization and classification no longer
    // share a model, so one `llm_model` cannot attribute the summary (§6.5).
    "role_models": { "summary": "qwen2.5:7b", "stage2": "qwen2.5:7b" },
    // Stage-1 provenance, forwarded verbatim: what actually produced this row.
    "nlp_engine":  "llm",                           // stub | models | llm
    "stub_mode":   false,                           // MODEL_STUB_MODE — true means the embedding is a hash, not a vector
    "degraded_components": [],                      // real-mode components that fell back (§9.10)
    "schema_version": "1.3"
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
| `comment_analysis.stage2_selection` | Which comments Stage 2 analysed. **`limit: 0` (the default) means every comment with text was analysed** — `selected` then equals `eligible`. A positive `limit` (`ROUTER_COMMENT_TOP_N`) keeps only that many, ranked by reaction count. `total` / `eligible` / `selected` are three different numbers, and reading any one as another is how "every comment was analysed" gets claimed for a capped run. |
| `comment_analysis.comments[].stage2_selected` | `false` on a comment outside the cut: **no Stage-2 model read it**, its label is Stage 1's, and `escalation_reason` is `below_top_n`. Absent/`true` means it was analysed. Textless comments carry neither — they are never ranked (`escalation_reason: no_text`). |
| `comment_analysis.comments[].parallel_labels` | `{voter: {sentiment, score, …}}` for each of the eight labellers that spoke — the seven `STAGE2_CLASSIFIER_*` heads and `llm`. A voter that failed to load or answered off-taxonomy is **absent**, never a neutral entry. **There is no `heuristic` key:** Stage 1's emoji/keyword label stopped voting on 17 Aug 2026, so `parallel_labels` contains model verdicts only. |
| `comment_analysis.comments[].label_voters` / `label_sources` | How many labellers actually voted, and which. Quote them with `label_agreement`: `1.0` over one voter is a single opinion, not a consensus. `label_voters: 0` ⇒ `sentiment: "uncertain"`, and *nothing* about that comment is claimed. |
| `comment_analysis.ensemble.llm_share` vs `llm_share_analysed` | Against the **whole thread** vs against the **router's selection**. 100 of 2,857 is 3.4% of the post and 100% of what was selected; both are needed or one gets read as the other. |
| `language_method` | `fasttext` \| `script_heuristic` \| `stub`. fastText is optional; without it the pipeline degrades to script detection rather than failing the post. |
| `processing.llm_used` | Only routed posts ([router rules](src/defense/services/workers/router/rules.py)) carry Stage-2 latency/cost. Note this is **not** the whole cost lever any more: comment labelling runs for every post and is 85–96% of LLM calls (PROJECT_ASSESSMENT §6.8). |

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

### 2b-bis. Stop a job, or delete its record

```bash
# Stop — no further post of this job is analysed
curl -s -X POST -H "$KEY" $API/v1/analysis/<job_id>/cancel
# → 200 {"analysis_id":"…","status":"cancelled","previous_status":"running",
#        "progress":{"total":50,"completed":23,"failed":0}}
# → 409 if the job already finished (nothing to stop)

# Delete the job record — stops it first if it is still running
curl -s -X DELETE -H "$KEY" $API/v1/analysis/<job_id>
# → 200 {"analysis_id":"…","deleted":{"jobs":1,"redis_keys":5},
#        "stopped":true,"previous_status":"running"}
```

Stopping is **cooperative**, and the reason is structural: a job is N envelopes
spread across the stage streams, so there is no process to kill and no way to
pull a message back out of a stream. `POST .../cancel` raises one flag
(`job:{id}:cancelled`, 24h TTL — `libs/jobs.py`) that **ingestion, Stage 1, the
router and Stage 2** each check as they pick a message up, and drop the message
instead of doing the work. So:

* **The stop costs at most one post per stage.** A post already inside a stage
  finishes and is persisted — its LLM spend is already paid — which is why the
  progress counter can tick up once or twice after a stop. Everything behind it
  is skipped.
* **A stopped job stays stopped.** The row goes to `cancelled`, which is terminal:
  the assembler's status update excludes it, and the counter reconciliation in
  `GET /v1/analysis/{id}` excludes it, so the last in-flight post cannot write
  the job back to `running` or `done`.
* **Watchers are told immediately.** A terminating `event: cancelled` frame goes
  out on `analysis:progress:{id}`, so an open SSE stream closes then rather than
  at its 5-minute timeout.
* **Re-running is the way to resume.** There is no partial resume; `POST
  /v1/analysis/run` with the same selector re-analyses the whole set.

`DELETE` removes the job row, its progress counters and its Trace-tab replay
buffer. It deliberately does **not** touch `analysis_results`: those rows are
keyed by post and campaign, not by job, and they are what every other tab reads —
several jobs (plus the original ingest) write the same rows, so deleting them
here would blank posts another job analysed. Use `DELETE /v1/posts/{post_id}` for
that. The cancel flag is the one key a delete leaves behind, since it is all that
still stops the deleted job's in-flight posts.

Both are tenant-scoped: another tenant's job id is a 404, not a stop.

### 2b-ter. Resume an interrupted job

The case this exists for: 300 posts queued, 30 analysed, the machine loses power.

```bash
curl -s -X POST -H "$KEY" $API/v1/analysis/<job_id>/resume
# → 200 {"analysis_id":"…","resumed":true,"status":"running","previous_status":"running",
#        "progress":{"total":300,"completed":30,"remaining":270}}
# → 409 if the job is still making progress, or already completed
```

Only the 270 go back on the stream. The 30 are not paid for twice, and the
progress bar picks up at 30/300 rather than restarting.

**How "what is left" is decided.** Nothing marks a power-cut job as dead: its row
still reads `running`, and its Redis counters went with the power. So the answer
is derived from **Postgres alone** — the job's selector gives the full post set,
and a post counts as done when its `analysis_results` row was written at or after
the job's `created_at`. That table is `UNIQUE (post_id)` with no job column, so
the timestamp is the only discriminator available; it also means a post some
*other* job re-analysed in the meantime counts as done, which is the right answer
— a fresh result exists either way.

**What resume rebuilds.** `job:{id}:total` and `job:{id}:completed` are re-seeded
(to 300 and 30), and `job:{id}:failed` is reset because those posts are being
retried. The seeded `completed` is what makes the assembler finish the job when
the *last* remaining post lands: with no `total` at all it falls back to
"first landing wins", and with `completed` at 0 the job could never reach its own
total. Any stop flag is cleared first — the workers drop anything carrying it, so
clearing after enqueueing would make the whole resume a no-op.

**When it is refused.** A job that has written progress within the last 5 minutes
(`_STALE_JOB_SECONDS`) is busy, not interrupted, and re-enqueueing under it would
duplicate work and corrupt its counters — so it 409s and tells you to stop it
first. `jobs.updated_at` is the heartbeat: the assembler touches it as every post
lands. The dashboard shows the same threshold as a **stalled** badge, so a
power-cut job stops reading as one that is still working.

**What it does not do.** There is no per-post partial resume: a post that was
mid-flight is redone from Stage 1. The original request's `options` are reused
(so a job run with summaries resumes with them — this is why `jobs.options` is
now persisted rather than written as `{}`), but `llm_backend` is re-resolved
against the tenant's policy, so a stored `groq` does not outlive a privacy lock.

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

### 2c-bis. Page through the comments of one post

The per-comment table is bulky, so the list/detail responses strip it
(`_comment_analysis_summary`) and this endpoint serves it paginated. It backs the
dashboard's per-comment comparison, and it is the only place the eight labellers'
individual verdicts are readable:

```bash
curl -s -H "$KEY" "$API/v1/analysis/post/<post_id>/comments?limit=200&offset=0" \
  | python -m json.tool

# Only the comments the labellers DISAGREED on — the review queue, and the set a
# gold standard should be built from:
curl -s -H "$KEY" "$API/v1/analysis/post/<post_id>/comments?sentiment=disagreed"

# Also: sentiment=positive|negative|neutral|uncertain|all, emotion=<label>|all
```

Returns the page in `comments[]` plus aggregates computed over the **full** set,
not the page: `sentiment_breakdown`, `emotion_breakdown`, `method_breakdown`,
`provenance`, `coverage` / `coverage_label`, `top_authors`, `top_liked`,
`avg_sentiment_score`, `target_stances`, and the two objects that say how much of
the thread was actually analysed — `ensemble` and `stage2_selection` (§1).

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

# What may the login screen offer? Unauthenticated, because a login screen has
# no credential yet. The dashboard calls this before rendering its Sign in /
# Create account tabs.
curl -s $API/v1/auth/config
# → {"signup_enabled":true,"bootstrap":true,"min_password_length":8,
#    "allow_any_login":true,"users_table_ready":true}

# Register. Username + password ONLY: tenant_id and role are assigned
# server-side (SIGNUP_TENANT_ID), because a client that names its own tenant
# names whose data it can read. Returns a token, so no second round-trip.
# `bootstrap: true` above means the users table is empty and THIS account
# becomes the administrator.
curl -s -X POST $API/v1/auth/signup -H "Content-Type: application/json" \
  -d '{"username":"analyst","password":"a-good-long-password"}'
# → 201 {"access_token":"…","token_type":"bearer","username":"analyst",
#        "tenant_id":"default","role":"admin"}
# → 409 if the username is taken · 422 if it fails validation
# → 403 if signup is disabled · 503 if there is no users table

# Log in. Credentials are verified against the `users` table when rows exist;
# while it is EMPTY, dev accepts anything and logs a loud warning
# (ALLOW_ANY_LOGIN). The first real account closes that hatch.
# The username match is case-insensitive, because signup stores it case-folded.
TOKEN=$(curl -s -X POST $API/v1/auth/token -H "Content-Type: application/json" \
  -d '{"username":"analyst","password":"a-good-long-password"}' | python -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')

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

### Search (identifier, keyword & semantic)

**An identifier is looked up, not searched for.** Paste a `post_id`,
`platform_post_id`, post URL or `campaign_id` and it is matched exactly, ahead of
whichever mode you asked for, and the response says so with
`match_type: "exact_id"`:

```bash
curl -s -H "$KEY" "$API/v1/search?q=cmp58e24s04pgwglq7g9u9jz0" | python -m json.tool
# → {"query":"cmp58e24…","semantic":false,"total":1,"match_type":"exact_id",
#    "id_lookup_missed":false,"results":[{…that post…}]}

# A prefix works too — the dashboard's post table renders only the first 8
# characters of an id, so a prefix is usually what is in your clipboard.
curl -s -H "$KEY" "$API/v1/search?q=cmp58e24"          # → match_type "id_prefix"

# An id that matches nothing says so, instead of answering with neighbours:
curl -s -H "$KEY" "$API/v1/search?q=cmzzzz9999zzzz9999zzzz999&semantic=true"
# → {"total":0,"match_type":"keyword","id_lookup_missed":true}
```

This exists because both arms got an id query wrong, in opposite directions.
Keyword search read only `post_summary` / `post_text` / `keywords` / `topics` /
comment `themes`, so a **real post id returned 0 results** — the id being the one
string most likely to be pasted in. Semantic search embedded the id and returned
**20 cosine neighbours**, none of them the post asked for, which is the worse
failure of the two: an empty result reads as "not found", twenty ranked results
read as a successful search. Hence:

* an exact identifier match short-circuits **every** mode, and its `score` is 1.0
  because an exact match is not a similarity;
* an id-shaped query that matches no identifier is **downgraded to the keyword
  arm** rather than embedded, and carries `id_lookup_missed: true` so a client
  can lead with "no post has that id";
* the shape test requires a digit, so `bangladesh` and `মুসলিমদের` are words, not
  ids — and a false positive can only cost an extra query, never results, because
  the keyword arm always still runs;
* the keyword arm now also covers the identifier columns, for the partial case.

`match_type` is `exact_id` | `id_prefix` | `keyword` | `semantic` | `hybrid`.

Free-text search is unchanged:

```bash
curl -s -H "$KEY" "$API/v1/search?q=politics&semantic=false&limit=10" | python -m json.tool
curl -s -H "$KEY" "$API/v1/search?q=fuel%20price%20anger&semantic=true&limit=10"
# Each result carries `embedding_is_stub`. When true, the stored vector (or the
# query's) is a deterministic hash — kNN returns ARBITRARY neighbours with scores
# that look exactly as plausible as real ones. Do not render that as a semantic
# match. Set EMBEDDING_ALLOW_STUB=false to refuse the write outright, or load a
# real embedding model. (PROJECT_ASSESSMENT §5.9)
#
# The flag is REPORTED BY STAGE 1 and carried to the column, not derived
# downstream — the stub is the same 768 dims as a real vector, and deriving it
# from the dimension is why every row was recorded as `false` until 5 Aug 2026
# (PROJECT_ASSESSMENT §13.2).
# → {"query":"…","semantic":true,"total":N,"match_type":"…","id_lookup_missed":false,
#    "results":[{"post_id","score","snippet","result":{<full §1 JSON>}}]}
#
# `snippet` is the summary truncated to 200 chars for the list view; `result` is
# the COMPLETE canonical object, so a client never needs a second request to show
# a hit in full. The dashboard's Search tab opens it in the same detail modal the
# Posts tab uses (it rendered only the snippet until 21 Aug 2026, which left the
# post you had just found by id unreadable).
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

# Download an existing report as a file
curl -s -H "$KEY" "$API/v1/reports/<report_id>/export?format=pdf" -o report.pdf

# Generate over recent posts AND download in one call — no report_id needed
curl -s -H "$KEY" \
  "$API/v1/reports/export_latest?campaign_id=all&type=mass_reaction&format=pdf" \
  -o latest.pdf     # format=html for the HTML version
```

**`clusters` is the SQL topic aggregate; `embedding_clusters` is the LLM one.**
A grounded report also runs the embedding-cluster path (k-means over the corpus
vectors, one LLM-B call per cluster — the cost lever architecture.md §5
describes), returned as `embedding_clusters` alongside
`embedding_clusters_are_stub`. When that flag is true the vectors were hash
stubs, so the groupings are arbitrary and the summaries describe nothing —
render the disclosure, not just the text. Until 5 Aug 2026 these summaries were
computed, paid for, and stripped by the response model (PROJECT_ASSESSMENT
§13.1).

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

**Coverage.** The counters are written by `LLMClient` itself, so every caller is
counted: both pipeline stages, `POST /v1/chat`, report narratives and cluster
summaries, and agent runs. `lane_split` has five lanes (`post`, `comment`,
`stage1`, `interactive`, `agent`) and `pipeline_tokens` sums the first three, so
the per-post cost model is not inflated by a chatbot session. Until 5 Aug 2026
only the Stage-2 worker incremented anything, leaving five callers uncounted
under a `scope_note` claiming system-wide coverage (PROJECT_ASSESSMENT §13.4).

### LLM backend (runtime switch)

```bash
curl -s -H "$KEY" $API/v1/config/llm                                   # current backend + models
curl -s -X PUT $API/v1/config/llm -H "$KEY" -H "Content-Type: application/json" \
  -d '{"backend":"groq"}'                                              # switch (needs GROQ_API_KEY)
curl -s -X PUT $API/v1/config/llm -H "$KEY" -H "Content-Type: application/json" \
  -d '{"backend":null}'                                                # back to default (local)
```

**This key is global, and selecting `groq` needs an admin role.** It applies to
every tenant's pipeline work, so it is an operator action. A privacy-locked
tenant is unaffected by whatever it says: the API resolves each job's backend
(request > toggle > env), applies the lock where the tenant is known, and stamps
the decision into the job envelope, which both workers honour. An explicit
`llm_backend:"groq"` on a request is refused with 403; the global toggle is
silently downgraded to `local` for a locked tenant, because an operator's switch
is not their choice.

Until 5 Aug 2026 this endpoint had no policy check and no role check, and the
pipeline read the key directly — so a locked tenant's content followed the toggle
to Groq while `POST /v1/analysis` still returned a 403 that read as the lock
holding (PROJECT_ASSESSMENT §13.5).

`GET /v1/config/llm` reports every role in `VALID_ROLES` — including `summary`,
the role §6.5 added so summaries get a stronger model. It is derived from
`LLMClient.default_model` rather than mirrored, so a new role appears
automatically (§13.7a).

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

### Chat history (server-side conversations)

Persisted per caller, so the dashboard's chat survives a reload. Separate from
`/v1/chat`, which is stateless — these endpoints store the turns.

```bash
curl -s -H "$KEY" "$API/v1/chat/conversations?limit=20"          # newest first
curl -s -X POST $API/v1/chat/conversations -H "$KEY" \
     -H "Content-Type: application/json" -d '{"title":"fuel price thread"}'
curl -s -H "$KEY" "$API/v1/chat/conversations/<id>"              # + its turns
curl -s -X POST $API/v1/chat/conversations/<id>/messages -H "$KEY" \
     -H "Content-Type: application/json" \
     -d '{"messages":[{"role":"user","content":"hi"}]}'          # append turns
curl -s -X PATCH $API/v1/chat/conversations/<id> -H "$KEY" \
     -H "Content-Type: application/json" -d '{"title":"renamed"}'
curl -s -X DELETE $API/v1/chat/conversations/<id>                # one
curl -s -X DELETE $API/v1/chat/conversations                     # all of them
```

An empty append is a 422, and a role outside `VALID_ROLES` is rejected — a
conversation must not be able to store a turn the LLM cannot replay.

### Raw pipeline events

```bash
curl -s -H "$KEY" "$API/v1/events?limit=200"
# → {"events":[{"stream_id":"1723…-0","event":{…}}, …]}   newest first
```

The last N frames of the global `pipeline:events` Redis stream, newest first —
the same frames the Trace tab consumes live via `/v1/pipeline/stream`. Useful when
a run has already finished and the stream is gone. An unreadable stream returns
`{"events": [], "error": …}` rather than a 500.

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

## 3b. The rest of the surface — every `/v1` path

The app serves **49 distinct `/v1` paths / 61 method+path pairs** (20 Aug 2026;
`python -c "from defense.services.api.main import app; ..."` over `app.routes` is
the check). §2–§3 cover the ones you drive by hand. These are the remainder — all
real routes, most of them what the dashboard calls:

| Method | Path | What it is |
| :--- | :--- | :--- |
| `GET` | `/v1/auth/verify` | Validate a raw token **without** establishing a session. Lets a client tell "no token" from "expired token" without a refresh. |
| `GET` | `/v1/analysis/post/{post_id}/comments` | Paginated per-comment sentiment for one post — full coverage, not a sample. Same data as §2c-bis. |
| `GET` | `/v1/analysis/export` | Download analysis results as a **ZIP of PDFs**, one per post. |
| `POST` | `/v1/analysis/{analysis_id}/cancel` | Stop a queued/running job — cooperative, see §2b-bis. `409` if it already finished. |
| `POST` | `/v1/analysis/{analysis_id}/resume` | Re-enqueue only the posts an interrupted job never finished — §2b-ter. `409` while it is still moving. |
| `DELETE` | `/v1/analysis/{analysis_id}` | Delete the job record (stops it first if running). Keeps the posts' analysis results — §2b-bis. |
| `GET` | `/v1/pipeline/stats` | Point-in-time stage state (queue depths, in-flight, last completion) — backs the dashboard **Pipeline** tab. |
| `GET` | `/v1/agents/types` | The registered agent types and their descriptions, read straight off `AGENT_REGISTRY`. Use this rather than hard-coding a list of agent names. |
| `GET`&nbsp;/&nbsp;`DELETE` | `/v1/agents/runs` | List recent agent runs / clear the history. Aliases of the `/v1/agents` collection. |
| `POST` | `/v1/chat/agent` | Route a chat message to an MCP-backed agent — the Chat tab's path into the agent layer. |
| `GET`&nbsp;/&nbsp;`PATCH`&nbsp;/&nbsp;`DELETE` | `/v1/chat/conversations/{conversation_id}` | Read a conversation and its turns / rename it / delete it. |
| `POST` | `/v1/chat/conversations/{conversation_id}/messages` | Append turns to a conversation. |
| `GET` | `/v1/reports/{report_id}` | Fetch a generated report by id. |
| `GET` | `/v1/reports/{report_id}/export` | Export that report as a downloadable PDF or HTML file. |
| `GET` | `/v1/reports/export_latest/export` | Generate a **fresh** grounded mass-reaction report and stream it straight back — no id round-trip. |
| `GET` | `/v1/logs/services` | Which services are present in the log buffer (populates the Logs tab's filter). |
| `GET` | `/v1/logs/stream` | Tail server-side logs over SSE. Needs an SSE ticket, like every other stream — see §3. |

Two of these are easy to get wrong from the outside:

```bash
# Agent types — the roster is nine, and it comes from the registry, not a constant.
curl -s http://127.0.0.1:8001/v1/agents/types -H "X-API-Key: demo" | python -m json.tool
# → [{"name":"analyst","description":…,"tools":[…],"llm_role":"agent","max_tool_calls":10}, …]

# A report you want but have not generated: export_latest builds and streams in one call.
curl -s -o report.pdf http://127.0.0.1:8001/v1/reports/export_latest/export -H "X-API-Key: demo"
```

## 4. Interactive docs

FastAPI serves the full OpenAPI spec with a try-it-out UI:

- Swagger UI → <http://127.0.0.1:8001/docs>
- ReDoc → <http://127.0.0.1:8001/redoc>
- Raw spec → <http://127.0.0.1:8001/openapi.json>

The dashboard (<http://127.0.0.1:8080>) exercises all of the above visually.
**Eleven tabs** (`dashboard/src/components/NavTabs.jsx` is the list of record):
Overview (usage/corpus stats), Posts, Jobs (live SSE progress, plus stop /
resume / re-run / delete per job), Reports, Search, Agents, Chat, Pipeline,
Trace, Warnings, Logs.

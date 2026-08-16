# Environment Reference

Companion to [`.env.example`](.env.example) — what every key does, which code reads it,
and what changes if you touch it. Sections match the numbered blocks in the file.

Settings are loaded by `Settings` in
[`src/defense/libs/common/config.py`](src/defense/libs/common/config.py) (pydantic-settings,
case-insensitive, `extra="ignore"`). **`extra="ignore"` means a misspelled key is silently
dropped, not rejected** — if a setting appears to have no effect, check the spelling first.

---

## 1. Core Infrastructure

| Key | What it does |
|---|---|
| `DATABASE_URL` | Postgres. Holds posts, analysis results, users — **and** the pgvector embeddings in `analysis_results.embedding`. There is no separate vector-store URL; RAG search shares this connection. Converted to `postgresql+asyncpg://` at runtime by `get_async_database_url()`. |
| `REDIS_URL` | Three jobs at once: the inter-stage work queues (Redis Streams — `ingestion:queue` → `nlp:stage1:queue` → `router:queue` → `llm:stage2:queue` → `assembler:queue`), the LLM response cache, and the live log/trace buffer when `LOG_TO_REDIS` is on. |
| `CLICKHOUSE_URL` | Analytics store the dashboard charts read from. |
| `MINIO_ENDPOINT` | S3-compatible object store for post images. **Must include the scheme.** boto3's `endpoint_url` needs a full URL ([`assembler/__main__.py:96`](src/defense/services/workers/assembler/__main__.py#L96)), and the image-URL builder logs a warning and patches in `http://` if you omit it ([`vision_analyzer.py:84`](src/defense/services/workers/stage1_nlp/vision_analyzer.py#L84)). |
| `MINIO_ACCESS_KEY` / `MINIO_SECRET_KEY` / `MINIO_BUCKET` | Credentials and bucket. The bucket is created on assembler start-up if missing. |

**Hostnames:** `.env.example` ships the Docker service names (`postgres`, `redis`,
`clickhouse`, `minio`) to match [`deploy/docker-compose.yml`](deploy/docker-compose.yml).
Swap them for `localhost` when you run the services directly on the host.

---

## 2. LLM Backend

| Key | What it does |
|---|---|
| `LLM_BACKEND` | `local` (Ollama / vLLM) or `groq`. This is the *default*; any call may override it per-request. |
| `LOCAL_LLM_BASE_URL` | The OpenAI-compatible endpoint. `ollama serve` exposes `http://localhost:11434` — the `/v1` suffix is required. |
| `LOCAL_LLM_API_KEY` | Ollama ignores it, but the OpenAI SDK requires a non-empty value. `ollama` is the conventional filler. |
| `GROQ_API_KEY` | Used when `LLM_BACKEND=groq` **and** on automatic failover. |

**Failover is automatic and one-directional.** If a Groq call fails, the client retries the
same request on the local backend ([`client.py:370`](src/defense/libs/llm/client.py#L370)).
Each backend also sits behind a circuit breaker (`LLM_CB_FAILURE_THRESHOLD`,
`LLM_CB_COOLDOWN_SECONDS`) so a flapping backend is taken out of rotation instead of being
hammered. This is why the `_GROQ_` model keys matter even when you run fully local.

---

## 3. Models

There are **seven roles**. A role is a *job*, not a stage — the mapping lives at
[`client.py:59-77`](src/defense/libs/llm/client.py#L59-L77). Every role has a `_LOCAL_` and a
`_GROQ_` variant naming the same job on each backend.

| Role | Env keys | Who calls it | What it does |
|---|---|---|---|
| `stage1` | `STAGE1_LOCAL_MODEL`<br>`STAGE1_GROQ_MODEL` | [`text_analyzer.py:854`](src/defense/services/workers/stage1_nlp/text_analyzer.py#L854), [`worker.py:470`](src/defense/services/workers/stage1_nlp/worker.py#L470), [`comment_analyzer.py:547`](src/defense/services/workers/stage1_nlp/comment_analyzer.py#L547) | The high-volume classifier. One JSON call returns 12 fields per post: `language, sentiment, sentiment_score, emotion, topics, intents, post_type, post_type_confidence, toxicity_score, hate_speech_score, entities, keywords`. Also per-comment labelling — but only when `STAGE1_LLM_COMMENTS=true`. |
| `stage2` | `STAGE2_LOCAL_MODEL`<br>`STAGE2_GROQ_MODEL` | [`worker.py:1466`](src/defense/services/workers/stage2_llm/worker.py#L1466), [`:1490`](src/defense/services/workers/stage2_llm/worker.py#L1490), [`:942`](src/defense/services/workers/stage2_llm/worker.py#L942) | Post-type, insight, and context-aware comment stance. Every one picks from a fixed vocabulary, so it wants a constrained model, not a creative one. |
| `summary` | `SUMMARY_LOCAL_MODEL`<br>`SUMMARY_GROQ_MODEL` | [`stage1_nlp/worker.py:451`](src/defense/services/workers/stage1_nlp/worker.py#L451), [`stage2_llm/worker.py:1276`](src/defense/services/workers/stage2_llm/worker.py#L1276) | All prose. **Note this is the model Stage 1 uses for its post summary** — the summary does *not* run on the `stage1` model. Also covers the Stage-2 comment-thread summary, and the Stage-2 post summary on the fallback path. |
| `agent` | `AGENT_LOCAL_MODEL`<br>`AGENT_GROQ_MODEL` | [`runner.py:500`](src/defense/services/agents/runner.py#L500) | All 9 MCP agents in [`registry.py`](src/defense/services/agents/registry.py). The only role doing multi-turn tool use, so it needs solid native function calling — hence `llama3.1:8b` rather than qwen. |
| `llm_b` | `LLM_B_LOCAL_MODEL`<br>`LLM_B_GROQ_MODEL` | [`reports.py:323`](src/defense/services/api/routers/reports.py#L323), [`:501`](src/defense/services/api/routers/reports.py#L501) | The report-generation layer. |
| `llm_a` | `LLM_A_LOCAL_MODEL`<br>`LLM_A_GROQ_MODEL` | *nothing* | **Dead.** No `role="llm_a"` call site exists in `src/`. Kept only for backward compatibility; safe to delete. |
| `vlm` | `VLM_LOCAL_MODEL`<br>`VLM_GROQ_MODEL` | [`worker.py:1396`](src/defense/services/workers/stage2_llm/worker.py#L1396) | Image posts. It **substitutes for** the `summary` role rather than adding a call: `role = "vlm" if (has_photos and vlm_enabled) else summary`. If the image bytes don't resolve, the result is tagged `post_summary_source: "llm"` instead of `"vlm"`. |

### Why the models are split this way

- **`stage1` is the smallest** (`gemma3:4b`) because it fires the most times per post over
  short classification prompts.
- **`stage2` is bigger** (`qwen2.5:7b`) because its verdicts — stance, insight, post-type —
  are what the dashboard reports as findings.
- **`summary` is separate from `stage2`** because classification and prose are different
  jobs: one picks from a fixed vocabulary, the other writes fluent Bangla. Splitting them
  is also the honest version of the cost story — the expensive model is spent once per
  post, not on every classification call. It currently points at the same `qwen2.5:7b`,
  so the split costs nothing until a bake-off picks a better writer:
  `python -m eval.bakeoff_summary --models qwen2.5:7b gemma4:26b gemma4:31b`
- Local model names **must match `ollama list` exactly**, tag included.

### Known sharp edge

The two live `stage1` calls both use `response_format={"type":"json_object"}`. A 4B model
under grammar-constrained decoding is the likeliest to return a well-formed but empty `{}`,
which parses fine and says nothing. [`_is_degenerate_json`](src/defense/libs/llm/client.py#L107)
catches this and retries unconstrained; if that also fails, Stage 1 falls back to the
deterministic stub for all 12 fields. Watch for `llm_json_mode_degenerate_retrying_unconstrained`
with `role=stage1` in the logs.

Also note `post_type` is computed **twice** in Stage 1 — once inside the 12-field blob and
again as a separate call at [`worker.py:470`](src/defense/services/workers/stage1_nlp/worker.py#L470).
Harmless, but it is a duplicate round-trip per post if you are counting cost.

---

## 4. RAG & Vector Search

| Key | What it does |
|---|---|
| `EMBEDDING_DIM` | Vector width, read by [`embeddings.py:33`](src/defense/libs/embeddings.py#L33). **Must match the pgvector column width in the database** — changing it without a migration breaks inserts. |
| `EMBEDDING_MODEL` | SentenceTransformer checkpoint. `paraphrase-multilingual-mpnet-base-v2` is 768-dim and covers Bangla, English, and romanized Banglish. This never goes through the LLM client. |
| `MODEL_STUB_MODE` | `true` = no heavy ML weights are downloaded or loaded. Embeddings come from a deterministic stub, and the two Stage-2 HF classifiers are skipped. It also drives the Hugging Face offline policy: `apply_hf_offline_policy()` sets `HF_HUB_OFFLINE`/`TRANSFORMERS_OFFLINE` so nothing reaches the network. Override that link with `HF_OFFLINE`. |

`MODEL_STUB_MODE=true` is the right default for development, but be clear about what it
costs: with the stub active, embedding-based search results are not meaningful, and the
comment ensemble runs with one voter instead of three.

---

## 5. Pipeline Execution & Coverage

| Key | What it does |
|---|---|
| `STAGE1_LLM` | `true` = Stage-1 NLP comes from the `stage1` LLM. `false` = deterministic stub. Any LLM or JSON failure falls back to the stub automatically, so this is a preference, not a hard switch. |
| `STAGE1_LLM_COMMENTS` | Whether Stage 1 *also* LLM-labels every comment. **Off by default and that is deliberate** — Stage 2 already labels comments with a bigger model plus two classifiers, so this re-does the work with the weaker one and its vote is usually outweighed. It is also the slowest step in the pipeline. Turn it on only when running *without* the Stage-2 ensemble. |
| `STAGE1_LLM_COMMENT_MAX` | `0` = every non-emoji comment reaches the LLM. A positive value caps it for a fast demo run. The old default of 60 meant ~29% of comments got an LLM label while the output described itself as full coverage. |
| `COMMENT_STANCE_MAX_PER_POST` | Same idea for the Stage-2 stance pass. `0` = no cap. |
| `STAGE1_LLM_BATCH` / `COMMENT_STANCE_BATCH` | Comments per LLM request (25). |
| `STAGE1_LLM_CONCURRENCY` / `COMMENT_STANCE_CONCURRENCY` | Batches in flight at once (3). Batches run concurrently with per-batch retry, which is why uncapped coverage doesn't stall a post — a 2,857-comment thread is ~115 batches. |
| `SUMMARY_MAX_TOKENS` | Ceiling for the post summary (1024). |
| `COMMENT_SUMMARY_MAX_TOKENS` | Ceiling for the comment-thread summary (640). |
| `INSIGHT_MAX_TOKENS` | Ceiling for the Stage-2 insight (768). |
| `LLM_MAX_CONTINUATIONS` | A reply cut off at its ceiling is auto-continued this many times, then flagged and **never cached**. This exists because Bangla costs far more tokens per character than English, so one fixed ceiling truncates Bangla while sparing English. The continuation prompt is deliberately language-neutral so the model doesn't switch language mid-summary. |
| `COMMENT_LAUGH_SENTIMENT` | How 🤣😂😆😹 are scored: `negative` \| `positive` \| `neutral`. Emoji-only comments are kept as crowd signal but never sent to an LLM. On this corpus laughing emoji read as mockery, hence `negative`. An unrecognised value falls back to `negative` ([`comment_analyzer.py:119-123`](src/defense/services/workers/stage1_nlp/comment_analyzer.py#L119-L123)). |
| `STAGE1_OCR_SENTIMENT` | Runs sentiment over OCR text for null-caption image posts. Off — no `photoUrl` in the current corpus resolves, so it would yield nothing. |

---

## 6. Authentication & Security

| Key | What it does |
|---|---|
| `APP_ENV` | Gates the placeholder-secret check. Outside `dev`/`development`/`local`/`test`/`ci`, the API **refuses to start** while `JWT_SECRET` is still a value that ships in this repo. It is also the fallback for `ALLOW_ANY_LOGIN` / `ALLOW_SIGNUP`. |
| `JWT_SECRET` | Token signing secret. Read fresh from the environment on **every** call, not from the cached settings snapshot — rotating it must invalidate issued tokens without a restart. `change-me-in-production` is on the placeholder blocklist. |
| `JWT_EXPIRE_HOURS` | Token lifetime (12). |
| `ALLOW_ANY_LOGIN` | Accept any credentials. Empty = follow `APP_ENV` (so it is already **on** in dev). |
| `ALLOW_SIGNUP` | Allow self-registration. Same `APP_ENV` fallback. |
| `SIGNUP_TENANT_ID` | Tenant assigned to self-registered users. |
| `API_KEY_HEADER` | **Dead key.** There is no `api_key_header` field on `Settings` and no consumer in `src/` — `extra="ignore"` swallows it silently. Kept only because it appears in older docs. |

Before any non-dev deployment: set a real `JWT_SECRET`, set `APP_ENV` to something outside
the dev list, and turn off `ALLOW_ANY_LOGIN` and `ALLOW_SIGNUP` explicitly — leaving them
empty is *not* the same as off if `APP_ENV` is still `dev`.

---

## 7. Services, Ports & Logging

| Key | What it does |
|---|---|
| `LOG_LEVEL` | Standard level. Companion settings `LOG_TO_REDIS`, `LOG_REDIS_MAX`, `LOG_REDIS_TTL` control the live dashboard log buffer. |
| `PUBLIC_BASE_URL` | The externally-reachable API base. The agents service uses it to build links in generated output ([`agents/main.py:393`](src/defense/services/agents/main.py#L393)) — if it is wrong, links point somewhere unreachable while everything else still works. |
| `AGENTS_SERVICE_URL` | Where the API reaches the agents service. `docker-compose.yml` overrides this to `http://agents:8010` for containers, so the value here is the one used for direct host runs. |
| `ANALYTICS_MCP_PORT` / `RETRIEVAL_MCP_PORT` / `INGEST_MCP_PORT` | Listen ports for the three MCP servers (8100 / 8101 / 8102). |
| `ANALYTICS_MCP_STUB` | `true` = the analytics MCP serves canned results instead of querying ClickHouse. Useful for a first run without the full stack; misleading if you forget it is on. |
| `RETRIEVAL_MCP_STUB` | Same for the retrieval MCP. |

---

## 8. Upstream Scraper

| Key | What it does |
|---|---|
| `UPSTREAM_API_URL` | Source of posts. Ingestion pulls `<url>/posts-with-details` ([`ingestion/service.py:424`](src/defense/services/ingestion/service.py#L424)); an empty value disables upstream pulls entirely. |
| `UPSTREAM_API_KEY` | Sent as `Authorization: Bearer <key>` when set. |

---

## Configured in code, not in this file

These have `Settings` defaults and can be overridden by adding them to `.env`, but they are
deliberately not in the shipped example:

| Key | Default | Notes |
|---|---|---|
| `STAGE2_CLASSIFIERS_ENABLED` | `true` | The two cheap voters in the comment ensemble. Skipped entirely in `MODEL_STUB_MODE`. |
| `STAGE2_CLASSIFIER_1` | `tabularisai/multilingual-sentiment-analysis` | Voter 1. |
| `STAGE2_CLASSIFIER_2` | `lxyuan/distilbert-base-multilingual-cased-sentiments-student` | Voter 2. Together with the LLM these are the **three verdicts** you see per comment. |
| `STAGE2_CLASSIFIER_DEVICE` | `auto` | `auto` tries GPU then falls back to CPU; `cpu` skips the GPU. The GPU usually already hosts the LLM — on a 4 GB card serving `qwen2.5:7b` there is ~285 MB left, which fits one classifier, not two. |
| `COMMENT_LLM_MODE` | `all` | `all` = every comment with text gets the LLM stance pass. `escalate` = only where the cheap voters disagree or a watchlist entity is mentioned (~20% of comments), at the cost of an empty LLM row on the rest. |
| `ROUTER_SUMMARY_ROUTES` | `false` | Whether "a summary is wanted" alone justifies Stage 2. **Keep it false** — Stage 1 writes a summary for every post, so setting it true fires the rule on every request and the gate stops gating. |
| `ROUTER_CONFIDENCE_THRESHOLD` | `0.8` | Plus `ROUTER_TOXICITY_THRESHOLD` (0.7), `ROUTER_LONG_TEXT_CHARS` (1000) — the other gate rules. |
| `LLM_CACHE_DISABLED` | `false` | Turns off the Redis LLM response cache. |
| `HF_OFFLINE` | unset | Overrides the `MODEL_STUB_MODE` → offline link in either direction. |

---

## Quick sanity check

```bash
python -c "
import sys; sys.path.insert(0,'src')
from defense.libs.llm.client import LLMClient
c = LLMClient()
for r in ['stage1','stage2','summary','agent','llm_a','llm_b','vlm']:
    print(f'{r:8} local={c.default_model(r,\"local\"):40} groq={c.default_model(r,\"groq\")}')
"
```

Prints the model each role resolves to after your `.env` is applied. Cross-check the local
column against `ollama list` — a name that isn't there fails at call time, not at start-up.

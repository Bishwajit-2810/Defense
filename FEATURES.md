# Feature List — What This System Does

Every capability, what it does, and **which of three states it is in**. That last
column is the point of this document.

[PROJECT_ASSESSMENT.md](PROJECT_ASSESSMENT.md) §9's framing advice: for every
capability on a slide, know whether it is **measured**, **implemented but
unexercised**, or **planned**. Examiners forgive the second and third when they
are labelled. What ends a defense is a claim in the second category presented as
if it were in the first — which is exactly what the routing gate was before §4,
and what the image modality was before §9.3.

| Legend | Meaning |
| ------ | ------- |
| ✅ **Measured** | Runs, and there is a number from running it |
| 🟡 **Works, unmeasured** | Runs and is tested, but no accuracy/perf figure exists |
| ⚠️ **Unexercised** | Implemented, but cannot currently produce a signal |
| 📋 **Planned** | Designed, not built |

**Scale (measured 17 Aug 2026):** **49,341 lines of Python across 170 files** —
`src/defense/services/` 21,001, `tests/` 17,065, `src/defense/libs/` 5,522,
`src/defense/mcp_servers/` 3,263, `eval/` 2,337, `run_all.py` 856. **1,261 tests**
across 62 files, all passing. Working corpus: 43 posts, 8,965 comments.
*(Was 27,537 lines / 673 tests on 5 Aug.)*

Test code is now 35% of the Python in the repository, up from 23% and 14% before
that. That ratio is the more meaningful number: the growth is almost entirely
regression tests pinning findings from
[PROJECT_ASSESSMENT.md](PROJECT_ASSESSMENT.md), and §9.12 records a
mutation-testing pass confirming they actually fail when the fixes are reverted.

---

## 1. Ingestion & data contract

| Feature | What it does | State |
| ------- | ------------ | ----- |
| **Post-with-details pull** | Pulls one payload per post — the post *with its comments embedded*, plus `engagement`, `reactionBreakdown`, `sampleShares` — into our own database. Read-only consumer; never writes back upstream. | ✅ Measured |
| **Platform detection** | Derives the platform from each post's URL host, so the service is not Facebook-specific. | ✅ Measured |
| **Idempotent upsert** | Every post has a stable content hash; re-ingesting is safe and re-analysis is a first-class operation. Postgres upserts on `post_id`, object storage writes a deterministic key, and the two ClickHouse tables collapse to the newest row per post — `comment_sentiments` in the engine, `analysis_events` in the queries. **Until 5 Aug 2026 the last of those was missing**, so a re-analysed post was counted twice in every analytics aggregate; see PROJECT_ASSESSMENT §11.2. | ✅ Measured |
| **Baseline preservation** | The upstream's coarse `sentiment`/`viralPotential` are kept as `baseline_*` and never overwritten, so our recomputation can be compared against theirs. | ✅ Measured |
| **Near-duplicate reuse** | A post within cosine 0.97 of an already-analysed one reuses that result and skips both stages. The result is **composed, not copied**: identity, engagement, reactions and timestamps come from the new post, only the post-level analysis is reused, and `processing.reused_from` records the source. The **comment thread is never reused** — a caption match is not a thread match, so the new post's thread is reported unanalysed rather than inheriting labels for comments nobody read. Reuse goes through the assembler, so all three stores are written. | 🟡 Works, unmeasured — was a verbatim row copy until 5 Aug 2026 (§13.3) |
| **Working-corpus filter** | `eval/make_text_corpus.py` writes `posts_text_only.json` (43 captioned posts) and prints exactly what it dropped and why. The source corpus is never modified. It is no longer any script's default input: eval runs read the full `posts_with_details.json` (50 posts), matching what ingestion uploads, and each script reports any null-caption posts it skips. | ✅ Measured |

---

## 2. Stage 1 — cheap NLP on every post and every comment

| Feature | What it does | State |
| ------- | ------------ | ----- |
| **Language + Banglish detection** | Classifies `bn` / `en` / code-mixed, and flags romanized Banglish. `language_method` reports whether fastText or the deterministic script heuristic produced it. | ✅ Measured |
| **Caption sentiment** | Recomputes post sentiment, language-routed to BanglaBERT / BanglishBERT / XLM-R. Reports `engine` (`stub`/`models`/`llm`) and the resolved model. | ✅ Measured (stub + LLM engines); real small models 📋 not yet run — see §9 |
| **Per-comment sentiment, full coverage** | Every comment in the stored thread gets a label. Three paths by comment `kind`: emoji-only, short, substantive. | ✅ Measured |
| **Comment kinds** | `emoji` (no word tokens) / `short` (1–2 tokens) / `substantive`. Emoji-only comments keep their sentiment but **never enter an LLM batch** — there is no text to read. Measured at **2.8%** of the corpus. | ✅ Measured |
| **Label provenance** | Per comment: `method` = `llm`/`model`/`stub`/`fast`/`emoji`/`failed`. Per post: a `provenance` block with `inferred_share`. `stub` is a hash of the text — reproducible, and *not* sentiment. Without this, `method_breakdown` claimed 8,513 model inferences in runs where zero models loaded. | ✅ Measured |
| **Emotion (7 labels)** | Per-post via a transformer; per-comment via the free emoji+lexicon heuristic. `emotion_method` says which — the per-comment table is heuristic even in real mode, which used to be invisible. | ✅ Measured |
| **Batched inference** | Groups comments by resolved model and runs one forward pass per group instead of one per comment. Transformer inference is 10–30× faster batched. | 🟡 Code complete, **speedup unverified** — no real weights installed |
| **Toxicity / hate scoring** | Feeds router rule 5. Under the keyword stub it never exceeds 0.2, so that rule is effectively inert without real models — the fallback log message says so. | ✅ Measured (as inert in stub mode) |
| **NER / brand mentions / keywords** | GLiNER multilingual + KeyBERT in real mode; seed lists and longest-token heuristics otherwise. | 🟡 Works, unmeasured |
| **Theme extraction** | Aggregates comment keywords weighted **sub-linearly** by likes. Raw-like weighting made one 946-like comment outweigh 946 ordinary ones, so "themes" was really the top comment's keyword list. | ✅ Measured |
| **Reaction cross-check** | `reactionBreakdown` (LIKE/LOVE/HAHA/WOW/SAD/ANGRY/CARE) nudges a neutral-or-positive verdict negative when SAD+ANGRY exceed 40%. A free crowd prior. | ✅ Measured |
| **Sentiment fusion** | Combines the available signals into `overall_sentiment`. **Weights renormalise over the terms that carry a real model verdict** — an absent image term no longer consumes its 0.4 and drag a real text signal 40% toward neutral. | ✅ Measured |
| **Image sentiment (SigLIP/CLIP zero-shot)** | Would produce a per-image verdict fused with text. | ⚠️ **Unexercised** — see §8 |
| **OCR (Tesseract bn+eng)** | Would extract embedded image text into the text path. | ⚠️ **Unexercised**, off by default (`STAGE1_OCR_SENTIMENT=false`) |
| **Degradation reporting** | `processing.degraded_components` lists every real-mode component that fell back because its model would not load. `engine: "models"` says which path was *intended*; this says what actually **ran**. Survival through the assembler and the API is asserted end to end — it was being silently dropped by both (§9.11). | ✅ Measured |

---

## 3. The router — the "thinking layer"

| Feature | What it does | State |
| ------- | ------------ | ----- |
| **Confidence-gated routing** | Six rules decide per post whether Stage 2 is worth it. **Measured: 16% of posts route on the shipped configuration, 74% under the keyword stub.** | ✅ Measured |
| **Named readers** | Every rule reads through a named reader accepting all three shapes the pipeline emits, so a field rename breaks a test instead of silently disabling a gate. A *missing* confidence now routes rather than being ignored. | ✅ Measured |
| **Env-tunable thresholds** | `ROUTER_CONFIDENCE_THRESHOLD`, `ROUTER_POST_TYPE_CONFIDENCE_THRESHOLD`, `ROUTER_TOXICITY_THRESHOLD`, `ROUTER_LONG_TEXT_CHARS` — which makes the gate sweepable rather than merely arbitrary. | ✅ Measured |
| **Exercised bypass leg** | A bypassed post is validated end to end against the output schema. That branch had never once executed before §4. | ✅ Measured |
| **Task flags** | Tells Stage 2 which tasks are actually needed, so a confidently-typed post does not pay for a redundant `post_type` call. | ✅ Measured |
| **Comment selection (the whole thread, by default)** | A second, separate decision the router makes: which comments Stage 2 analyses. `ROUTER_COMMENT_TOP_N` defaults to **0 = every comment with text**, and the selection is recorded either way (`stage2_selected` per comment, `stage2_selection` per post). A positive value keeps the top-N by reaction count instead — ranked by `likes`, tie-broken by original index so the choice is reproducible — for when a run needs to be fast rather than complete. Marked on the comments rather than filtered out, so all three stores receive the whole thread regardless. | 🟡 Works, unmeasured |
| **The gate decides post-level work only** | Comment analysis is **not** gated: every post reaches Stage 2 for its comments, and `stats:llm_routed` counts only the posts that got post-level tasks, so `estimated_llm_share` keeps its meaning. Bypassed posts used to go straight to the assembler, which left the majority of posts with three empty columns in the per-comment comparison and nothing saying why. | ✅ Measured |

> **Read the routing rate as a measure of Stage-1 quality, not cost efficiency.**
> A Stage 1 that types a post confidently bypasses Stage 2, so the rate *falls as
> Stage 1 improves*. And since every non-emoji comment now reaches an LLM,
> **85–96% of LLM calls are comment-level** — the gate governs 30–55% of spend
> depending on how good Stage 1 is. See §6.8 of the assessment.

---

## 4. Stage 2 — selective LLM

| Feature | What it does | State |
| ------- | ------------ | ----- |
| **Post summary in the original language** | A Bangla post gets a Bangla summary. `post_summary_grounding` records which inputs informed it. | ✅ Measured |
| **Separate `summary` role** | Summarization and classification resolve to **different models**: classification picks from a fixed vocabulary and wants a cheap constrained model; summarization writes prose and wants a fluent one. The expensive model is spent once per post, not on every classification call. | ✅ Measured |
| **Truncation recovery** | `finish_reason` is inspected; a reply that hit the token ceiling is auto-continued (up to `LLM_MAX_CONTINUATIONS`), trimmed to its last complete sentence, flagged `post_summary_truncated`, and **never cached**. Truncation is language-correlated — Bangla costs far more tokens per character — so this was silently biased against Bangla. | ✅ Measured |
| **Post-type classification** | Nine-label taxonomy shared from `src/defense/libs/labels.py` so Stage 1, the Stage-2 prompt and the router cannot drift apart. | ✅ Measured |
| **Insight / topic refinement** | Refines topics and intents, and writes a short one-line `insight`. **This row was wrong until 5 Aug 2026:** the LLM call ran and was paid for, but the assembler read `topics`/`intents` from Stage 1 only and never read `insight` at all, which was also absent from `output_schema.json` — so the entire task's output was discarded before it reached the API, the stores or the dashboard. Merged now (`builder._merge_stage2_labels`, schema `1.3`), pinned by `tests/test_stage2_insight_survives.py`. Pass 5 carried it the last hop: it reached the dashboard only as an untitled row in the bottom "All Fields" dump, and now has its own section beside the summary plus a Trace-tab row (§12.4d). | 🟡 Works, unmeasured |
| **Context-aware comment stance** | Re-labels comments by stance *toward the post*, with the post as context — a different and better signal than standalone comment sentiment. | ✅ Measured |
| **Eight-labeller comment ensemble** | Every analysed comment collects up to **eight** independent verdicts in `parallel_labels`: **seven small sentiment heads** batched on CPU (`STAGE2_CLASSIFIER_1..7` → `xlmr`, `distilbert`, `twitter_xlmr`, `banglabert`, `bengali_sentiment_bert`, `mbert`, `modernbert`) and the LLM stance pass. `libs/ensemble.combine()` is the **single writer** of the comment's final `sentiment` — three places used to write it and the last one won, which is how a post could report eight negative comments while every comment in the list read neutral. The dashboard shows all eight columns on one row. | 🟡 Works, unmeasured — the roster grew from 2 heads to 7 on 16 Aug 2026 and has not been scored since |
| **Only a model may label a comment** | Stage 1's emoji + keyword rule stopped voting on 17 Aug 2026: it answers on every comment (mostly with the deterministic hash stub — 11 of 12 on a real Bangla thread) and a voter that can never abstain makes every agreement number unfalsifiable. Its label is still disclosed in `method` / `provenance`, just never as a verdict. | ✅ Measured |
| **A voter that can't vote abstains** | A head that fails to load, or returns a label outside the taxonomy, contributes **nothing** — never a fabricated neutral. `label_voters` / `label_sources` report who actually spoke, so `label_agreement: 1.0` cannot render as "100% agree" for a comment one model read. Zero voters is `uncertain`, which is a statement about our confidence, not about the comment. A declared-but-silent head is logged at WARNING (`stage2_cheap_voters voted=2 declared=7`) — five of the original roster's checkpoints were base encoders or did not exist on the Hub, and voted zero times while reading as coverage. | ✅ Measured |
| **Roster prefetch + vote check** | `deploy/prefetch_classifiers.py` downloads the seven heads into the local HF cache and runs each on a Bangla/English/Banglish probe, so "downloaded" is never mistaken for "voting". `MODEL_STUB_MODE=true` means *download nothing*, so an unprefetched box degrades to LLM-only rather than stalling on a 3 GB fetch. | ✅ Measured |
| **Comment-thread summary** | A short natural-language account of how commenters reacted, grounded on the recomputed breakdowns. | 🟡 Works, unmeasured |
| **Bounded-concurrency batch queue** | Comments batch at 25 and run 3 batches in flight with per-batch retry and index-alignment assertions. Replaced a strictly sequential loop — which is why the old caps existed at all. | ✅ Measured |
| **Full comment coverage** | `STAGE1_LLM_COMMENT_MAX` and `COMMENT_STANCE_MAX_PER_POST` both default to **0 = no cap**. The old 60/40 defaults meant only ~29% of comments ever got an LLM label while the output described itself as full coverage. **Read this row with the next one:** since 17 Aug 2026 the *router* bounds the Stage-2 comment set to 100 per post, so "no cap" now describes these two knobs, not the pipeline. | ✅ Measured |
| **One analysis set, all models** | Whatever the router selects, **every** Stage-2 voter reads that same set — the seven cheap heads, the near-duplicate cache and the LLM stance pass. Replaces a cap that lived in the stance pass alone, so the LLM column of the per-comment comparison was empty on comments the cheap heads had all voted on. At the default (`ROUTER_COMMENT_TOP_N=0`) the set is **every comment with text**, so the post-level breakdown and the per-comment table describe the same comments. With a positive cap they diverge and the output says so: `ensemble.analysed` / `not_analysed`, and `uncertain` on each comment no model read. | 🟡 Works, unmeasured |
| **Response cache** | Content-addressed, 7-day TTL, keyed on `(backend, **resolved model id**, task, content_hash)`. The model id used to be a role label, so switching models re-served the previous model's answers — which would have silently invalidated any model comparison. `LLM_CACHE_DISABLED=1` for eval runs. | ✅ Measured |
| **VLM image-grounded summary** | Would ground the summary on actual image bytes. | ⚠️ **Unexercised** — no image is fetchable |

---

## 5. Watchlist-driven target stance — the novelty item

Full design: **[stance_targets.md](stance_targets.md)**.

| Feature | What it does | State |
| ------- | ------------ | ----- |
| **Configurable entity watchlist** | `config/stance_targets.yml` lists entities under `favored` / `opposed` / `neutral`. Answers *"who is this comment angry at?"* — which document-level sentiment cannot. | 🟡 Built + tested; **watchlist contents and validation outstanding** |
| **Alias matching across three scripts** | The load-bearing part. The same entity appears in Bangla script, romanized Banglish (no standard spelling) and English. Case-insensitive for Latin, exact for Bangla, Bangla suffixes allowed but not prefixes, token boundaries for initialisms. | 🟡 Built, 36 tests |
| **Unmatched-target reporting** | A target matching zero comments is logged as the likely alias-coverage bug it is, not read as "nobody discussed it". | 🟡 Built |
| **Two scorers, one shape** | LLM path injects matched targets into the Stage-2 stance call **that already runs** — so this feature costs **no additional LLM calls**. A deterministic clause-based fallback keeps it working in stub mode and CI. | 🟡 Built |
| **Separate output field** | `target_stances` is never merged into `sentiment`. A comment can be positive in tone while opposing a listed entity; conflating the two would destroy the only distinction the feature exists to make. | 🟡 Built |
| **Stated bias model** | A file declaring "support for X is positive" encodes a political stance into the labels. Legitimate for a monitoring product, indefensible presented as neutral measurement. Versioned, with a named owner per entry; the shipped file uses `neutral:` only. | ✅ Decided and enforced by the schema |
| **~150-comment validation** | Mention-detection precision/recall + stance agreement, reported separately. | 📋 Planned — this is what makes the novelty claim measurable |

---

## 6. Pluggable LLM backend

| Feature | What it does | State |
| ------- | ------------ | ----- |
| **`local` ⇄ `groq`, switchable at runtime** | `local` (Ollama/vLLM) has no per-token bill and no data egress; `groq` is fastest with no GPU to own. Flip it from the dashboard without a redeploy. | ✅ Measured |
| **Per-role model resolution** | `stage1` / `stage2` / `summary` / `llm_a` / `llm_b` / `vlm`, each with its own env override per backend. `processing.role_models` records what each resolved to. | ✅ Measured |
| **Automatic failover** | A Groq failure falls back to local mid-request; local failures propagate. Local is always available and privacy-safe. | ✅ Measured |
| **Circuit breaker** | A flapping backend is taken out of rotation for a cooldown instead of being hammered every request. Groq's open circuit pre-empts straight to local. | ✅ Measured |
| **JSON-mode degeneracy retry** | Ollama turns `response_format=json_object` into grammar-constrained decoding, and small models satisfy it with `{}` — a syntactically valid answer carrying no analysis. Retried unconstrained. | ✅ Measured |
| **Model bake-off harness** | `eval/bakeoff_summary.py` scores candidates on real Bangla posts: latency p50/p95, truncation rate, whether the summary stayed in the post's script, and a grounding proxy. | ✅ Measured (one run recorded) |
| **Tenant backend pinning** | A privacy-locked tenant cannot be routed to `groq` **by any path**: the API resolves each job's backend (request > toggle > env), applies the lock where the tenant is known, and stamps the decision into the job envelope, which Stage 1 and Stage 2 honour. An explicit request for `groq` is refused; the *global* toggle is silently downgraded to `local` for a locked tenant, because an operator's switch is not their choice. `PUT /v1/config/llm` needs an admin role. Fails closed — an unreadable policy table denies rather than permits. **Until 5 Aug 2026 this covered `/v1/chat` and agents only**: the pipeline read the global key and the guarded option was read by nobody (§13.5). | ✅ Measured |

---

## 7. Output, storage & API

| Feature | What it does | State |
| ------- | ------------ | ----- |
| **Canonical JSON schema** | One validated object per post+thread. Every result is schema-checked before persistence. | ✅ Measured |
| **Coverage honesty** | `coverage = analyzed / commentCount`, **clamped to 1.0**. Five posts store more comments than the platform reports (up to 112 against 42) — that surfaces as `coverage_anomaly`, never as "267% coverage". Corpus-level coverage is reported alongside the per-post figure. | ✅ Measured |
| **Three-store fan-out** | Postgres + pgvector (canonical + vectors), ClickHouse (analytics), object storage (raw results) — written in parallel. Every table either store creates now has a writer: the Postgres `llm_cache` (§11.3) and the ClickHouse `llm_usage` (§12.2) were both dropped for having none, and a test asserts the rule in both directions. | ✅ Measured |
| **Semantic search** | kNN over pgvector. Every result carries `embedding_is_stub`, because a hash-seeded stub vector returns *arbitrary* neighbours with scores that look exactly as plausible as real ones. The flag is **reported by Stage 1 and carried** to the column — it cannot be derived downstream, because the stub is the same 768 dims as a real vector, and deriving it from the dimension is why **every row was recorded as real** until 5 Aug 2026 (§13.2). `EMBEDDING_ALLOW_STUB=false` refuses the write outright. | ✅ Measured (as stub-backed by default) |
| **Keyword search** | JSONB search over the post's own text, summaries, keywords, topics and themes. `post_text` was added 5 Aug 2026 — without it, a post the router sent straight to the assembler has no `post_summary`, so its actual words were unsearchable. LIKE metacharacters in the query are escaped, so `q=%` no longer matches every row. | ✅ Measured |
| **Cluster-summarized reports** | The LLM cost lever architecture.md §5 names: cluster the corpus's embeddings (pure-numpy k-means, ≤8 clusters) and spend **one LLM-B call per cluster** rather than one per post. Returned as `embedding_clusters`, distinct from the zero-LLM SQL topic aggregate in `clusters`, and rendered in its own dashboard section. Clustering over stub vectors is disclosed (`embedding_clusters_are_stub`) so a summary of noise is never shown as a finding. **Until 5 Aug 2026 the summaries were computed, paid for and stripped by the response model** (§13.1). | 🟡 Works, unmeasured |
| **Cost telemetry** | `GET /v1/usage` reports tokens and cost **per backend and model** — local priced at 0.0 (its marginal token cost genuinely is zero) — plus a five-lane split (`post`, `comment`, `stage1`, `interactive`, `agent`) and `pipeline_tokens`, which isolates the per-post model from per-question chat/agent spend. Counters are written by `LLMClient` itself, so **every** caller is counted; they were written by the Stage-2 worker alone until 5 Aug 2026, leaving five callers uncounted under a `scope_note` claiming system-wide coverage (§13.4). | ✅ Measured |
| **Dead-letter queue** | Bounded retry-by-re-enqueue, then dead-letter with error context; the original is always ACKed. A dead-lettered post is **counted against its job**, so one LLM timeout no longer leaves the progress bar at 49/50 forever. | ✅ Measured |
| **Per-identity rate limiting** | A per-minute budget on expensive endpoints, with `X-RateLimit-*` headers and `Retry-After`. | ✅ Measured |
| **Distributed tracing** | OpenTelemetry → Jaeger/Loki, opt-in. | 🟡 Works, unmeasured |

### API surface

| Prefix | What it serves |
| ------ | -------------- |
| `/v1/auth` | `token`, `refresh`, `me`, `sse-ticket`, `verify` |
| `/v1/posts/upload`, `/v1/ingest/sync` | Batch upload and upstream pull |
| `/v1/analysis` | `run`, `{id}`, `{id}/stream` (SSE), `latest`, `overview`, `stats`, `post/{id}/comments` |
| `/v1/search` | Semantic + keyword |
| `/v1/reports`, `/v1/agents` | Grounded reports; agent runs |
| `/v1/chat` | Streaming general assistant |
| `/v1/usage` | Tokens and cost per backend/model |
| `/v1/pipeline`, `/v1/logs` | Live stage state and log tail, both SSE |
| `/v1/config` | Runtime LLM-backend and sentiment-model overrides |
| `/v1/health`, `/v1/ready` | Probes |

---

## 8. Authentication & tenancy

| Feature | What it does | State |
| ------- | ------------ | ----- |
| **JWT verified on every transport** | A JWT-shaped credential is verified as a token whether it arrives in `Authorization`, `X-API-Key`, or `?api_key=`. Previously only the header was parsed, so an **expired or forged** token authenticated on all four SSE streams. | ✅ Measured |
| **Claim allowlist** | Only `sub`/`tenant_id`/`role`/`exp`/`iat`/`scope` are taken from a token body, and `auth_method` is set server-side *after* the merge. The payload used to be spread last, so any claim in the token won. | ✅ Measured |
| **API keys in the database** | SHA-256 hashes in `api_keys`; the **tenant comes from the row**, never from the client. Unknown keys are rejected outside dev, so a deployment that forgot to provision keys fails closed. The degraded path — table unreadable — is genuinely degraded as of 5 Aug 2026: it used to swallow the error but leave the request's Postgres transaction aborted, so the endpoint 500'd on the first API-key request per process, which is the outcome that branch exists to prevent (§12.1, reproduced against Postgres 16). | 🟡 Built; needs rows seeded |
| **Password verification** | PBKDF2-SHA256, 200k iterations, salted. `POST /v1/auth/token` used to issue a signed 24-hour token to **anybody**. | 🟡 Built; needs rows seeded |
| **SSE tickets** | `POST /v1/auth/sse-ticket` returns a single-use, ~60-second, hash-stored ticket. `EventSource` cannot send headers, so *something* must go in the URL — a ticket makes that harmless. | ✅ Measured |
| **Sliding session** | `/refresh` allows `exp` to be 1 hour instead of 24; `/me` lets a client distinguish "no token" from "expired token". The dashboard tears down every stream on a 401, closing the split state where the UI said "logged out" while streams kept working. | ✅ Measured |
| **One secret, one source** | `JWT_SECRET` is read per call through `src/defense/libs/common/config.py` by both issuer and verifier, so it can be rotated without a restart and cannot drift. A fingerprint is logged at boot in each. Placeholder values are refused outside dev. | ✅ Measured |

---

## 9. Agentic insight layer

| Feature | What it does | State |
| ------- | ------------ | ----- |
| **Three agents** | `analyst` (Q&A over the corpus), `coverage` (comment-coverage analysis), `alerting` (monitoring). Corpus-tier only, never per post. | 🟡 Works, unmeasured |
| **MCP tool servers** | `analytics` / `retrieval` / `ingest` — internal, read-mostly; `ingest-mcp` never writes upstream. **History worth knowing:** `analytics-mcp`'s tools had only ever been exercised through `ANALYTICS_MCP_STUB=true`, which is how `get_reaction_mix` came to query a `reaction_events` table no migration creates — it raised on every real deployment while the stub returned plausible numbers (§11.3b). Fixed, a test asserts every table the handlers name is one the migration creates, **and as of Pass 5 all four real-path handlers have been run against a live ClickHouse** rather than only reasoned about (§12.5) — the dedup, the reaction mix and the top-N all verified end to end through the real `persist_clickhouse`. | ✅ Measured (analytics); 🟡 unmeasured (retrieval/ingest) |
| **Budget cap** | `max_tool_calls` with a synthetic "budget cap reached" tool reply, so an agent loop cannot run away. | ✅ Measured |
| **Citations** | Post IDs are extracted from tool results so an answer can be traced to its evidence. | 🟡 Works, unmeasured |
| **Prompt-injection hardening** | Tool results carry Facebook comment text verbatim, on a corpus of political content with adversarial participants. Results are wrapped in `<tool_data trust="untrusted">` with forged-delimiter neutralisation, and the system prompt states that content inside is data. **Risk reduction, not a fix** — no prompt-level defence can promise resistance. | 🟡 Built + probed |

---

## 10. Dashboard & observability

| Feature | What it does | State |
| ------- | ------------ | ----- |
| **React dashboard** | React 19 + Vite + Tailwind (`dashboard/`), charts via chart.js: Posts, Overview, Jobs, Trace, Pipeline, Logs, Chat, Reports, Agents. Replaced the vanilla HTML/CSS/JS build the design docs specify, which is preserved at `dashboard_legacy/`. | ✅ Measured |
| **Per-post live trace** | Streams per-stage events over SSE, including explicit `skipped` frames when Stage 2 is bypassed and the routing gate's own inputs next to its verdict. A live per-post trace during a defense is worth a lot. | ✅ Measured |
| **Batch progress** | `batch k/N` frames during long comment passes, so a 115-batch thread does not look hung. | ✅ Measured |
| **XSS-safe rendering** | Facebook comment text is attacker-controlled input. On the React build, JSX escapes every interpolated value, so comment and caption text cannot inject markup. **One `dangerouslySetInnerHTML` remains** — `Overview.jsx`'s `StatCard subtext`, whose only HTML-bearing caller is a locally-built coverage string of numbers (`coverageHtml`), never upstream text. It is still the one place a future caller could pass through a post field, so it is named here rather than described as absent. The `escHtml`/`escAttr` helpers this row used to describe belong to `dashboard_legacy/`. | 🟡 Works, unmeasured on the React build |
| **Provenance in the UI** | Label-provenance share, truncation badges, coverage anomalies and the emoji-vs-written split are all surfaced — so a chart can state what produced it. | ✅ Measured |
| **Prometheus + Grafana + Loki** | Metrics, dashboards, log aggregation. | 🟡 Works, unmeasured |

---

## 11. Scale & operations

| Feature | What it does | State |
| ------- | ------------ | ----- |
| **Queue-based, stage-isolated workers** | Redis Streams between ingestion → Stage 1 → router → Stage 2 → assembler. Each stage scales independently; nothing is a monolith with a `main()`. | ✅ Measured |
| **Single source of truth for identifiers** | `src/defense/libs/streams.py` owns every stream and consumer-group name, and a test asserts the KEDA manifests agree. Four separate bugs in this system have been one component writing a string and another reading a different one. | ✅ Measured |
| **KEDA autoscaling** | One `ScaledObject` per worker, triggering on consumer-group **lag** (`lagCount`). `pendingEntriesCount` cannot scale from zero — with no replicas there is no consumer, so nothing is ever delivered and the count stays 0 forever. | 🟡 Manifests correct + test-asserted; **not run in a cluster** |
| **Docker Compose + Kubernetes** | Compose for the MVP, 10 K8s manifests with network policies, secrets and ingress for production. | 🟡 Works, unmeasured |
| **Dev orchestrator** | `run_all.py` brings the whole stack up locally and loads the sample corpus. | ✅ Measured |

---

## 12. Evaluation & measurement tooling

| Tool | What it reports | State |
| ---- | --------------- | ----- |
| `eval/measure_routing_rate.py` | Routing rate per Stage-1 engine, rules fired, comment volume, and the post-vs-comment LLM call split | ✅ Measured |
| `eval/make_text_corpus.py` | The working corpus, and exactly what it excluded | ✅ Measured |
| `eval/bakeoff_summary.py` | Per-model latency, truncation rate, language fidelity, grounding proxy | ✅ Measured |
| `eval/sweep_threshold.py` | Cost-vs-threshold and cost-vs-comment-cap curves | ✅ Measured (cost axis only) |
| `eval/harness.py` | Structural checks: input validation, platform detection, coverage | ✅ Measured |
| **Accuracy metrics** | Macro-F1 per language bucket, κ agreement, calibration | 📋 **Planned — zero gold labels exist.** The one decisive gap (§7.2) |

---

## 13. What is deliberately not claimed

Being explicit about this is worth more in a defense than one more feature.

- **Multimodal sentiment.** The image path is implemented but **has never
  produced a signal**: the corpus's 69 `photoUrls` are relative object-storage
  keys and the objects are not in MinIO. Post sentiment is a **text**
  measurement. A failed fetch now reports `vision_status`, not a fabricated
  neutral verdict.
- **Accuracy.** There are no gold labels, so there is no F1, no confusion matrix
  and no ship gate. Everything above marked ✅ is a *system property* — a rate, a
  count, a latency — not a correctness claim.
- **"Cost-efficient" as a headline.** The routing rate is 16%, not single digits,
  and cost is comment-dominated. The defensible claim is *"cheap NLP filters
  which comments and which posts deserve an LLM"* — narrower, and measured.
- **Real-mode performance.** `MODEL_STUB_MODE=false` runs, but with no ML
  dependencies installed every component degrades to a heuristic and says so via
  `degraded_components`. **No latency or accuracy number from such a run is
  quotable.**
- **Prompt-injection immunity.** The standard mitigations are in place; no
  general solution exists, and the tests do not pretend one does.
- **Resistance to a leaked API key or token.** Keys are hashed and tenant-scoped,
  but revocation is a database update, not a live blocklist.

---

## 14. Where to read more

| Document | Covers |
| -------- | ------ |
| [PROJECT_ASSESSMENT.md](PROJECT_ASSESSMENT.md) | **Read the status header first.** Every finding, what was fixed, what is measured, and what is still open |
| [stance_targets.md](stance_targets.md) | The novelty item in full — design, bias framing, validation plan |
| [data_contract.md](data_contract.md) | The upstream input contract and the golden rules |
| [architecture.md](architecture.md) | Services, data flow, the hybrid pipeline |
| [models.md](models.md) | Model choices per task, the role system, fine-tuning strategy |
| [evaluation.md](evaluation.md) | How each signal would be scored, the sampling frame, and what exists today |
| [endpoints.md](endpoints.md) | Every endpoint with a real request and response |
| [run.md](run.md) / [easy_run.md](easy_run.md) | Getting it running, and every env knob |

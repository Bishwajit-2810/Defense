# RAG — Current State, Roadmap, and Required Agent Changes

> **Scope:** what the retrieval layer actually stores and does today (traced through
> the code, not the design docs), what to improve, what else is worth storing, and
> — for each change — whether the agent layer has to change with it.
>
> Companion to [AGENTIC_RAG_NOVELTY.md](AGENTIC_RAG_NOVELTY.md), which frames the
> subsystem as a thesis contribution. This document is the engineering view.

> **Companion:** [SEARCH.md](SEARCH.md) is the user-facing view of this layer — the
> `/v1/search` modes, the identifier short-circuit, RRF, and the same measured
> recall@k table read as a retrieval result rather than as a roadmap item.


---

## 0. Implementation status

Sections 1 and 2 describe the system **as it was**. The roadmap in §3–§6 has now
been built, with two deliberate exceptions. Read §6 for the per-item detail; this
is the summary.

§6 carries two audit sections written after the fact, by testing the claims above
rather than by making them. **Section 7** re-read §0–§6 against the code and found
seven defects, one serious: a database failure in the chunk or comment-vector
write silently discarded the entire post, analysis row included, while logging
success. **Section 8** re-audited with Section 7 itself treated as unverified and
found three more, one serious: a `CUDA out of memory` on the shared 4 GB GPU made
the encoder fall back to hash vectors, so retrieval degraded to ranking noise
intermittently — and it had already corrupted two analyses during the audit
before an assertion caught it.

**Section 9** covers what broke in the *agent* layer once retrieval started
returning real Bengali payloads — a silent 4k context window, `ensure_ascii`
doubling the token bill, repeat-call loops, and two new shapes of non-answer.

Everything in the table below still holds and the measurements re-run unchanged,
but read both sections before trusting this table on its own. Section 8 also
stratifies the eval by query leakage, which changes how the headline numbers
should be read — in the embeddings' favour.

| Item | Status |
| :--- | :--- |
| §3.1 Real embeddings | **Done** — `EMBEDDING_STUB_MODE=false` on a real multilingual encoder; the refusal switch now actually refuses |
| §3.2 Comment vectors | **Done** — `comment_embeddings` table, batch encoding, near-duplicate sharing, backfill script |
| §3.3 Hybrid + RRF | **Done** — both arms in `semantic_search`, `search_comments` and `GET /v1/search?mode=hybrid` |
| §3.4 Reranking | **Done, off by default** — `RETRIEVAL_RERANK=true`; the model is installable now |
| §3.5 Chunking | **Done** — `post_chunks`, Bangla-aware splitter, chunk arm in `semantic_search` |
| §3.6 Silent truncations | **Done** — cluster cap disclosed per row; date window on `semantic_search` |
| §3.7 Vector versioning | **Done** — `embedding_model` / `embedding_dim` columns, mixed-space warning |
| §3.8 Retrieval eval | **Done** — `eval/build_retrieval_set.py`, `eval/score_retrieval.py` |
| §5.2 Honesty gap | **Done** — `embedding_is_stub` on every result row + quality-agent prompt |
| §5.3 `search_comments` | **Done** — tool, allowlists, runner guards, budgets |
| §5.4 Narrative mismatch | **Done twice** — prompt fixed, and `cluster_labels` gives it a real label to use |
| §4 Other stores | **Partly** — chunk vectors and persisted cluster labels done; the rest still candidates |

### Python 3.12

`.python-version` pins 3.12 and the venv was rebuilt on it. The `ml` extra cannot
be installed on 3.13 at all: `fasttext-wheel` 0.9.2 publishes no cp313 wheel and
its bundled C++ no longer compiles (`args.cc` fails on an out-of-scope
declaration). Everything else in the extra is version-agnostic — the language
detector alone was holding the embedding model hostage, which is also why
`embeddings` now exists as a separate, lighter extra.

### The measurement that was missing

`python -m eval.score_retrieval` over 32 known-item queries, k=10, on the live
50-post corpus. The same set was run before and after the §3.1 flip, so the two
recall columns differ only in whether the vectors are hashes:

| config | recall@10 (stub) | recall@10 (real) | MRR@10 (real) | nDCG@10 (real) |
| :--- | ---: | ---: | ---: | ---: |
| `vector` — the old behaviour | 0.1875 | 0.6250 | 0.2869 | 0.3667 |
| `lexical` (fts + trgm, fused) | 0.3750 | 0.7500 | 0.6503 | 0.6748 |
| `chunk` (§3.5) | — | 0.6562 | 0.3014 | 0.3846 |
| `hybrid` (§3.3) | 0.5312 | 0.8750 | 0.6108 | 0.6759 |
| `chunked` = chunk + fts + trgm | — | **0.8750** | **0.6482** | **0.7041** |

Four things worth reading out of it.

**The stub vectors were indistinguishable from chance.** 0.1875 recall@10, where
returning 10 random posts out of 50 scores 0.20. §3.1 asserted that in prose for
a long time; this is the number.

**Real embeddings are worth 3.3x on the vector arm** (0.1875 -> 0.6250), and the
end-to-end configuration went from 0.5312 to 0.8750 — a 65% relative gain.

**The lexical column moved too, and that was a bug, not a benefit.** `lexical`
went 0.3750 -> 0.7500 with no vector involved, because the full-text half of the
arm had never fired: `plainto_tsquery` ANDs every term, so an eight-token
question demanded all eight words in one caption and matched ZERO rows across
the whole set. Everything previously credited to "full-text + trigram" was
trigram alone, and the GIN index built for FTS was dead weight. See §6 step 5.

**Chunking is real but small, and now hidden.** It was +0.031 recall over
`hybrid` while the lexical arm was weak; with the arm fixed both reach 0.8750 and
the remaining gain shows up in ranking (MRR 0.6108 -> 0.6482, nDCG 0.6759 ->
0.7041). Expected either way: only 14 of 50 posts exceed 600 characters, so 50
posts yielded 86 chunks and most are a single chunk identical to the caption.

All figures are lower bounds: the set marks one relevant post per query, so every
other on-topic post counts as a miss. They compare retrievers against each other
on one set; they are not absolute quality figures. Note also that `lexical` alone
scores a higher MRR than `hybrid` — an artefact of queries derived from each
post's own topics and entities, which favours literal matching. `hybrid` wins on
recall@k, which is what matters when the whole top-k is handed to an LLM.

One caveat on the **stub** column specifically, found later (Section 7 item 6):
it was measured before the harness applied the 0.2 down-weight `semantic_search`
puts on a hash-stub query vector, so it reflects an unweighted fusion the server
does not run in stub mode. The `real` columns are unaffected — the weight is 1.0
either way — and re-run unchanged at the production pool and weights.

`reranked` is implemented and **unmeasured** — it needs the cross-encoder weights
(`RETRIEVAL_RERANK=true`). The harness prints `skipped` rather than silently
reporting the fused order as though a rerank had happened.

---

## 1. What the RAG stores today

Everything vector-related lives in **one Postgres column**.

```
analysis_results                      -- deploy/init-db.sql:35, UNIQUE(post_id)
  post_id            VARCHAR          -- one row per post
  campaign_id        VARCHAR
  tenant_id          VARCHAR
  result             JSONB            -- the canonical analysis document
  embedding          vector(768)      -- ONE vector per post
  embedding_is_stub  BOOLEAN          -- provenance: is this vector semantic?
  + idx_analysis_embedding_hnsw        (hnsw, vector_cosine_ops)
```

The `result` JSONB is the real payload — `post_summary`, `post_text`,
`overall_sentiment`, `topics`, `keywords`, `entities`,
`comment_analysis.themes`, `comment_analysis.representative_comments`,
`comment_analysis.target_stances`. Keyword search scans it; semantic search
returns fields out of it.

### Two facts about that vector that shape everything downstream

**(a) It is built from the post caption only.**
[`worker.py:372`](../src/defense/services/workers/stage1_nlp/worker.py#L372) calls
`analyze_text(caption, ...)`, and the embedding falls out of that call
([`text_analyzer.py:871`](../src/defense/services/workers/stage1_nlp/text_analyzer.py#L871)).
The only alternative source is OCR text, behind `_OCR_SENTIMENT`, for
null-caption image posts.

**Comments are never embedded.** The [`comments`](../deploy/init-db.sql#L55) table has
`text`, `author`, `likes`, `sentiment` — and no vector column. The per-comment
ensemble labels every comment, but none of that text is retrievable by meaning.

> **Now fixed (§3.2).** Comment vectors live in `comment_embeddings`, written in
> the same transaction as the analysis row and reachable through the
> `search_comments` tool.
>
> **Retrieval coverage and ensemble coverage are two different caps — do not
> conflate them.** Embedding is done by the **assembler** over the whole stored
> thread, bounded by `COMMENT_EMBEDDING_MAX_PER_POST`, which as of 18 Aug 2026 is
> **`0` — uncapped** (it was `1000`; see §6 item 2 and §5.3.2 below for what that
> cap was hiding). The Stage-2 ensemble reads every comment with text
> (`ROUTER_COMMENT_TOP_N=0`, the default), so with both at `0` the two now
> coincide on every thread. Set either to a positive value and they diverge — and
> then
> `search_comments` can retrieve a comment that **no model labelled**, whose
> `sentiment` is `uncertain` (Stage 1's keyword label does not vote). An agent
> quoting a retrieved comment's sentiment must not imply the ensemble judged it;
> `stage2_selected` is the field that separates the two.

**(b) In the default configuration the vector is not semantic.**
`.env` sets `MODEL_STUB_MODE=true`, so
[`embeddings.py:62`](../src/defense/libs/embeddings.py#L62) returns a SHA-256-seeded
random unit vector — correct dimension, correct HNSW index, arbitrary
neighbours. `EMBEDDING_ALLOW_STUB` defaults to `true`
([`config.py:329`](../src/defense/libs/common/config.py#L329)), so persistence
writes them without complaint.

The plumbing that carries this fact is good: `embedding_is_stub` is produced at
the one layer that knows
([`_embed_with_provenance`](../src/defense/services/workers/stage1_nlp/text_analyzer.py#L535)),
carried through the assembler, stored on the row, and surfaced by
`GET /v1/search` and `get_clusters`. **It is not surfaced by the retrieval-mcp
`semantic_search` tool** — see §4.1.

### What is *not* in the vector store

| Store | Contents | Vectors? |
| :--- | :--- | :--- |
| Postgres `posts` | raw scraped payload (JSONB) | no |
| Postgres `comments` | flat comment rows | **no** |
| Postgres `chat_messages` | conversation history + tool provenance | no |
| ClickHouse `analysis_events` | post-level aggregates, reactions, watchlist | no |
| ClickHouse `comment_sentiments` | per-comment labels + ensemble provenance | no |
| Object storage | raw artefacts | no |

---

## 2. What the RAG does today

```mermaid
graph LR
    Q["Question"] --> R["AgentRunner<br/>tool-use loop"]
    R --> S["semantic_search"]
    S --> E["embed query<br/>libs/embeddings.py"]
    E --> PG[("analysis_results.embedding<br/>HNSW cosine")]
    PG --> K["top-k rows"]
    K --> R
    R --> A["Answer + citations"]
```

### The retrieval surface

`retrieval-mcp` (:8101) exposed **8 tools**
([`server.py`](../src/defense/mcp_servers/retrieval_mcp/server.py)) — now 9, with
`search_comments` added by §3.2:

| Tool | Backing | Notes |
| :--- | :--- | :--- |
| `semantic_search` | pgvector kNN | the only vector-search entry point |
| `search_comments` *(new)* | pgvector kNN + trigram over comment text | §3.2 |
| `get_post` | Postgres | full analysis JSON for one post |
| `get_thread` | Postgres | post + all comments, ordered by likes |
| `representative_comments` | JSONB, falls back to `comments` table | |
| `get_clusters` | pgvector + k-means/HDBSCAN | capped at 100 rows — see §3.6 |
| `stance_by_target` | JSONB `target_stances` | |
| `stance_over_time` | JSONB + date window | |
| `coverage_stats` | Postgres | |

### The retrieval algorithm

One statement — [`server.py:188`](../src/defense/mcp_servers/retrieval_mcp/server.py#L188):

```sql
SELECT post_id, campaign_id, overall_sentiment, post_summary,
       1 - (embedding <=> :qvec) AS score
FROM analysis_results
WHERE embedding IS NOT NULL [AND campaign/tenant/sentiment filters]
ORDER BY embedding <=> :qvec
LIMIT :limit
```

That is the whole thing. There is **no chunking, no reranking, no query
rewriting or expansion, no MMR/diversity, no score threshold, and no date
filter**. Retrieval quality is entirely whatever the raw kNN returns.

> **Now:** that single statement is one of **three** arms (dense, `fts`, `trgm`)
> fused by RRF, with an optional cross-encoder rerank, a date window, and chunk
> vectors as the dense arm. Query rewriting/expansion, MMR/diversity and a score
> threshold remain absent — see §6 "Still not done".

### Keyword vs semantic are mutually exclusive

`GET /v1/search?semantic=true|false`
([`search.py:56`](../src/defense/services/api/routers/search.py#L56)) branches into
*either* a JSONB `LIKE` scan *or* a pgvector search. There is no fusion, and
`semantic_search` (the tool the agents actually use) has no lexical mode at all.

### Clustering as the LLM cost lever

[`get_clusters`](../src/defense/mcp_servers/retrieval_mcp/server.py#L1315) pulls
vectors, runs seeded k-means (or HDBSCAN), and returns one representative post
per cluster so the report summarises a handful of slices instead of N posts
([`clustering.py`](../src/defense/libs/clustering.py)).

---

## 3. How to improve it

Ordered by payoff.

### 3.1 Turn the embeddings on — everything else depends on this

`MODEL_STUB_MODE=false` + `uv sync --extra ml`, and flip
`EMBEDDING_ALLOW_STUB=false` so persistence *refuses* to write a hash stub
rather than silently accepting one.

Until this happens, semantic search, `get_clusters`, and the cluster summaries
in reports are all operating on noise. Every other improvement below is
unmeasurable while this is true.

### 3.2 Embed comments, not just captions

The single biggest structural gap. The corpus's signal lives in comment threads
— that is the entire reason the per-comment ensemble exists — but a question
like *"where are people angry about fuel prices?"* can only ever match caption
text. The anger is in rows that carry no vector.

Add a vector column to `comments`, or a dedicated table:

```sql
CREATE TABLE comment_embeddings (
  comment_id  VARCHAR,
  post_id     VARCHAR REFERENCES posts(id),
  tenant_id   VARCHAR NOT NULL DEFAULT 'default',
  embedding   vector(768),
  embedding_is_stub BOOLEAN DEFAULT FALSE,
  embedding_model   VARCHAR,       -- see §3.7
  PRIMARY KEY (post_id, comment_id)
);
```

Batch-encode per post — one `model.encode(list_of_texts)` call per thread, not
one per comment. Reuse [`comment_groups.py`](../src/defense/libs/comment_groups.py)
to skip near-duplicates: embed the representative, point the duplicates at it.

### 3.3 Hybrid retrieval with rank fusion

Unusually valuable here because the corpus is code-mixed Bangla / English /
Banglish: exact entity names, transliterations, and hashtags are precisely what
a multilingual sentence encoder blurs and what lexical search nails.

Add a `tsvector` column (or `pg_trgm` index), run both retrievals, and fuse with
reciprocal rank fusion rather than making the caller pick a mode:

```
score(d) = Σ  1 / (60 + rank_i(d))       over each retriever i
```

### 3.4 Rerank the top-k

Retrieve 30–50, rerank down to 8. Either a cross-encoder (`bge-reranker-v2-m3`
is multilingual and small) or a cheap LLM rerank pass. Highest quality-per-line
change after §3.1, and it costs latency rather than tool calls.

### 3.5 Chunk long captions and threads

One vector per post averages a long post into mush and forces retrieval to
return whole documents. A chunk table keyed `(post_id, chunk_idx)` improves both
precision and citation granularity.

### 3.6 Fix two silent truncations

- [`get_clusters`](../src/defense/mcp_servers/retrieval_mcp/server.py#L1315) hardcodes
  `LIMIT 100 ORDER BY created_at DESC`. "Corpus themes" is really "themes of the
  100 newest posts, in at most 8 buckets" — and it reads as corpus-wide.
- `semantic_search` accepts no date window, even though `_date_filter` already
  exists in the same file for the stance tools. Given the corpus is roughly three
  months behind the current date, recency scoping matters.

Either raise the caps or make the tool *say* what it dropped.

### 3.7 Version the vectors

Add `embedding_model` and `embedding_dim` columns to `analysis_results`. Change
`EMBEDDING_MODEL` today and old rows stay in the old vector space — still
comparable, silently wrong, with no way to detect it. `fit_dim()` will even
truncate/pad a mismatched model into place with a single warning.

### 3.8 Measure retrieval

There is no recall@k or MRR anywhere in the repo. Fifty queries with known-relevant
post ids would let any of the above be shown to help rather than assumed to. The
unlabelled 300-comment gold set is the natural starting point.

---

## 4. What else is worth storing

| Candidate | Why | Effort |
| :--- | :--- | :--- |
| **Comment vectors** (§3.2) | makes the actual discourse retrievable | M |
| **Chunk vectors** | precision + citation granularity on long posts | M |
| **Persisted cluster centroids + LLM-written labels** | themes stay stable across reports instead of being recomputed each time | S |
| **Target/entity profile vectors** | "who is being attacked and how is it shifting" becomes a vector query, not a JSONB scan | M |
| **Image + OCR vectors** (CLIP) | vision path and `photoUrls` exist; image content is entirely unretrievable today | L |
| **Narrative / claim-level vectors** | dedupe recurring narratives across campaigns and over time — turns per-post analysis into campaign-level intelligence | L |
| **Chat history vectors** | `chat_messages.meta` already carries provenance; embedding gives cross-session memory | S |
| **External document corpus** | press releases, fact-check archives, prior reports — today agents can only cite the corpus back to itself | L |
| **Author / account vectors** | natural substrate for coordination detection | L |

---

## 5. Do the agents need to change?

Short answer: **most RAG changes are transparent to the agents, but three of them
are not — and one requires a change to the runner's fabrication guards, not just
to prompts.**

The agent layer is
[`registry.py`](../src/defense/services/agents/registry.py) (nine agents, each with
a fixed `tools` allowlist and a `max_tool_calls` budget) driven by
[`runner.py`](../src/defense/services/agents/runner.py) (OpenAI tool-call loop,
plus citation/quote verification).

### 5.1 Per-change impact

| RAG change | Agent change needed? | What exactly |
| :--- | :--- | :--- |
| §3.1 Real embeddings | **No** | tool signatures unchanged; results just get better. But see §5.2 — the honesty gap becomes *more* important, not less |
| §3.2 Comment vectors | **Yes — significant** | new tool + allowlists + runner guards. See §5.3 |
| §3.3 Hybrid + RRF | **No** | same tool, same shape, better ranking |
| §3.4 Reranking | **No** | costs latency, not tool calls; `max_tool_calls` budgets unaffected |
| §3.5 Chunking | **Yes — small** | results carry `chunk_idx`; prompts must still cite `post_id` and use `get_post` to expand |
| §3.6 Date filter on `semantic_search` | **Yes — small** | new optional arg; prompts should mention recency scoping |
| §3.6 Cluster caps | **Yes — small** | narrative agent must report the cap; see §5.4 |
| §3.7 Vector versioning | **No** | internal to persistence |
| §4 External doc corpus | **Yes — large** | new tool, new citation shape, probably a new agent |

### 5.2 The honesty gap that real embeddings do *not* close

`GET /v1/search` returns `embedding_is_stub` per result and logs
`semantic_search_over_stub_vectors`. `get_clusters` returns `is_stub` per
cluster. **The retrieval-mcp `semantic_search` tool returns neither** — its
result dict is `post_id`, `score`, `campaign_id`, `overall_sentiment`,
`post_summary` ([`server.py:206`](../src/defense/mcp_servers/retrieval_mcp/server.py#L206)).

So an agent that says *"the most semantically similar posts are…"* is asserting
something it has no way to check, and in the default configuration that
assertion is false. The runner's fabrication guards
(`_unverified_citations`, `_unverified_quotes`) catch invented ids and invented
quotes — they cannot catch a real id retrieved by a meaningless ranking.

**Fix regardless of §3.1:** add `embedding_is_stub` to the `semantic_search`
result rows, and give the `quality` agent's prompt a line instructing it to
report vector provenance alongside coverage and ensemble agreement.

### 5.3 Comment-level search — the one change with real agent surface area

A `search_comments` tool would need:

**Allowlist additions.** `toxicity` most of all — today it can only reach toxic
comments by walking `top_posts` → `get_thread`, i.e. it can find toxic *posts*
and then read their threads, but cannot search for a harassment pattern
directly. Also `analyst`, `stance`, and `narrative`.

**A runner change.** [`_extract_post_ids`](../src/defense/services/agents/runner.py#L475)
and the citation verifier are built around post ids. Comment-level results
introduce `comment_id`s that the verifier does not recognise, so a fabricated
comment citation would pass unchallenged while a real one might be flagged. The
guard needs a comment-id shape before this tool ships — otherwise the change
that most improves grounding also opens the largest hole in the fabrication
checks.

**Budget review.** `toxicity` and `stance` sat at `max_tool_calls=10`. Comment
search encourages more, narrower calls; watch for agents truncating mid-analysis.
*Shipped values (20 Aug 2026):* `coverage` 5, `alerting` 8, `quality` 8, `analyst`
10, `stance` 12, `narrative` 12, `toxicity` 14, `comparator` 15, `reporter` 15 —
[`registry.py`](../src/defense/services/agents/registry.py) is the source of truth.
`alerting` went 5 → 8 after a live run hit the cap and returned
`[Budget cap of 5 tool calls reached]` **as the briefing**, having answered
nothing; the runner now also refuses to dispatch a byte-identical repeat, which
is what was consuming the budget in the first place (§6, agent hardening).

### 5.4 A live prompt/tool mismatch worth fixing now

`NARRATIVE_SYSTEM_PROMPT` asks for a *"Narrative Theme Summary Table (cluster
name, post count, dominant sentiment)"*. `get_clusters` returns no `name` field
— only `representative_summary`. The agent is being asked for a column its tool
cannot supply, so it invents one. Persisting LLM-written cluster labels (§4)
fixes this properly; renaming the column in the prompt fixes it today.

### 5.5 What does not need to change

Nine agents, their personas, the router's description-matching, the tool-use
loop, and the fabrication guards all stay as they are for §3.1, §3.3, §3.4 and
§3.7. The MCP boundary is doing its job: better retrieval behind the same tool
contract is invisible to the agent layer, which is the main argument for having
put it there.

---

## 6. Order of work — what was built

1. **§3.1 — taken, and the switch was fixed first.** `EMBEDDING_ALLOW_STUB=false`
   was a weaker switch than it read as: the refusal sat only on the "no usable
   vector" path, so it fired when Stage 1 sent *nothing* and stayed silent for
   the case the flag exists for — a correctly-sized, correctly-flagged hash
   stub, which is what every post produced by default. An operator who set it to
   keep noise out of the index got a full index of noise and no error. Fixed,
   with a test, before the flip made it load-bearing.

   The flip itself is **`EMBEDDING_STUB_MODE=false`, not `MODEL_STUB_MODE=false`**.
   The latter is one switch over seven models — sentiment, emotion, toxicity,
   CLIP, NER, KeyBERT and the sentence encoder — and retrieval needs only the
   last. The other six land on a GPU that is normally already hosting the LLM
   (the `stage2_classifier_device` comment puts the free VRAM on a 4 GB card at
   ~285 MB), so tying "make search meaningful" to "load the vision stack" made
   the first change cost the second. The new setting defaults to following
   `MODEL_STUB_MODE`, so it is a no-op unless set.

   Corpus state after the flip: 50 caption vectors, 86 chunk vectors and 8,415
   comment vectors, all `paraphrase-multilingual-mpnet-base-v2`, re-embedded in
   28 s on the GPU. `coverage_stats` reports `stub_embedding_share: 0.0` and a
   single entry in `embedding_models`.

   The encoder is doing the job the corpus needs: EN *"fuel price protest"* vs BN
   *"জ্বালানি তেলের দাম বৃদ্ধির প্রতিবাদ"* scores **0.771**, against −0.042 for an
   unrelated sentence. That cross-lingual alignment is why an English operator
   question now retrieves Bangla posts on merit rather than by luck.

2. **§5.2 done.** `semantic_search` rows carry `embedding_is_stub` and
   `matched_by`; `search_comments` and the recency-stub path carry the same
   fields, so a caller never learns a shape the live server does not produce.
   The `quality` agent's prompt now names `stub_embedding_share`,
   `embedding_models` and `comment_vector_coverage` as first-class findings, and
   `coverage_stats` returns all three.

3. **§5.4 done.** The narrative prompt asked for a "cluster name" that
   `get_clusters` has never returned. It now asks for `representative_summary`,
   is told that a theme name of its own is its reading rather than data, and is
   told to report `scan_truncated`.

4. **§3.8 done — and it is what makes 5–8 defensible.** `build_retrieval_set.py`
   writes known-item queries derived from each post's topics and entities, with
   a leakage guard that drops any query more than 60% contained in the target's
   own indexed text. `score_retrieval.py` scores recall@k / MRR@k / nDCG@k across
   four configurations by importing the *same* arm functions the MCP server uses,
   so it measures what the agents get rather than a copy. Every row is
   `human_verified: false` and the harness reports the verified subset
   separately — the set is honest about being derived, not judged.

5. **§3.3 done — three arms, after two corrections.** `pg_trgm` + a
   `to_tsvector('simple', …)` expression index, fused by RRF. `'simple'` is
   deliberate: Postgres ships no Bengali configuration, and a stemmer that
   guessed would be worse than none. The stub-vector arm is down-weighted to 0.2
   rather than dropped, so behaviour is continuous across the §3.1 flip.

   Two defects in the first implementation, both found by pointing the eval set
   at it and both mine:

   **The full-text arm never fired.** `plainto_tsquery` conjoins every term, so
   an eight-token question required all eight words in one caption. Across the
   32-query set it matched **zero rows** — the arm was dead, the GIN index built
   for it was never touched, and every figure credited to "full-text + trigram"
   was trigram carrying it alone. The failure is silent by construction: a
   predicate matching nothing returns an empty arm, which looks exactly like "no
   lexical match for this query". `to_tsquery('a | b | c')` matches any term and
   lets `ts_rank` discriminate. Lexical recall@10: 0.375 → 0.750.

   **The two lexical signals were merged before fusion.** They shared one arm via
   `GREATEST(ts_rank, similarity)` — a max across two scales that have nothing to
   do with each other, which is exactly the comparison RRF exists to remove.
   Splitting them into independent arms, with no other change, raised MRR@10 from
   0.551 to 0.648 and nDCG from 0.631 to 0.704.

   `semantic_search` now fuses **three** arms: dense (chunk or post vectors),
   `fts`, and `trgm`. `matched_by` names which of them found each row. A
   down-weighted `trgm` (0.5) scored slightly higher still (MRR 0.664) but was
   not adopted — that is a hyperparameter tuned on 32 derived queries, and the
   unweighted version is defensible without it.

6. **§3.4 done, off by default.** `libs/retrieval.rerank` degrades to
   first-stage order and *reports that it did* — returning the fused order as
   though it had been reranked would let a precision claim be published for a
   pass that never ran.

7. **§3.2 / §5.3 done.** `comment_embeddings` (keyed `(post_id, comment_id)`,
   HNSW, with `represented_by` for near-duplicates that share a vector), written
   in the same transaction as the analysis row, one encoder call per thread.
   The 10,272-comment corpus backfilled to 8,415 vectors from 5,628 encoder
   inputs — near-duplicate grouping removed a third of the work.

   The runner guards moved first, as §5.3 required. `_comment_tools` is now
   distinct from `_per_post_tools`, so a run that reads comments via
   `search_comments` is not shoved back through the second-hop prompt or flagged
   for uncorroborated statistics. `_CITED_COMMENT_ID_RE` closes the fabrication
   hole — every previous pattern was anchored on the words "post id". And
   because a comment id is a CUID in this corpus, ids are now separated by the
   *field* they arrived in rather than by shape, so comment ids stop landing in
   `run.citations`, which the UI renders as post links.

8. **§3.6 / §3.7 done.** The cluster scan cap is configurable *and disclosed on
   every cluster row* — raising it alone would only move the number at which the
   tool quietly lies. `semantic_search` takes `from_date`/`to_date` via the same
   `_date_filter` the stance tools use. `embedding_model` / `embedding_dim` are
   written on every row, and `coverage_stats` warns when the index holds more
   than one vector space.

9. **§3.5 chunking — done.** `post_chunks` keyed `(post_id, chunk_idx)`, written
   in the same transaction as the analysis row and replaced wholesale on
   re-analysis (an upsert would strand surplus tail rows describing text the
   post no longer has). The splitter is Bangla-first: `।` is the sentence
   terminator for most of this corpus, and a splitter that knew only `.!?` would
   see a 3,000-character Bangla post as one unsplittable sentence and fall
   through to a hard character cut. Overlap is dropped rather than allowed to
   push a chunk past the encoder's window — a test pins that bound.

   The unit of *retrieval* changed; the unit of *citation* deliberately did not.
   A result row gains `matched_chunk` and `chunk_idx`, and the shared agent
   directive says to quote the passage but cite the `post_id`, because
   `chunk_idx` is an internal offset no reader can look up. That is also what
   keeps the fabrication guards working unchanged.

10. **§4 persisted cluster labels — done.** `cluster_labels` stores a centroid
    and a name. Matching is by centroid cosine distance, never by `cluster_id`:
    k-means renumbers ids on every run, so "cluster 3" this week and last week
    are unrelated groups — which is why themes could not be tracked over time
    and why the narrative agent had nothing stable to name. A distance floor is
    enforced so a genuinely new narrative cannot silently inherit the nearest old
    label, which would read as continuity that was never observed. `get_clusters`
    emits `label` only when a stored centroid is close enough; the prompt tells
    the agent to fall back to `representative_summary` otherwise.

### Section 7 — defects found by auditing §0–§6 against the code

Everything above was written alongside the implementation. This section is the
result of reading it back afterwards and testing the claims rather than the
happy path. Seven things did not survive that.

1. **A failed enrichment write silently destroyed the whole post.** The worst of
   them, and invisible to every existing test. `persist_postgres` opens one
   connection-level transaction and binds a Session to it, so the Session *joins*
   that transaction. Postgres aborts an entire transaction on any statement
   error — so the chunk and comment-vector paths, which catch their exceptions
   and return 0 on the stated grounds that "a post whose thread could not be
   embedded is still a correctly analysed post", left the transaction poisoned.
   The outer commit became a rollback and took the analysis row and the chunks
   with it. `persist_postgres` then returned normally and logged
   `postgres: upserted`, so nothing upstream could tell. The swallow did exactly
   what its own comment said it existed to prevent: it traded the canonical
   result for the index.

   `session.rollback()` in the handler is **not** the fix — in this join mode it
   unwinds the whole joined transaction, which is the same loss by a shorter
   route. Each enrichment now runs in an explicit `session.begin_nested()`
   SAVEPOINT, which confines a rollback to that write, and the two repo methods
   no longer `commit()` (a commit releases the savepoint and gives the isolation
   away). A parametrised test injects a foreign-key violation into each of the
   two paths and asserts the analysis row survives. Only a *database* failure
   ever had this effect, which is why a Python-level encoder failure — the case
   that gets exercised — always looked fine.

2. **The per-post comment cap was the silent truncation §3.6 was about.** And it
   was not hypothetical: `COMMENT_EMBEDDING_MAX_PER_POST=1000` accounts for
   *exactly* the corpus's comment-vector shortfall — 10,272 comments, 8,415
   vectors, and one post whose thread runs 1,857 comments past the cap. Those
   comments are stored and labelled but carry no vector, so `search_comments`
   cannot reach them, and `comment_vector_coverage: 0.8192` read as an encoder
   shortfall. `coverage_stats` now derives and discloses
   `posts_over_comment_cap` / `comments_dropped_by_cap`, the write path logs the
   truncation at warning rather than debug, and the `quality` prompt is told to
   report it as a retrieval blind spot distinct from a fault.

   **Then the cap itself was removed (18 Aug 2026).** Disclosure was the right
   first move, but the honest disclosure of an 82% index is still an 82% index,
   and the 1,857 hidden comments were the tail of the single most-discussed post
   in the corpus — the last place a blind spot is acceptable. The default is now
   `COMMENT_EMBEDDING_MAX_PER_POST=0`, which matches `ROUTER_COMMENT_TOP_N=0` and
   makes comment-vector coverage a question about the *encoder* again rather than
   about a knob. `posts_over_comment_cap` / `comments_dropped_by_cap` stay: the
   cap can still be set, and if it is, it must still say so.

3. **`score` was an RRF score presented as relevance.** A top hit scores
   `1/(60+1) = 0.0164` no matter how good it is, and nothing said so — an LLM
   handed `score: 0.0164` can reasonably describe the best match in the corpus as
   a 1.6% one. Result rows now also carry `vector_similarity`, the real cosine in
   [-1, 1] (0.32 and 0.52 on the two probes above), absent when only the lexical
   arms matched. Both tool docstrings now state that `score` orders results and
   is not a relevance figure.

4. **`search_comments` still merged its two lexical signals with `GREATEST()`** —
   the precise defect step 5 above reports fixing on the post side, where
   splitting the pair was worth 0.10 of MRR@10. Comment search now fuses `term`
   (the comment contains your words, ranked by how many) and `trgm` (fuzzy
   spelling) as separate arms, named individually in `matched_by`. Applied for
   consistency of mechanism and *not* on a measurement: there is no
   comment-level gold set, and §3.8 only ever built a post-level one.

5. **`cluster_labels.embedding_model` was written and never read.** The column's
   own comment says "a centroid from a different embedding model is not
   comparable to this one, so matching must be able to exclude it", and
   `_match_persisted_labels` filtered on campaign and tenant only. A distance
   floor cannot catch this — a cosine distance across two vector spaces is
   computed, comparable-looking and meaningless. Now filtered on the active
   model, with NULL (pre-provenance rows) allowed through.

6. **The eval harness diverged from production in three ways**, so "it measures
   what the agents get" was overclaimed. It fused with no weights while
   `semantic_search` down-weights a stub query vector to 0.2 — a no-op with real
   embeddings, which is why the *real* columns stand, but it means the **stub**
   column of the table above was measuring a fusion the server does not run in
   stub mode. It defaulted to a pool of 50 against production's
   `candidate_pool(k)` = 40. And `_lexical_arm`, which only the harness calls,
   carried a docstring describing the `plainto_tsquery`-AND behaviour that step 5
   removed. All three corrected; **every figure in the table above re-runs
   unchanged** at the production pool and weights.

7. **Two stale cross-references**, both now fixed: §2's "Now" note still listed
   chunking among what "remains absent" and pointed at a §6 subsection called
   "Deliberately not done" that does not exist, and `.env.example` pointed at
   `deploy/backfill_embeddings.py` for the backfill, which is
   `deploy/backfill_embeddings.py`.

Also fixed, though not a code defect: the table above could not be reproduced at
all from a clean checkout. `DATABASE_URL` pointed at `localhost:5432`, where this
host runs its own Postgres, so `python -m eval.score_retrieval` failed with
`role "defense" does not exist` — and the compose Postgres published no host port
to point at instead. The documented workaround reached the container by IP, which
breaks on every `docker compose up`. The compose file now publishes **5433**
(minio already does the same on 9002 for the same collision), `.env` and
`.env.example` say so, and the harness prints that fix when it sees the error.

### Section 8 — second audit pass

Section 7 was written by auditing the *document*. This pass re-audited the code
with Section 7 itself treated as an unverified claim, and went at the parts the
first pass only skimmed: the eval builder, and the failure modes of the encoder
and of the filter arguments. Three more defects, one of which had been silently
corrupting measurements — including two of my own during this audit.

1. **`CUDA out of memory` degraded retrieval to hash vectors, silently and
   intermittently.** The most serious of the three, and the one that had already
   done damage. This GPU is a 4 GB card with ollama resident at ~2.6 GB, so the
   encoder's CUDA load fails depending on nothing but what the LLM is doing at
   that moment. `_get_model` caught the exception and returned `None`, which means
   every vector became a SHA-256 hash: `semantic_search` degraded to ranking
   noise, and it did so *transiently*, so the same query could be answered from
   real vectors once and from hashes a minute later.

   §3.1 anticipated the VRAM pressure — it is why `EMBEDDING_STUB_MODE` and the
   lighter `embeddings` extra exist — but the mitigation only governed which
   models load, not what happens when the one model retrieval needs cannot fit.
   Two ad-hoc analyses during this audit were computed on stub vectors and
   produced plausible, wrong numbers before the encoder assertion caught it.

   The loader now tries CPU before it gives up on hashes, and `EMBEDDING_DEVICE`
   makes CPU selectable outright (set to `cpu` in `.env` for this box). CPU is
   not a compromise here: **99 ms per query, 133 ms for a batch of 64, and
   bit-identical vectors** — the EN/BN cross-lingual probe scores 0.771 on either
   device. Losing 99 ms was never worth paying hash noise to avoid. Two tests pin
   it: a CUDA-OOM must reach CPU, and a total failure must still reach the stub
   path.

2. **A placeholder `post_id` silently widened `search_comments` to the corpus.**
   `post_id` is an optional filter, and the guard *dropped* it when the value
   contained "cuid"/"uuid" — so `post_id="<CUID>"` returned five comments from
   four different posts, presented as the comments on one post. `_ID_RE` does not
   catch these on its own: `post-uuid` and `cuid-of-the-post` are built from legal
   id characters.

   This is precisely the failure `_clean_campaign_id` already exists to prevent,
   one field over — its docstring even states the principle ("dropping the filter
   silently answers a corpus-wide question with a campaign-shaped label on it,
   which reads as correct and is not"). It also inverted the incentive an agent
   learns from: a correctly scoped call can legitimately return zero rows, while
   the placeholder call always returned something. `_clean_post_id` now raises,
   matching the campaign contract, and tells the model to fetch a real id.

3. **The chunk arm applied `sentiment`/date filters after its inner kNN limit.**
   The inner CTE takes the `chunk_pool` nearest chunks corpus-wide and the outer
   join then filters them, so a selective filter deletes most of the candidates
   rather than narrowing the search. With `sentiment='neutral'` (4 posts of 50)
   the chunk arm returned **0** rows where the post-level arm returned 4.

   Total loss was masked by the caller's fallback to `_vector_arm`; partial loss
   was not masked at all, and begins as soon as the corpus exceeds `chunk_pool`
   chunks — 120 at the default limit, against **86 today**, i.e. about 17 posts of
   headroom. Filters are now pushed into the inner CTE, but only when one exists,
   so the unfiltered path keeps its original no-join shape and its HNSW plan. When
   a filter is present the join may cost the index: that is the standard
   filtered-ANN tradeoff, taken deliberately, on the grounds that a narrowed
   candidate set is when a scan is affordable.

#### What the eval actually measures, stratified

Not a defect, but it changes how the headline table should be read. §0 notes that
`lexical` outscoring `hybrid` on MRR is "an artefact of queries derived from each
post's own topics and entities, which favours literal matching". That is true and
substantially understates it. Splitting the 32 queries by the `lexical_overlap`
the builder already records:

| bucket | n | lexical | vector | chunked |
| :--- | ---: | ---: | ---: | ---: |
| overlap ≥ 0.5 | 12 | **1.000** | 0.583 | 1.000 |
| overlap < 0.5 | 20 | 0.600 | **0.650** | **0.800** |
| — of which overlap = 0 | 8 | **0.000** | **0.500** | 0.500 |

Twelve of the 32 queries are near-verbatim lookups where lexical cannot lose. On
the eight genuinely leak-free queries lexical scores **zero** and the dense arm
is the only thing that retrieves anything at all. So the aggregate table
*understates* the embeddings rather than flattering them, and the stratified view
is a much stronger argument for fusion than the headline numbers: `chunked` ties
lexical on the easy bucket and beats it 0.800 to 0.600 on the hard one. Neither
arm dominates — which is exactly §3.3's thesis, now with the evidence pointed at
the right place.

The caveats in §0 still stand, and one more belongs with them: n=8 in the bucket
that carries the argument. Paired McNemar tests over the full set put every
`lexical`-vs-`chunked` comparison above p=0.05 (k=1 p=0.375, k=3 p=1.000, k=10
p=0.125, the last from a clean 4–0 split). The direction is consistent; the set
is too small to call it. Reporting recall@1 alongside recall@10 would also help:
at k=1 the dense arm *displaces* correct top hits (lexical 19/32, chunked 16/32),
which the k=10-only table hides.

### Section 9 — the agent layer, once real retrieval was behind it

§5.5 said the tool-use loop and the fabrication guards could stay as they were.
That held right up until the retrieval got good: real embeddings over a Bengali
corpus return **large** results, and every failure below is the agent layer
meeting a payload the stub never produced. All are live-observed, not reasoned
about, and each has a regression test
([`test_agent_context_window.py`](../tests/test_agent_context_window.py),
[`test_agent_placeholder_post_ids.py`](../tests/test_agent_placeholder_post_ids.py)).

1. **The context window was 4,096 tokens and nothing said so.** `ollama serve`
   defaults to `num_ctx 4096` and *silently discards* a longer prompt, oldest
   message first, reporting only what it evaluated. The oldest messages are the
   system prompt and the operator's question. Measured: an 11k-token prompt
   evaluates **24** tokens on `llama3.1:8b` and all **11,045** on a derived
   `llama3.1:8b-16k` tag ([`config/Modelfile.llama31-16k`](../config/Modelfile.llama31-16k)),
   which is now the `AGENT_LOCAL_MODEL` default.

2. **Half the token bill was `\uXXXX` escaping.** `json.dumps` defaults to
   `ensure_ascii=True`, so every Bengali character cost six bytes. One
   `semantic_search(limit=50)` result — 32 rows — serialised to 76,666 characters
   (~39,000 tokens); the same rows at `ensure_ascii=False` are 23,194 characters
   (~18,600). Zero added information for half the context.

3. **A single result must not dominate the window.** Results are capped at 6,000
   characters, dropping **whole rows** (truncating mid-JSON hands the model
   malformed data), and the model is told what was withheld — a silently
   shortened list reads as a complete answer. Citations are extracted from what
   the model was *shown*, so a row past the cut cannot be credited as grounding.

4. **The question is re-appended after every round of tool results.** It is the
   second message, so it is the first casualty of an overflow; and even inside
   the window, a small model that has just read thousands of tokens of JSON
   answers the payload instead of the operator.

5. **Byte-identical repeats are not dispatched.** With the window fixed, the
   model stopped bailing early and started looping: one run spent 11 of 15 calls
   on identical `semantic_search(query="comment sentiment", campaign_id="all")`
   and shipped the budget-cap notice as the briefing. A repeat is now answered
   from the first result, with a note naming the tools the run has **not** tried;
   after two, the tools are withdrawn for the rest of the run.

6. **Two more non-answer shapes joined the two in §5.2.** *Self-narration* — "The
   original user question was not provided… the operator is attempting to
   retrieve data using the `top_posts` tool with an invalid metric", recorded
   `completed` with 32 citations attached — is conclusive on a single hit,
   because only a truncated context makes that claim. The *empty template* is
   the more dangerous: correct headings, correct columns, every cell `...`, and
   "the actual values will be filled in based on the data retrieved", written
   after 14 tool calls and 20,411 prompt tokens of real data.

7. **An honest escape hatch became an unconditional phrase.** Given a way to say
   "no post-level tool ran", an 8B model says it regardless. A run called
   `top_posts`, printed its ten real post_ids in the metrics table, and then
   wrote *"No trigger posts identified: no post-level data was retrieved"*. That
   is worse than the `[insert post ID]` placeholder it replaced — a placeholder
   is visibly unfinished; this is a confident false statement about the run. The
   guard is in code, checked against the run record rather than the answer's own
   table.

8. **An off-watchlist `target_id` returned `[]`, indistinguishable from silence.**
   `stance_over_time(target_id="primary_political_figures")` was briefed as "no
   stance data exists for the primary political figures" while the one tracked
   entity had rows in seven posts. The roster is an operator-owned file the model
   never sees and there is no tool to list it, so it invents an id — and an
   invented id looked exactly like an empty corpus. Unknown ids are now
   **rejected** the way `_clean_campaign_id` rejects a placeholder, with every
   valid id named in the error, so the correction costs one turn. Ids, display
   names and aliases (Bangla and Banglish included, because that is what the
   corpus contains) all resolve. A *tracked* target with no rows still returns
   empty — that is a real answer, and it is the one the docstrings promise.

### Still not done

**§4's remaining candidates.** Entity/target-profile vectors, CLIP image vectors,
narrative/claim-level vectors, chat-history vectors, author vectors, and an
external document corpus. None has a live consumer in the way chunk vectors and
cluster labels did; the external corpus in particular is a new tool, a new
citation shape and probably a new agent (§5.1 rates it "Yes — large").

**Nothing writes `cluster_labels` yet.** The read path, the centroid matching and
the prompt handling are in place and degrade correctly to "no label", but the
LLM pass that *names* a cluster and stores it has not been built. Until it is,
the table stays empty and the narrative agent uses `representative_summary` —
which is the §5.4 prompt fix, still doing its job.

**`reranked` is unmeasured.** Implemented and honest about not running; it needs
the cross-encoder weights downloaded.

### Schema migration

`deploy/init-db.sql` is idempotent — re-running it against an existing database
adds the new columns and the `post_chunks`, `comment_embeddings` and
`cluster_labels` tables, plus `pg_trgm` and the lexical indexes, without touching
data:

```bash
docker exec -i deploy-postgres-1 psql -U defense -d defense < deploy/init-db.sql
python deploy/backfill_embeddings.py        # posts, chunks and comments
```

**Reaching Postgres from the host.** The compose Postgres publishes on
**5433**, not 5432, because this host runs its own Postgres on 5432 — and that
one has no `defense` role, so a host tool pointed at 5432 fails with
`role "defense" does not exist`, an error that names neither the port nor the
cause. Anything running as a compose service keeps using `postgres:5432`; host
tools (`eval/score_retrieval.py`, `deploy/backfill_embeddings.py`, psql) want:

```bash
DATABASE_URL=postgresql://defense:defense@localhost:5433/defense
```

This replaces an earlier workaround that reached the container by IP
(`docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}'
deploy-postgres-1`), which broke on every `docker compose up`. Publishing a
non-conflicting host port is the same treatment minio already gets on 9002.

# Search — Identifiers, Keyword, Semantic, Hybrid

> **Scope.** The user-facing search surface (`GET /v1/search`) and the retrieval
> primitives it shares with the agents' `semantic_search` tool: the identifier
> short-circuit, the three arms, reciprocal-rank fusion, and the optional
> reranker. Includes the **measured** recall@k numbers, which are the only
> accuracy figures anywhere in this project.
>
> Code: [`api/routers/search.py`](../src/defense/services/api/routers/search.py)
> (the endpoint), [`libs/retrieval.py`](../src/defense/libs/retrieval.py) (terms,
> RRF, rerank), [`mcp_servers/retrieval_mcp/server.py`](../src/defense/mcp_servers/retrieval_mcp/server.py)
> (the arms the agents call). Storage, chunking and vector versioning are
> [RAG_STATE_AND_ROADMAP.md](RAG_STATE_AND_ROADMAP.md); the evaluation harness is
> [evaluation.md](evaluation.md) §8.

---

## 1. The endpoint

```
GET /v1/search?q=…&mode=keyword|semantic|hybrid&semantic=true|false&campaign_id=…&limit=20
```

`mode` overrides the older boolean `semantic` when both are given. `mode` is
**not** the default, because `semantic` is the existing contract and silently
changing what a caller's query returns is worse than making them ask.

Results are tenant-scoped and carry a `match_type` saying **how** each hit was
found — `exact_id`, `id_prefix`, or the search mode.

## 2. An identifier is looked up, not searched for

Every post carries three identifiers — the upstream CUID (`post_id`), the
platform's own numeric id, and its URL — and pasting one of them into a search box
is a lookup, not a similarity question.

The resolution order is deliberate:

1. **Exact identifier match, in *every* mode, unconditionally.** No shape test
   gates it, so a query that happens to be an id can never be answered by cosine
   distance instead.
2. **Identifier prefix**, when the query *looks* like an id but matches nothing
   exactly. This is the case the UI creates by rendering 8 characters of a
   25-character id.
3. Otherwise the requested search mode.

## 3. Three arms

| Arm | Mechanism | Good at |
| --- | --------- | ------- |
| **Vector** | pgvector cosine over `analysis_results.embedding` (768-dim multilingual encoder) | Paraphrase, cross-language, "posts about X" |
| **Chunk** | Same, over `post_chunks` — a Bangla-aware splitter | Long posts where the relevant passage is not the opening |
| **Lexical** | Postgres full-text (`tsvector`) **fused with** trigram similarity | Exact entity names, transliterations, hashtags |

Keyword mode is a case-insensitive JSONB text scan across
`analysis_results.result` — matching post summary, caption, topics, keywords and
comment themes.

**Semantic quality requires real embeddings** (`MODEL_STUB_MODE=false` plus
`uv sync --extra ml`). In stub mode vectors are deterministic hashes, so results
are *stable but not meaning-based* — and measurably indistinguishable from chance
(§6).

## 4. Fusion, not a mode switch

`GET /v1/search?semantic=true|false` used to make the **caller** pick, and the
`semantic_search` MCP tool the agents actually use had no lexical mode at all. On
this corpus that is the wrong place to put the choice: it is code-mixed
Bangla / English / Banglish, where exact entity names, transliterations and
hashtags are precisely what a multilingual sentence encoder blurs and precisely
what a lexical match nails. Neither arm dominates, so the system runs both.

**Reciprocal rank fusion**, not score normalisation, because the two arms produce
numbers that are not comparable and never will be — one is a cosine similarity in
[-1, 1], the other a trigram similarity or a `tsvector` rank with its own
arbitrary scale. RRF throws the scores away and keeps only the ranks:

```
score(d) = Σ  1 / (k + rank_i(d))        k = 60
```

A document both arms rank highly beats a document either arm ranks first alone,
which is the behaviour wanted here.

RRF is also why hybrid retrieval is worth switching on *before* real embeddings
are: with hash vectors the vector arm contributes noise at every rank and the
lexical arm still finds the post, so fusion degrades to "lexical, with some noise
mixed in" where pure kNN degrades to "noise".

**Reranking** (`RETRIEVAL_RERANK=true`) adds an optional cross-encoder pass over
the fused candidates. Implemented and **unmeasured** — it needs the cross-encoder
weights, and the harness prints `skipped` rather than reporting the fused order as
though a rerank had happened.

## 5. Comment search

`search_comments` (agents) and the comment path here run their own three arms over
`comment_embeddings` — vector, term and trigram — because the signal in this
corpus lives in the threads, not the captions. Coverage caveat:
`COMMENT_EMBEDDING_MAX_PER_POST` now defaults to `0` (uncapped), but a positive
value is the *quietest* cap in the system — those comments are still stored and
still labelled, they simply have **no vector**, so comment search cannot reach
them and the only symptom is an unexplained dip in `comment_vector_coverage`. The
old default of `1000` hid **1,857 comments, 18% of the corpus** — the tail of the
single most-discussed thread.

## 6. Measured: recall@k

`python -m eval.score_retrieval` over **32 known-item queries**, k=10, on the live
50-post corpus. The same set was run before and after real embeddings were turned
on, so the two recall columns differ only in whether the vectors are hashes.

| config | recall@10 (stub) | recall@10 (real) | MRR@10 (real) | nDCG@10 (real) |
| :--- | ---: | ---: | ---: | ---: |
| `vector` | 0.1875 | 0.6250 | 0.2869 | 0.3667 |
| `lexical` (fts + trgm, fused) | 0.3750 | 0.7500 | 0.6503 | 0.6748 |
| `chunk` | — | 0.6562 | 0.3014 | 0.3846 |
| `hybrid` | 0.5312 | 0.8750 | 0.6108 | 0.6759 |
| `chunked` = chunk + fts + trgm | — | **0.8750** | **0.6482** | **0.7041** |

Four readings, each of which is a claim this table converts from argument to
number:

- **Stub vectors were indistinguishable from chance.** 0.1875 recall@10, where
  returning 10 random posts out of 50 scores 0.20.
- **Real embeddings are worth 3.3× on the vector arm** (0.1875 → 0.6250), and the
  end-to-end configuration went 0.5312 → 0.8750 — a 65% relative gain.
- **The lexical column moved too, and that was a bug, not a benefit.** `lexical`
  went 0.3750 → 0.7500 with no vector involved, because the full-text half had
  never fired: `plainto_tsquery` ANDs every term, so an eight-token question
  demanded all eight words in one caption and matched **zero** rows across the
  whole set. Everything previously credited to "full-text + trigram" was trigram
  alone, and the GIN index built for FTS was dead weight.
- **Chunking is real but small.** It was +0.031 recall over `hybrid` while the
  lexical arm was weak; with the arm fixed both reach 0.8750 and the remaining
  gain shows up in *ranking* (MRR 0.6108 → 0.6482, nDCG 0.6759 → 0.7041).
  Expected: only 14 of 50 posts exceed 600 characters, so 50 posts yielded 86
  chunks and most are a single chunk identical to the caption.

### How to read these numbers honestly

- **They are lower bounds.** The set marks **one** relevant post per query, so
  every other on-topic post counts as a miss.
- **They compare retrievers on one set** — they are not absolute quality figures.
- **`lexical` alone scores a higher MRR than `hybrid`**, which is an artefact of
  queries derived from each post's own topics and entities: that favours literal
  matching. `hybrid` wins on recall@k, which is what matters when the whole top-k
  is handed to an LLM.
- **The set is not human relevance judgement.** `human_verified_count` is 0; each
  query was derived from its target post's own analysis. A human can upgrade a row
  by adding ids to `relevant_post_ids` and setting `human_verified`.
- **The stub column has one extra caveat**: it was measured before the harness
  applied the 0.2 down-weight `semantic_search` puts on a hash-stub query vector,
  so it reflects an unweighted fusion the server does not run in stub mode. The
  `real` columns are unaffected — the weight is 1.0 either way.

Rebuild the set with `python -m eval.build_retrieval_set`; score with
`python -m eval.score_retrieval`.

## 7. Configuration

| Env var | Effect |
| ------- | ------ |
| `MODEL_STUB_MODE` / `EMBEDDING_STUB_MODE` / `EMBEDDING_ALLOW_STUB` | Whether vectors are real; whether the encoder refuses rather than degrading |
| `EMBEDDING_MODEL` / `EMBEDDING_DIM` | The encoder and its width — must match the pgvector column |
| `RETRIEVAL_RERANK` | Enable the cross-encoder pass |
| `RETRIEVAL_CLUSTER_SCAN_CAP` | How many posts `get_clusters` may pull vectors for (2000); the cap is disclosed on every row |
| `COMMENT_EMBEDDINGS_ENABLED` / `COMMENT_EMBEDDING_MAX_PER_POST` | Comment vectors and their (now uncapped) ceiling |

Full list: [env.example.md](env.example.md).

## 8. Evidence class

| Claim | State |
| ----- | ----- |
| Identifier lookup precedes similarity in every mode | ✅ Measured |
| recall@10 / MRR@10 / nDCG@10 per configuration | ✅ **Measured** — `eval/score_retrieval.py`, 32 queries |
| Real embeddings beat hash vectors 3.3× on the vector arm | ✅ **Measured** |
| Hybrid beats either arm alone on recall | ✅ **Measured** |
| The FTS arm now fires | ✅ Measured (it did not before) |
| Chunking helps ranking, not recall, on this corpus | ✅ Measured |
| Reranking | ⚠️ **Unmeasured** — needs cross-encoder weights; harness prints `skipped` |
| Human relevance judgement | 📋 **None** — `human_verified_count = 0` |

Cross-references: [RAG_STATE_AND_ROADMAP.md](RAG_STATE_AND_ROADMAP.md) (storage,
chunking, vector versioning, the audit sections) ·
[MCP_SERVERS.md](MCP_SERVERS.md) (the tools agents call) ·
[AGENTS.md](AGENTS.md) · [evaluation.md](evaluation.md) ·
[endpoints.md](endpoints.md).

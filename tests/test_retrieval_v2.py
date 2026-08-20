"""Regression tests for the RAG_STATE_AND_ROADMAP work: hybrid retrieval,
comment vectors, vector versioning, and the fabrication guards that had to move
before comment search could ship.

Each test below corresponds to a defect the roadmap named, and is written to
FAIL on the pre-change code rather than merely to exercise the new code.
"""

from __future__ import annotations

import pytest

from defense.libs import chunking as ch
from defense.libs import retrieval as rt
from defense.libs.embeddings import (
    EMBEDDING_DIM,
    STUB_MODEL_NAME,
    active_model_name,
    embed_texts_with_provenance,
    stub_embedding,
)
from defense.services.agents import runner as R
from defense.services.agents.registry import (
    AGENT_REGISTRY,
    NARRATIVE_SYSTEM_PROMPT,
    QUALITY_SYSTEM_PROMPT,
)
from defense.services.workers.assembler import persistence as pers


# ---------------------------------------------------------------------------
# §3.3 — reciprocal rank fusion
# ---------------------------------------------------------------------------


def test_rrf_promotes_a_document_both_arms_found():
    """The whole point of fusing: agreement beats either arm's top hit alone."""
    fused = rt.fuse({"vector": ["a", "b", "c"], "lexical": ["b", "x"]})
    assert fused[0][0] == "b"
    assert fused[0][2] == ["vector", "lexical"]


def test_rrf_does_not_penalise_a_short_arm():
    """An arm returning 2 rows must not drag down documents it never saw.

    This is why RRF is used instead of score normalisation or a worst-rank
    placeholder: the lexical arm routinely returns a handful of rows where the
    kNN returns forty, and both have to be fusible without either being padded.
    """
    long_arm = [f"d{i}" for i in range(40)]
    fused = {doc: score for doc, score, _ in rt.fuse({"v": long_arm, "l": ["d39"]})}
    # d39 is LAST in the long arm but first in the short one, and that is enough
    # to beat documents the short arm never returned.
    assert fused["d39"] > fused["d1"]


def test_rrf_weights_can_discount_a_meaningless_arm():
    """A stub-vector arm is down-weighted, not deleted: it still contributes
    candidates the other arm missed, but it cannot outvote it."""
    unweighted = {d: s for d, s, _ in rt.fuse({"v": ["x"], "l": ["y"]})}
    assert unweighted["x"] == pytest.approx(unweighted["y"])

    weighted = {d: s for d, s, _ in rt.fuse({"v": ["x"], "l": ["y"]}, weights={"v": 0.2})}
    assert weighted["y"] > weighted["x"] > 0


class _CapturingSession:
    """Records the SQL and bound parameters an arm would execute."""

    def __init__(self):
        self.statements: list[tuple[str, dict]] = []

    async def execute(self, sql, params=None):
        self.statements.append((str(sql), dict(params or {})))

        class _R:
            def mappings(self_inner):
                return self_inner

            def all(self_inner):
                return []

        return _R()


def _captured(arm_coro_fn, query="what are people saying about fuel price increases"):
    import asyncio as _aio

    from defense.mcp_servers.retrieval_mcp import server as srv

    session = _CapturingSession()
    _aio.run(arm_coro_fn(srv, session, query))
    assert session.statements, "the arm executed no statement"
    return session.statements[0]


def test_fts_query_uses_or_not_and():
    """The bug that made the full-text arm dead for the entire corpus.

    `plainto_tsquery` conjoins every term, so an eight-token operator question
    demanded that all eight words appear in one caption. Across the 32-query
    evaluation set that predicate matched ZERO rows — the FTS half of hybrid
    retrieval never fired, the GIN index built for it was never used, and every
    number attributed to "full-text + trigram" was trigram working alone.
    Switching to `to_tsquery('a | b | c')` took lexical recall@10 from 0.375 to
    0.750 and hybrid from 0.719 to 0.875.

    The failure mode is silent by construction — a predicate matching nothing
    returns an empty arm, which is indistinguishable from "no lexical match for
    this query" — so this asserts on the statement rather than on results.
    """
    sql, params = _captured(
        lambda srv, s, q: srv._fts_arm(s, q, ["1=1"], {}, 10)
    )
    assert "to_tsquery" in sql
    assert "plainto_tsquery" not in sql, (
        "plainto_tsquery ANDs every term — it matched nothing on the whole corpus"
    )
    # ...and the terms really are OR'd, not merely passed to a different function.
    assert "|" in params["or_query"]


def test_fts_and_trigram_are_separate_fusion_arms():
    """They must not be merged with GREATEST() before fusion.

    ts_rank and trigram similarity live on unrelated scales, so taking the max
    of them is the scale comparison RRF exists to eliminate. Merging cost 0.10
    of MRR@10 against fusing them as independent arms.
    """
    fts_sql, _ = _captured(lambda srv, s, q: srv._fts_arm(s, q, ["1=1"], {}, 10))
    trgm_sql, trgm_params = _captured(
        lambda srv, s, q: srv._trgm_arm(s, q, ["1=1"], {}, 10)
    )
    assert "GREATEST" not in fts_sql and "GREATEST" not in trgm_sql
    # Each arm computes exactly one signal.
    assert "similarity(" not in fts_sql
    assert "ts_rank" not in trgm_sql
    assert "|" not in trgm_params["probe"], "trigram takes the phrase, not a tsquery"


def test_lexical_terms_keeps_bengali_and_drops_stopwords():
    """A Bangla query must not be tokenised down to nothing.

    The corpus is majority Bengali; a term extractor built on \\w with an ASCII
    assumption would hand the lexical arm an empty probe and silently reduce
    hybrid search to vector-only on most of the corpus.
    """
    terms = rt.lexical_terms("what are the comments about আওয়ামী লীগ")
    assert "আওয়ামী" in terms
    assert "লীগ" in terms
    assert "the" not in terms and "are" not in terms


def test_candidate_pool_over_fetches():
    """Fusion and reranking can only reorder what was fetched: if each arm
    returns exactly `limit` rows, nothing ranked limit+1 can be promoted."""
    assert rt.candidate_pool(10) > 10


def test_rerank_reports_when_it_did_not_run():
    """Returning first-stage order as though it were reranked would let a
    caller publish a precision claim for a pass that never happened."""
    rows = [{"post_id": "a", "text": "hello"}]
    out, was_reranked = rt.rerank("query", rows, text_key="text")
    # RETRIEVAL_RERANK defaults to false, so the pass is skipped...
    assert was_reranked is False
    # ...and the rows come back untouched rather than dropped.
    assert [r["post_id"] for r in out] == ["a"]


# ---------------------------------------------------------------------------
# §3.5 — chunking
# ---------------------------------------------------------------------------


def test_short_post_is_exactly_one_chunk_holding_everything():
    """The no-op case, and it is load-bearing.

    Median caption in this corpus is 166 characters. If a short post produced no
    chunk row, chunk retrieval would need a fallback path for most of the corpus
    and "no chunk matched" would be ambiguous between "did not match" and "was
    never chunked". One chunk holding the whole caption removes both problems,
    and its vector is identical to the post-level one.
    """
    text = "ছোট পোস্ট। এখানে কিছু নেই।"
    chunks = ch.chunk_text(text)
    assert len(chunks) == 1
    assert chunks[0].text == text
    assert chunks[0].idx == 0


def test_long_bangla_post_splits_on_the_dari():
    """`।` is the sentence terminator for most of this corpus.

    A splitter that knew only `.!?` would see a 3,000-character Bangla post as
    one unsplittable sentence, fall through to the hard character cut, and chop
    mid-word — producing exactly the mush chunking exists to avoid.
    """
    sentence = "এই সিদ্ধান্তটি সম্পূর্ণ ভুল এবং অন্যায় বলে আমি মনে করি।"
    chunks = ch.chunk_text(" ".join([sentence] * 60))
    assert len(chunks) > 1
    # Every chunk ends on a sentence boundary, not mid-word.
    assert all(c.text.rstrip().endswith("।") for c in chunks)
    assert all(len(c.text) <= ch.DEFAULT_TARGET_CHARS + 40 for c in chunks)


def test_runon_text_with_no_boundaries_still_splits():
    """Scraped captions really do contain 2,000 characters with no punctuation."""
    source = "ক" * 2000
    chunks = ch.chunk_text(source)
    assert len(chunks) > 1
    # Nothing is dropped: the pieces cover at least the whole source. They cover
    # MORE than it, because consecutive chunks overlap on purpose — so this is a
    # coverage assertion, not a reconstruction one.
    assert sum(len(c.text) for c in chunks) >= len(source)
    assert chunks[0].text.startswith("ক")


def test_no_chunk_exceeds_the_target_even_with_overlap():
    """The bound the encoder cares about.

    Carrying an overlap tail onto a full-sized piece used to produce chunks of
    `target + overlap`, past the window the sentence encoder truncates at — so
    the tail added to preserve context was silently clipped off at encode time,
    along with whatever else did not fit.
    """
    for source in (
        "ক" * 2000,
        " ".join(f"শব্দ{i}" for i in range(400)),
        " ".join(["এই বাক্যটি যথেষ্ট দীর্ঘ এবং এটি বারবার পুনরাবৃত্তি হচ্ছে।"] * 40),
    ):
        for c in ch.chunk_text(source):
            assert len(c.text) <= ch.DEFAULT_TARGET_CHARS, (
                f"chunk of {len(c.text)} exceeds target {ch.DEFAULT_TARGET_CHARS}"
            )


def test_chunk_offsets_are_monotonic_despite_overlap():
    """Overlap must not make chunk N+1 appear to start before chunk N."""
    body = " ".join(f"বাক্য সংখ্যা {i} এখানে কিছু অতিরিক্ত শব্দ আছে।" for i in range(80))
    chunks = ch.chunk_text(body)
    assert len(chunks) > 2
    starts = [c.start for c in chunks]
    assert starts == sorted(starts)
    assert [c.idx for c in chunks] == list(range(len(chunks)))


def test_empty_text_produces_no_chunks():
    assert ch.chunk_text("") == []
    assert ch.chunk_text("   \n  ") == []


# ---------------------------------------------------------------------------
# §3.1 — EMBEDDING_ALLOW_STUB must actually refuse a stub
# ---------------------------------------------------------------------------


def test_allow_stub_false_refuses_a_correctly_sized_stub(monkeypatch):
    """The gap that made the switch weaker than it reads.

    The refusal used to sit only on the "no usable vector" path, so it fired
    when Stage 1 sent NOTHING and stayed silent for the case the flag exists
    for: a correctly-sized, correctly-flagged hash stub — which is what every
    post produces in the default configuration. An operator who set the flag to
    keep noise out of the index got a full index of noise and no error.
    """
    monkeypatch.setattr(pers, "_ALLOW_STUB_EMBEDDING", False)
    vec = stub_embedding("some caption")
    assert len(vec) == EMBEDDING_DIM  # indistinguishable by shape — the whole trap

    with pytest.raises(pers.StubEmbeddingRefused):
        pers._resolve_embedding(vec, "p1", True)


def test_allow_stub_false_still_accepts_a_real_vector(monkeypatch):
    monkeypatch.setattr(pers, "_ALLOW_STUB_EMBEDDING", False)
    vec, is_stub = pers._resolve_embedding([0.01] * EMBEDDING_DIM, "p1", False)
    assert is_stub is False and len(vec) == EMBEDDING_DIM


# ---------------------------------------------------------------------------
# §3.2 / §3.5 — an enrichment failure must not take the analysis row with it
# ---------------------------------------------------------------------------


_PERSIST_RESULT = {
    "post_id": "savepointpost",
    "campaign_id": "spc",
    "tenant_id": "default",
    # Long enough to produce more than one chunk, so the chunk path really runs.
    "post_text": "x " * 500,
    "post_summary": "savepoint probe",
    "processing": {"schema_version": "1"},
    "comment_analysis": {"comments": [{"id": "spcomment1", "text": "a comment"}]},
}


async def _persist_counts(engine) -> tuple[int, int, int]:
    from sqlalchemy import text as sql_text

    out: list[int] = []
    async with engine.begin() as conn:
        for table in ("analysis_results", "post_chunks", "comment_embeddings"):
            out.append(
                (
                    await conn.execute(
                        sql_text(
                            f"SELECT count(*) FROM {table} WHERE post_id = :pid"
                        ),
                        {"pid": _PERSIST_RESULT["post_id"]},
                    )
                ).scalar()
                or 0
            )
    return out[0], out[1], out[2]


def test_chunk_arm_pushes_selective_filters_into_the_inner_knn():
    """A sentiment/date filter must narrow the kNN, not post-filter its output.

    The inner CTE takes the `chunk_pool` nearest chunks corpus-wide; applying the
    filter only in the outer query then deletes most of them. Measured with
    sentiment='neutral' (4 posts of 50) the chunk arm returned 0 rows where the
    post-level arm returned 4. Total loss is masked by the caller's fallback to
    `_vector_arm`; partial loss is not, and starts once the corpus exceeds
    `chunk_pool` chunks.

    Asserted on the generated SQL rather than against a database, because the
    defect is structural — which query the filter lands in — and this corpus is
    small enough that the two forms happen to agree.
    """
    from defense.mcp_servers.retrieval_mcp import server as srv

    captured: list[str] = []

    class _Session:
        async def execute(self, stmt, params=None):
            captured.append(str(stmt))
            raise RuntimeError("stop after capturing SQL")

    async def _run(where_parts, params):
        captured.clear()
        await srv._chunk_arm(_Session(), "[0]", where_parts, params, 10)
        return captured[0]

    import asyncio

    sentiment = "ar.result->>'overall_sentiment' = :sentiment"
    filtered = asyncio.run(_run(["1=1", sentiment], {"sentiment": "neutral"}))

    # The filter must appear before the inner LIMIT, i.e. inside top_chunks.
    inner = filtered.split("),", 1)[0]
    assert sentiment in inner, (
        "sentiment filter is applied only after the inner kNN LIMIT — the arm "
        "will silently under-return for any selective filter"
    )
    assert "JOIN analysis_results" in inner

    # The unfiltered path keeps its original shape: no join, so the HNSW plan is
    # unchanged for the case agents actually hit.
    plain = asyncio.run(_run(["1=1"], {}))
    assert "JOIN analysis_results" not in plain.split("),", 1)[0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method, breaks",
    [
        ("upsert_comment_embeddings", "comment vectors"),
        ("replace_post_chunks", "chunks"),
    ],
)
async def test_enrichment_db_failure_does_not_discard_the_analysis_row(
    postgres_container, monkeypatch, method, breaks
):
    """The bug this pins was silent TOTAL loss of the post.

    `persist_postgres` binds a Session to one connection-level transaction, so
    the Session joins it rather than owning it. Postgres aborts the whole
    transaction on any statement error — so catching an enrichment's database
    error and returning left the transaction poisoned, the outer commit became a
    rollback, and the analysis row and chunks went with it. `persist_postgres`
    returned normally and logged `postgres: upserted` on the way out, so nothing
    upstream could tell.

    A plain `session.rollback()` in the handler does NOT fix it: in this join
    mode it unwinds the entire joined transaction, which is the same loss by a
    shorter route. Only a SAVEPOINT (`begin_nested`) confines the damage, which
    is what makes "enrichment failure is logged and swallowed" true.
    """
    from sqlalchemy import text as sql_text
    from sqlalchemy.ext.asyncio import create_async_engine

    from defense.libs.repos.posts import PostRepository

    # This test is about the transaction, not the stub policy; keep it
    # independent of whichever way EMBEDDING_ALLOW_STUB happens to be set.
    monkeypatch.setattr(pers, "_ALLOW_STUB_EMBEDDING", True)

    engine = create_async_engine(postgres_container)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                sql_text(
                    "INSERT INTO posts (id, campaign_id, tenant_id) "
                    "VALUES (:pid, 'spc', 'default') ON CONFLICT DO NOTHING"
                ),
                {"pid": _PERSIST_RESULT["post_id"]},
            )

        # A realistic DB-level failure: a foreign-key violation from inside the
        # enrichment write. A Python-level failure never poisoned the
        # transaction, which is why this went unnoticed.
        async def _boom(self, *args, **kwargs):
            await self._session.execute(
                sql_text(
                    "INSERT INTO comment_embeddings (post_id, comment_id) "
                    "VALUES ('no-such-post', 'x')"
                )
            )

        monkeypatch.setattr(PostRepository, method, _boom)

        await pers.persist_postgres(
            _PERSIST_RESULT, engine, embedding=[0.01] * EMBEDDING_DIM,
            embedding_is_stub=True,
        )

        analysis, chunks, cvecs = await _persist_counts(engine)
        assert analysis == 1, (
            f"a failed {breaks} write destroyed the analysis row — the canonical "
            "result was traded for the index, which is the one thing the "
            "enrichment paths swallow failures to avoid"
        )
        # The failed arm wrote nothing; the other one is unaffected.
        if method == "replace_post_chunks":
            assert chunks == 0 and cvecs == 1
        else:
            assert chunks == 2 and cvecs == 0
    finally:
        async with engine.begin() as conn:
            for table in ("comment_embeddings", "post_chunks", "analysis_results"):
                await conn.execute(
                    sql_text(f"DELETE FROM {table} WHERE post_id = :pid"),
                    {"pid": _PERSIST_RESULT["post_id"]},
                )
            await conn.execute(
                sql_text("DELETE FROM posts WHERE id = :pid"),
                {"pid": _PERSIST_RESULT["post_id"]},
            )
        await engine.dispose()


# ---------------------------------------------------------------------------
# §3.2 / §3.7 — batch embedding and vector versioning
# ---------------------------------------------------------------------------


def test_batch_embedding_matches_the_single_text_path():
    """A comment embedded in a batch must be the same vector as one embedded
    alone, or `represented_by` propagation would mean two different things."""
    texts = ["সুন্দর", "this is a comment", ""]
    vectors, is_stub = embed_texts_with_provenance(texts)
    assert len(vectors) == 3
    assert all(len(v) == EMBEDDING_DIM for v in vectors)
    if is_stub:
        assert vectors[0] == stub_embedding("সুন্দর")


class _FakeEncoder:
    """Stands in for SentenceTransformer, failing on whichever devices we say."""

    def __init__(self, name, device=None, fail_on=()):
        if device in fail_on or (device is None and None in fail_on):
            raise RuntimeError(
                "CUDA out of memory. Tried to allocate 20.00 MiB. GPU 0 has a "
                "total capacity of 3.68 GiB of which 24.31 MiB is free."
            )
        self.device = device

    def encode(self, texts, **kw):
        import numpy as np

        one = isinstance(texts, str)
        n = 1 if one else len(texts)
        out = np.ones((n, EMBEDDING_DIM), dtype="float32") / (EMBEDDING_DIM**0.5)
        return out[0] if one else out


def _install_fake_encoder(monkeypatch, fail_on):
    """Put a fake `sentence_transformers` in sys.modules and reset the cache."""
    import sys as _sys
    import types

    from defense.libs import embeddings as emb

    mod = types.ModuleType("sentence_transformers")
    mod.SentenceTransformer = lambda name, device=None: _FakeEncoder(
        name, device=device, fail_on=fail_on
    )
    monkeypatch.setitem(_sys.modules, "sentence_transformers", mod)
    monkeypatch.setattr(emb, "_model", None)
    monkeypatch.setattr(emb, "_model_failed", False)
    # Real mode, so _get_model actually tries to load something.
    monkeypatch.setattr(emb, "_stub_mode", lambda: False)
    return emb


def test_cuda_oom_falls_back_to_cpu_not_to_hashes(monkeypatch):
    """A GPU too full for the encoder must cost latency, not correctness.

    This project's GPU is a 4 GB card that normally already hosts the LLM, so
    the encoder's CUDA load fails with `CUDA out of memory` depending only on
    what ollama is doing at that moment. The loader used to catch that and return
    None, which silently turned every vector into a SHA-256 hash — retrieval
    degraded to ranking noise, transiently, so the same query could be answered
    from real vectors once and from hashes a minute later. CPU costs ~99 ms per
    query and produces identical vectors, so it must be tried first.
    """
    emb = _install_fake_encoder(monkeypatch, fail_on=(None, "cuda"))

    model = emb._get_model()
    assert model is not None, "fell back to hash stubs while CPU was available"
    assert model.device == "cpu"

    _vec, is_stub = emb.embed_text_with_provenance("fuel price protest")
    assert is_stub is False
    assert emb.active_model_name() != STUB_MODEL_NAME


def test_stub_fallback_still_happens_when_every_device_fails(monkeypatch):
    """The degradation path must survive — just not be reached prematurely."""
    emb = _install_fake_encoder(monkeypatch, fail_on=(None, "cuda", "cpu"))

    assert emb._get_model() is None
    _vec, is_stub = emb.embed_text_with_provenance("fuel price protest")
    assert is_stub is True
    assert emb.active_model_name() == STUB_MODEL_NAME


def test_placeholder_post_id_is_rejected_not_silently_widened():
    """A placeholder post_id must not turn a one-post question corpus-wide.

    `search_comments` took post_id as an optional filter and DROPPED it when the
    value contained "cuid"/"uuid", so `post_id="<CUID>"` returned comments from
    four different posts as though they were the comments on one. That is the
    failure `_clean_campaign_id` already exists to prevent, one field over — and
    it inverted the incentive: a correctly scoped call can legitimately return
    zero rows, while the placeholder call always returned something.
    """
    from defense.mcp_servers.retrieval_mcp import server as srv

    # Omitted or explicitly unspecified is the ONLY thing that widens the search.
    assert srv._clean_post_id(None) is None
    assert srv._clean_post_id("") is None
    assert srv._clean_post_id("  ") is None

    # A real CUID survives untouched.
    assert srv._clean_post_id("cmoxb5qi102l") == "cmoxb5qi102l"

    # Placeholders raise. "post-uuid" and "cuid-of-the-post" are made of legal
    # id characters, so the shape regex alone does not catch them.
    for junk in ("<CUID>", "post-uuid", "cuid-of-the-post", "POST_ID", "<placeholder>"):
        with pytest.raises(ValueError):
            srv._clean_post_id(junk)

    # 'all' is a wildcard for campaign_id but meaningless for a single post.
    with pytest.raises(ValueError):
        srv._clean_post_id("all")


def test_stub_vectors_are_recorded_under_a_model_sentinel():
    """'Which model produced this vector' and 'no model did' are different
    facts. NULL says the second; only a sentinel can say the first is a stub."""
    name = active_model_name()
    # In the default configuration there is no model, so the sentinel applies.
    if name == STUB_MODEL_NAME:
        assert name != "" and name is not None
    else:  # a real model is loaded — then it must NOT be the sentinel
        assert name != STUB_MODEL_NAME


# ---------------------------------------------------------------------------
# §5.3 — the runner guards that had to move before search_comments shipped
# ---------------------------------------------------------------------------


def _tool(name: str, required: list[str] | None = None) -> dict:
    return {
        "function": {
            "name": name,
            "parameters": {"required": required or []},
        }
    }


def test_search_comments_counts_as_having_read_a_comment():
    """`search_comments` returns comment text but takes no post_id, so the old
    "per-post tool" test classified it as a discovery tool.

    Left that way, a run that HAD retrieved comments would be shoved back
    through the second-hop prompt — a live run, asked a second time to go and
    read comments it had already read, refused outright and the briefing was
    lost — and every percentage in it would be flagged as uncorroborated.
    """
    tools = [
        _tool("top_posts"),
        _tool("search_comments"),
        _tool("get_thread", ["post_id"]),
    ]
    assert "search_comments" in R._comment_tools(tools)
    # ...but it is still not a per-post tool: the second-hop push works by
    # handing the model a real post_id, which this tool does not take.
    assert "search_comments" not in R._per_post_tools(tools)
    assert "get_thread" in R._per_post_tools(tools)


def test_fabricated_comment_id_is_flagged():
    """Every citation pattern was anchored on the words "post id", so a
    fabricated comment citation passed the guard unchallenged — the change that
    most improves grounding would have opened the largest hole in the checks."""
    answer = "The worst example is Comment ID: 998877, quoted above."
    tool_output = '[{"post_id": "cmoxb5qi102luv0toasaz5ofh", "comment_id": "cmoxbkz5c041iv0touw6fdbp8"}]'
    assert "998877" in R._unverified_citations(answer, tool_output)


def test_real_comment_id_is_not_flagged():
    answer = "See Comment ID: cmoxbkz5c041iv0touw6fdbp8 under post cmoxb5qi102luv0toasaz5ofh."
    tool_output = '[{"post_id": "cmoxb5qi102luv0toasaz5ofh", "comment_id": "cmoxbkz5c041iv0touw6fdbp8"}]'
    assert R._unverified_citations(answer, tool_output) == []


def test_comment_ids_do_not_become_post_citations():
    """In this corpus a comment id is a CUID — the same shape as a post id — so
    a regex over the serialised result cannot tell them apart. A tool whose
    entire result is comment rows would have filled `run.citations`, which the
    UI renders as post links, with ids that are not posts.
    """
    result = [
        {"comment_id": "cmcomment0000000000000001", "post_id": "cmpost00000000000000000001", "text": "hi"},
        {"comment_id": "cmcomment0000000000000002", "post_id": "cmpost00000000000000000001", "text": "ho"},
    ]
    posts, comments = R._ids_from_result(result, str(result))
    assert set(posts) == {"cmpost00000000000000000001"}
    assert set(comments) == {"cmcomment0000000000000001", "cmcomment0000000000000002"}


def test_id_extraction_falls_back_to_the_regex_for_unstructured_results():
    """A tool returning a bare string must still contribute citations."""
    posts, comments = R._ids_from_result("no ids here", "post cmpost00000000000000000001 was hot")
    assert posts == ["cmpost00000000000000000001"]
    assert comments == []


# ---------------------------------------------------------------------------
# §5.2 / §5.4 — the two prompt-level honesty fixes
# ---------------------------------------------------------------------------


def test_quality_prompt_asks_for_vector_provenance():
    """§5.2: real embeddings do not close this gap — an agent that says "the
    most semantically similar posts are…" is asserting something it cannot
    check unless it is told to look."""
    lowered = QUALITY_SYSTEM_PROMPT.lower()
    assert "stub_embedding" in lowered
    assert "embedding_models" in lowered
    assert "comment_vector_coverage" in lowered


def test_narrative_prompt_no_longer_asks_for_a_column_the_tool_lacks():
    """§5.4: the prompt asked for a "cluster name"; get_clusters returns no
    `name` field, so the agent invented one every run."""
    assert "cluster name" not in NARRATIVE_SYSTEM_PROMPT.lower()
    assert "representative_summary" in NARRATIVE_SYSTEM_PROMPT
    # ...and it must report the truncation rather than passing a sample off as
    # the corpus.
    assert "scan_truncated" in NARRATIVE_SYSTEM_PROMPT


def test_comment_search_is_allowlisted_where_the_roadmap_says():
    for name in ("toxicity", "analyst", "stance", "narrative"):
        assert "search_comments" in AGENT_REGISTRY[name].tools, name


def test_comment_search_agents_have_budget_for_it():
    """Comment search encourages more, narrower calls; a budget left at 10
    just moves the failure to a truncated analysis."""
    assert AGENT_REGISTRY["toxicity"].max_tool_calls > 10
    assert AGENT_REGISTRY["stance"].max_tool_calls > 10

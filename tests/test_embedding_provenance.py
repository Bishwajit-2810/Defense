"""Regression tests: `embedding_is_stub` must survive to the DATABASE COLUMN.

PROJECT_ASSESSMENT §13.2. This is §9.11's defect one layer down, and it survived
that fix for a reason worth stating: §9.11 corrected the place where the flag was
*noticed* (the assembler's trace frame) and not the place where it is *consumed*
(the `analysis_results.embedding_is_stub` column, which is what `/v1/search`
returns to a user).

The trap is that the stub is **indistinguishable by shape**:

    stub_embedding(text) -> a deterministic hash vector of EMBEDDING_DIM floats
    a real model         -> a semantic vector of EMBEDDING_DIM floats

`persistence._resolve_embedding` used to classify by dimension — "768 floats, so
this must be real" — which is true of a stub too. With `MODEL_STUB_MODE=true`,
the default, that recorded **every row in the corpus** as a genuine semantic
vector: the exact inverse of what the column exists to disclose.

So these tests assert the chain END TO END, producer to persisted parameter. A
per-hop test would pass on the broken code: Stage 1 was right, the trace frame
was right, and the column was wrong, because nobody checked that the two agreed.
"""

import asyncio
import sys

sys.path.insert(0, '/home/bk/code/defense/src/defense')
sys.path.insert(0, '/home/bk/code/defense/src/defense/libs')
sys.path.insert(0, '/home/bk/code/defense/src/defense/services/workers/stage1_nlp')
sys.path.insert(0, '/home/bk/code/defense/src/defense/services/workers/assembler')

import pytest

from libs.embeddings import EMBEDDING_DIM, stub_embedding
from services.workers.assembler import assembler as asm
from services.workers.assembler import persistence as pers
from services.workers.stage1_nlp import text_analyzer as ta

_CAPTION = "Election rally in Dhaka drew a huge crowd today"


def _stage1_stub_result() -> dict:
    """Stage-1 output in the DEFAULT (stub) configuration."""
    return asyncio.run(ta.analyze_text(_CAPTION, ta.ModelRegistry()))


def _canonical(stub_mode: bool = True) -> dict:
    return {
        "post_id": "p1",
        "campaign_id": "c1",
        "processing": {"schema_version": "1.3", "stub_mode": stub_mode},
    }


@pytest.fixture(autouse=True)
def _allow_stub_embeddings(monkeypatch):
    """These tests are ABOUT persisting stub vectors, so stubs must be allowed.

    `_ALLOW_STUB_EMBEDDING` is read from EMBEDDING_ALLOW_STUB at import time, so
    without this the whole file passes or fails depending on the developer's
    `.env` — and once that flag was set to false for real (the §3.1 flip) every
    test here started raising StubEmbeddingRefused from the refusal path rather
    than exercising the provenance chain it means to check.

    The refusal itself is covered in tests/test_retrieval_v2.py, where it is the
    subject rather than an obstacle.
    """
    monkeypatch.setattr(pers, "_ALLOW_STUB_EMBEDDING", True)


# ---------------------------------------------------------------------------
# The producer tells the truth
# ---------------------------------------------------------------------------


def test_stage1_reports_its_stub_embedding_as_a_stub():
    """The flag must come from Stage 1, which is the only layer that knows."""
    r = _stage1_stub_result()
    assert r["embedding_is_stub"] is True
    # ...and the vector is full-sized, which is precisely why no downstream
    # consumer can work this out for itself.
    assert len(r["embedding"]) == EMBEDDING_DIM
    assert r["embedding"] == stub_embedding(_CAPTION)


# ---------------------------------------------------------------------------
# The persistence layer must not "correct" the producer
# ---------------------------------------------------------------------------


def test_resolve_embedding_trusts_the_producer_over_the_dimension():
    """A correctly-sized vector flagged as a stub stays flagged.

    This is the assertion that fails on the pre-fix code: `_resolve_embedding`
    returned False for any EMBEDDING_DIM-length vector, whatever it was told.
    """
    vec = stub_embedding(_CAPTION)
    _, is_stub = pers._resolve_embedding(vec, "p1", True)
    assert is_stub is True


def test_resolve_embedding_reports_a_real_vector_as_real():
    vec = [0.01] * EMBEDDING_DIM
    out_vec, is_stub = pers._resolve_embedding(vec, "p1", False)
    assert is_stub is False
    assert len(out_vec) == EMBEDDING_DIM


def test_resolve_embedding_falls_back_to_its_own_stub_when_there_is_no_vector():
    """No vector at all → persistence substitutes a post_id-seeded stub, and
    says so regardless of what the caller claimed."""
    vec, is_stub = pers._resolve_embedding(None, "p1", False)
    assert is_stub is True
    assert vec == stub_embedding("p1")


# ---------------------------------------------------------------------------
# One value, not two
# ---------------------------------------------------------------------------


def test_assembler_reads_stage1s_flag_rather_than_guessing():
    r = _stage1_stub_result()
    assert asm._embedding_is_stub(_canonical(), r) is True


def test_assembler_flag_survives_a_stage1_result_from_before_the_field_existed():
    """Old messages have no `embedding_is_stub`; the run-wide marker stands in."""
    legacy = {"embedding": stub_embedding(_CAPTION)}
    assert asm._embedding_is_stub(_canonical(stub_mode=True), legacy) is True
    assert asm._embedding_is_stub(_canonical(stub_mode=False), legacy) is False


def test_a_real_vector_is_not_flagged():
    real = {"embedding": [0.01] * EMBEDDING_DIM, "embedding_is_stub": False}
    assert asm._embedding_is_stub(_canonical(stub_mode=False), real) is False


# ---------------------------------------------------------------------------
# End to end: Stage 1 -> assembler -> the bound SQL parameter
# ---------------------------------------------------------------------------


class _CapturingEngine:
    """Just enough async SQLAlchemy surface for `persist_postgres`."""

    def __init__(self):
        self.params: list[dict] = []

    def begin(self):
        class _Ctx:
            async def __aenter__(self_inner):
                return object()      # the repository is patched; nothing runs on it

            async def __aexit__(self_inner, *_exc):
                return False

        return _Ctx()


def _capture_upsert(monkeypatch, engine):
    """Record the kwargs persist_postgres hands the repository.

    It writes through `PostRepository(AsyncSession(bind=conn))` now, so a fake
    connection object with an `execute` method is no longer enough surface —
    SQLAlchemy rejects it before any parameter is bound. Capturing at the
    repository boundary asserts the same contract (the producer's flag is what
    gets persisted) without re-implementing SQLAlchemy.
    """
    import sqlalchemy.ext.asyncio as sa_async

    from defense.libs.repos.posts import PostRepository

    async def _fake_upsert(self, **kwargs):
        engine.params.append(kwargs)

    monkeypatch.setattr(PostRepository, "upsert_analysis", _fake_upsert)
    # persist_postgres builds `AsyncSession(bind=conn)` first, and SQLAlchemy
    # validates the bind before anything else happens.
    monkeypatch.setattr(sa_async, "AsyncSession", lambda **kwargs: object())


def test_the_flag_stage1_produced_is_the_flag_that_reaches_the_column(monkeypatch):
    """The whole point: producer -> assembler -> persisted parameter.

    On the pre-fix code Stage 1 said "stub", the trace frame said "stub", and the
    value bound into the INSERT said "not a stub". Every hop was individually
    defensible; the chain was wrong.
    """
    s1 = _stage1_stub_result()
    result = _canonical()
    flag = asm._embedding_is_stub(result, s1)

    engine = _CapturingEngine()
    _capture_upsert(monkeypatch, engine)
    asyncio.run(
        pers.persist_postgres(
            result, engine,
            embedding=s1["embedding"],
            embedding_is_stub=flag,
        )
    )

    assert engine.params, "persist_postgres bound no parameters"
    assert engine.params[0]["embedding_is_stub"] is True
    # ...and it is the producer's value, carried, not re-derived.
    assert engine.params[0]["embedding_is_stub"] == s1["embedding_is_stub"]


def test_persist_postgres_without_the_flag_still_marks_a_missing_vector(monkeypatch):
    """Callers that pass no flag keep the old dimension fallback."""
    engine = _CapturingEngine()
    _capture_upsert(monkeypatch, engine)
    asyncio.run(pers.persist_postgres(_canonical(), engine, embedding=None))
    assert engine.params[0]["embedding_is_stub"] is True


# ---------------------------------------------------------------------------
# The prototype classifier must not run on hash noise
# ---------------------------------------------------------------------------


def test_stub_embeddings_do_not_feed_the_prototype_topic_classifier():
    """`_embed_with_provenance` now returns a usable stub where a failed real
    model used to return `[]`. The empty list was what kept prototypes off it, so
    the flag has to do that job instead — otherwise this fix would have silently
    started classifying topics from hash noise."""
    vec, is_stub = ta._embed_with_provenance("some text", None)
    assert is_stub is True
    assert len(vec) == EMBEDDING_DIM


def test_embed_with_provenance_reports_a_failed_real_model_as_a_stub():
    class _Exploding:
        def encode(self, *_a, **_k):
            raise RuntimeError("model died")

    vec, is_stub = ta._embed_with_provenance("some text", _Exploding())
    assert is_stub is True, "a failed real model must not be reported as real"
    assert len(vec) == EMBEDDING_DIM


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-v"]))

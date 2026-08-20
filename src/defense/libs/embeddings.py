"""Shared text-embedding helper — single source of truth for vector dims.

Used by Stage-1 NLP (document embeddings), the API and retrieval-mcp (query
embeddings). Everything that touches the pgvector column
``analysis_results.embedding`` must agree on the dimension, so it lives here.

Behaviour
---------
- Stub mode (``MODEL_STUB_MODE=true``, the default) or when
  ``sentence-transformers`` isn't installed: a deterministic hash-seeded unit
  vector of ``EMBEDDING_DIM`` dims. Identical text → identical vector, so
  upserts/caching/round-trips are exercised, but similarity is NOT semantic.
- Real mode (``MODEL_STUB_MODE=false`` + ``uv sync --extra ml``): a
  SentenceTransformer (``EMBEDDING_MODEL``, default
  ``paraphrase-multilingual-mpnet-base-v2`` — 768-dim, multilingual incl.
  Bangla). If the model's output dim mismatches ``EMBEDDING_DIM`` the vector is
  truncated/padded once with a warning — fix the env instead of relying on it.
"""

from __future__ import annotations

import hashlib
import structlog
import os
from defense.libs.common.config import get_settings

config = get_settings()
import struct
from typing import Any, Optional

log = structlog.get_logger(__name__)

EMBEDDING_DIM: int = config.embedding_dim

# 768-dim multilingual default — matches the pgvector column in init-db.sql.
DEFAULT_EMBEDDING_MODEL = "paraphrase-multilingual-mpnet-base-v2"

#: The name recorded in ``analysis_results.embedding_model`` for a hash stub.
#: A sentinel rather than NULL: "which model produced this vector" and "no model
#: produced this vector" are different facts, and only the first is a NULL.
STUB_MODEL_NAME = "stub:sha256"

_model: Any = None
_model_failed = False
_dim_warned = False


def _stub_mode() -> bool:
    """Whether to hash instead of encode.

    ``EMBEDDING_STUB_MODE`` overrides ``MODEL_STUB_MODE`` for the sentence
    encoder alone, in either direction. Read through ``get_settings()`` rather
    than the module-level snapshot so a test (or a backfill run) can flip it
    without re-importing.
    """
    settings = get_settings()
    if settings.embedding_stub_mode is not None:
        return settings.embedding_stub_mode
    return settings.model_stub_mode


def stub_embedding(text: str) -> list[float]:
    """Deterministic unit vector seeded from the text hash (not semantic)."""
    import numpy as np

    digest = hashlib.sha256((text or "").encode("utf-8")).digest()
    (seed,) = struct.unpack(">Q", digest[:8])
    rng = np.random.default_rng(seed=seed % (2**31))
    vec = rng.standard_normal(EMBEDDING_DIM).astype(np.float32)
    norm = float(np.linalg.norm(vec))
    if norm > 0:
        vec = vec / norm
    return vec.tolist()


def _get_model() -> Optional[Any]:
    """Lazy-load the SentenceTransformer; None in stub mode or when unavailable.

    Falls back to CPU before it falls back to hashes. That order matters more
    than it looks: this project's GPU is a 4 GB card that normally already hosts
    the LLM (ollama holds ~2.6 GB of it), so loading the encoder on CUDA fails
    with `CUDA out of memory` intermittently — depending on nothing more than
    what the LLM is doing at that moment. The old behaviour caught that
    exception and returned None, which means every vector silently became a
    SHA-256 hash: `semantic_search` degraded to ranking noise, and it did so
    transiently, so the same query could be answered from real vectors once and
    from hashes a minute later.

    CPU is a completely adequate device for this: 99 ms for one query, 133 ms
    for a batch of 64, and bit-identical output (the EN/BN cross-lingual probe
    scores 0.771 on both). Losing 99 ms is not a cost worth paying hash noise to
    avoid, and unlike the GPU it does not contend with the LLM.
    """
    global _model, _model_failed
    if _stub_mode() or _model_failed:
        return None
    if _model is not None:
        return _model

    try:
        from sentence_transformers import SentenceTransformer  # type: ignore
    except Exception as exc:  # not installed — no device can help
        _model_failed = True
        log.error(
            "embedding model unavailable — falling back to STUB embeddings; "
            "semantic search and clustering will return arbitrary neighbours. "
            "Install it with `uv sync --extra embeddings`.",
            model=config.embedding_model,
            error=str(exc),
        )
        return None

    name = config.embedding_model
    # An explicit EMBEDDING_DEVICE is honoured first; otherwise let
    # sentence-transformers pick (CUDA when present). CPU is always the last
    # attempt, because the alternative to a slow real vector is a fast wrong one.
    attempts: list[str | None] = [config.embedding_device or None]
    if "cpu" not in attempts:
        attempts.append("cpu")

    for device in attempts:
        try:
            _model = SentenceTransformer(name, device=device)
            log.info("embedding model loaded", name=name, device=device or "auto")
            return _model
        except Exception as exc:
            if device != attempts[-1]:
                # Not terminal — there is another device to try. WARNING rather
                # than ERROR: retrieval is about to be correct, just slower.
                log.warning(
                    "embedding model failed to load on this device — retrying on CPU",
                    model=name, device=device or "auto", error=str(exc),
                )
                continue
            _model_failed = True
            log.error(
                "embedding model unavailable on every device — falling back to STUB "
                "embeddings; semantic search and clustering will return arbitrary "
                "neighbours, and every result row will report embedding_is_stub.",
                model=name, device=device, error=str(exc),
            )
            return None
    return _model  # pragma: no cover — the loop always returns


def active_model_name() -> str:
    """Which model is actually producing vectors right now.

    ``STUB_MODEL_NAME`` when the hash stub is in use — including the case where
    ``MODEL_STUB_MODE=false`` but the library or weights are missing, which is
    the one an operator is most likely to mistake for the real thing. Recorded
    on every row so a later ``EMBEDDING_MODEL`` change is detectable rather than
    silently mixing two vector spaces in one index (§3.7).
    """
    return STUB_MODEL_NAME if _get_model() is None else config.embedding_model


def fit_dim(vec: list[float]) -> list[float]:
    """Truncate/pad to EMBEDDING_DIM, warning once on mismatch."""
    global _dim_warned
    if len(vec) == EMBEDDING_DIM:
        return vec
    if not _dim_warned:
        _dim_warned = True
        log.warning(
            "embedding dim %d != EMBEDDING_DIM %d — truncating/padding; "
            "set EMBEDDING_MODEL to a %d-dim model (or adjust EMBEDDING_DIM + the "
            "analysis_results.embedding column)",
            len(vec), EMBEDDING_DIM, EMBEDDING_DIM,
        )
    if len(vec) > EMBEDDING_DIM:
        return vec[:EMBEDDING_DIM]
    return vec + [0.0] * (EMBEDDING_DIM - len(vec))


def embed_text(text: str) -> list[float]:
    """Embed text → list[float] of EMBEDDING_DIM (real model when available)."""
    vec, _is_stub = embed_text_with_provenance(text)
    return vec


def embed_text_with_provenance(text: str) -> tuple[list[float], bool]:
    """Embed text and report whether the vector is the deterministic STUB.

    ``stub_embedding`` is explicitly "a deterministic unit vector seeded from the
    text hash (not semantic)". With ``MODEL_STUB_MODE=true`` — the default —
    every vector written to the pgvector column is one of those, so kNN
    "semantic" search returns arbitrary neighbours and the embedding-cluster
    summarisation that reports.py presents as the LLM cost lever clusters noise.

    Nothing downstream could previously tell the difference: the assembler's
    trace frame reported ``embedding_stored: true`` and ``embedding_dims: 768``,
    both of which read as success. This is the signal that makes the distinction
    available (PROJECT_ASSESSMENT §5.9).
    """
    model = _get_model()
    if model is None:
        return stub_embedding(text), True
    vec = model.encode(text or "", normalize_embeddings=True)
    return fit_dim([float(v) for v in vec]), False


def embed_texts_with_provenance(texts: list[str]) -> tuple[list[list[float]], bool]:
    """Embed many texts in ONE model call, and report whether they are stubs.

    Comment threads are the reason this exists: a post carries hundreds of
    comments, and ``model.encode(list_of_texts)`` batches them through the
    transformer in a handful of forward passes where a loop over
    ``embed_text`` pays the per-call overhead hundreds of times (§3.2).

    The provenance flag is per-BATCH, not per-text, because the model is either
    loaded or it is not — there is no path where one element of a batch is
    semantic and the next is a hash.
    """
    if not texts:
        return [], _get_model() is None
    model = _get_model()
    if model is None:
        return [stub_embedding(t) for t in texts], True
    vectors = model.encode(
        [t or "" for t in texts], normalize_embeddings=True, batch_size=32
    )
    return [fit_dim([float(v) for v in vec]) for vec in vectors], False


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Batch form of :func:`embed_text`, discarding the provenance flag."""
    vectors, _ = embed_texts_with_provenance(texts)
    return vectors


def to_pgvector_literal(vector: list[float]) -> str:
    """Format a float list as a pgvector text literal: ``[0.1,0.2,...]``."""
    return "[" + ",".join(repr(float(v)) for v in vector) + "]"

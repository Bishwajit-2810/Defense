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

_model: Any = None
_model_failed = False
_dim_warned = False


def _stub_mode() -> bool:
    return config.model_stub_mode


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
    """Lazy-load the SentenceTransformer; None in stub mode or when unavailable."""
    global _model, _model_failed
    if _stub_mode() or _model_failed:
        return None
    if _model is None:
        try:
            from sentence_transformers import SentenceTransformer  # type: ignore

            name = config.embedding_model
            _model = SentenceTransformer(name)
            log.info("embedding model loaded", name=name)
        except Exception as exc:  # ImportError or download failure
            _model_failed = True
            log.warning("embedding model unavailable — using stub embeddings", error=str(exc))
            return None
    return _model


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


def to_pgvector_literal(vector: list[float]) -> str:
    """Format a float list as a pgvector text literal: ``[0.1,0.2,...]``."""
    return "[" + ",".join(repr(float(v)) for v in vector) + "]"

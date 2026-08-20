"""Retrieval primitives shared by retrieval-mcp and the search API.

Three things live here, in the order a query meets them:

1. :func:`lexical_terms` — how a natural-language question becomes something a
   Postgres text scan can use.
2. :func:`reciprocal_rank_fusion` — how two ranked lists become one, without
   either arm's score scale having to mean anything.
3. :func:`rerank` — an optional cross-encoder pass over the fused candidates.

Why fusion rather than a mode switch
------------------------------------
``GET /v1/search?semantic=true|false`` used to make the *caller* pick, and the
``semantic_search`` MCP tool — the one the agents actually use — had no lexical
mode at all. On this corpus that is the wrong place to put the choice: it is
code-mixed Bangla / English / Banglish, where exact entity names,
transliterations and hashtags are precisely what a multilingual sentence encoder
blurs and precisely what a lexical match nails. Neither arm dominates, so the
system should run both.

Reciprocal rank fusion is used instead of score normalisation because the two
arms produce numbers that are not comparable and never will be: one is a cosine
similarity in [-1, 1], the other is a trigram similarity or a tsvector rank with
its own arbitrary scale. RRF throws the scores away and keeps only the ranks:

    score(d) = Σ  1 / (k + rank_i(d))       over each retriever i that returned d

with k = 60 (the constant from Cormack et al. 2009, and the one every
implementation uses). A document both arms rank highly beats a document either
arm ranks first alone, which is the behaviour wanted here.

RRF is also the reason hybrid retrieval is worth switching on *before* real
embeddings are: with ``MODEL_STUB_MODE=true`` the vector arm contributes noise
at every rank, and the lexical arm still finds the post. Fusion degrades to
"lexical, with some noise mixed in", where pure kNN degrades to "noise".
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Sequence

import structlog

from defense.libs.common.config import get_settings

log = structlog.get_logger(__name__)

_config = get_settings()

#: Tokens too short or too common to narrow anything down. Deliberately tiny and
#: English-only: this corpus is mostly Bangla, and a stop-list that guessed at
#: Bengali function words would drop content words it misread.
_STOPWORDS = frozenset(
    """
    a an and are as at be but by for from has have how in into is it its of on or
    that the their there these they this to was were what when where which who
    why will with about most many much any all show find tell me give list
    """.split()
)

#: Word characters INCLUDING the Bengali block, so tokenising a Bangla query does
#: not reduce it to punctuation. \w under re.UNICODE already covers it; this is
#: written out so the intent survives someone "simplifying" the pattern later.
_TOKEN_RE = re.compile(r"[\wঀ-৿]+", re.UNICODE)


def lexical_terms(query: str, *, max_terms: int = 8) -> list[str]:
    """Content tokens from a natural-language query, longest first.

    Longest-first because a lexical arm has a fixed budget of predicates and the
    long tokens are the discriminating ones: in "what are people saying about
    fuel price increases", `increases` and `people` are worth a scan and `about`
    is not.
    """
    seen: set[str] = set()
    terms: list[str] = []
    for raw in _TOKEN_RE.findall(query or ""):
        tok = raw.casefold()
        if len(tok) < 2 or tok in _STOPWORDS or tok in seen:
            continue
        seen.add(tok)
        terms.append(tok)
    terms.sort(key=len, reverse=True)
    return terms[:max_terms]


def reciprocal_rank_fusion(
    ranked_lists: Sequence[Sequence[str]],
    *,
    k: int | None = None,
    weights: Sequence[float] | None = None,
) -> dict[str, float]:
    """Fuse ranked ID lists into ``{id: rrf_score}``, best first when sorted.

    ``ranked_lists`` are lists of IDs in rank order — the scores that produced
    them are deliberately not an input. ``weights`` scales each arm's
    contribution for callers that have a reason to trust one more (the API's
    stub-vector case does; see below).

    An ID absent from an arm contributes nothing from that arm, rather than
    being penalised with a worst-rank placeholder. That is what makes RRF robust
    to arms of different lengths: a lexical scan that returns 3 rows and a kNN
    that returns 40 fuse correctly without normalising either.
    """
    kk = _config.retrieval_rrf_k if k is None else k
    scores: dict[str, float] = {}
    for i, ranked in enumerate(ranked_lists):
        weight = weights[i] if weights is not None and i < len(weights) else 1.0
        if weight == 0:
            continue
        for rank, doc_id in enumerate(ranked, start=1):
            scores[doc_id] = scores.get(doc_id, 0.0) + weight / (kk + rank)
    return scores


def fuse(
    arms: dict[str, Sequence[str]],
    *,
    weights: dict[str, float] | None = None,
    k: int | None = None,
) -> list[tuple[str, float, list[str]]]:
    """RRF over named arms, returning ``(id, score, arms_that_found_it)``.

    The third element is the point of naming the arms: an operator (or an agent
    prompt) can be told that a hit came from the lexical arm only, which on a
    stub-vector corpus is the difference between a real match and a coincidence.
    """
    names = list(arms)
    ranked_lists = [arms[n] for n in names]
    weight_seq = [(weights or {}).get(n, 1.0) for n in names]
    scores = reciprocal_rank_fusion(ranked_lists, k=k, weights=weight_seq)

    found_in: dict[str, list[str]] = {}
    for name in names:
        for doc_id in arms[name]:
            found_in.setdefault(doc_id, []).append(name)

    out = [(doc_id, score, found_in.get(doc_id, [])) for doc_id, score in scores.items()]
    out.sort(key=lambda t: (-t[1], t[0]))
    return out


# ---------------------------------------------------------------------------
# Cross-encoder reranking (§3.4)
# ---------------------------------------------------------------------------
# Retrieve 30-50, rerank down to 8. A bi-encoder embeds the query and the
# document independently and can only ever compare two summaries of them; a
# cross-encoder reads the pair together, which is why it wins on precision and
# why it cannot be used as the first-stage retriever.
#
# Off by default (`RETRIEVAL_RERANK=false`). It needs the `ml` extra, it costs a
# second model load, and reranking a list produced by hash vectors reorders
# noise. Everything below degrades to "return the input order" rather than
# raising: a missing reranker must cost precision, never a run.

_reranker: Any = None
_reranker_failed = False


def _get_reranker() -> Any | None:
    """Lazy-load the cross-encoder; ``None`` when disabled or unavailable."""
    global _reranker, _reranker_failed
    if not _config.retrieval_rerank or _reranker_failed:
        return None
    if _reranker is None:
        try:
            from sentence_transformers import CrossEncoder  # type: ignore

            _reranker = CrossEncoder(_config.retrieval_rerank_model)
            log.info("reranker_loaded", model=_config.retrieval_rerank_model)
        except Exception as exc:
            _reranker_failed = True
            log.warning(
                "reranker unavailable — returning first-stage order",
                model=_config.retrieval_rerank_model,
                error=str(exc),
            )
            return None
    return _reranker


def rerank(
    query: str,
    candidates: Sequence[dict],
    *,
    text_key: str = "text",
    limit: int | None = None,
) -> tuple[list[dict], bool]:
    """Reorder ``candidates`` by cross-encoder relevance to ``query``.

    Returns ``(rows, reranked)``. ``reranked`` is False when the pass did not
    run — disabled, unavailable, or nothing to score — and the caller is
    expected to report it rather than let a first-stage ordering be presented as
    a reranked one. Rows that ran through the model carry ``rerank_score``.
    """
    rows = list(candidates)
    if not rows:
        return rows, False
    model = _get_reranker()
    if model is None:
        return rows[:limit] if limit else rows, False

    pairs = [(query, str(r.get(text_key) or "")) for r in rows]
    try:
        scores = model.predict(pairs)
    except Exception as exc:  # a live model that throws must not kill the run
        log.warning("rerank_failed", error=str(exc))
        return rows[:limit] if limit else rows, False

    for row, score in zip(rows, scores):
        row["rerank_score"] = round(float(score), 6)
    rows.sort(key=lambda r: r.get("rerank_score", 0.0), reverse=True)
    return (rows[:limit] if limit else rows), True


def candidate_pool(limit: int, *, cap: int = 200) -> int:
    """How many rows each arm fetches before fusion, for a requested ``limit``.

    Over-fetching is what makes fusion and reranking able to change anything: if
    both arms return exactly the k rows the caller asked for, the fused list is
    those same rows in a different order and no document either arm ranked 11th
    can ever be promoted.
    """
    return min(max(limit * max(1, _config.retrieval_candidate_multiplier), limit), cap)


def dedupe_preserving_order(ids: Iterable[str]) -> list[str]:
    """First occurrence wins — rank order is the thing being preserved."""
    seen: set[str] = set()
    out: list[str] = []
    for i in ids:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out


__all__ = [
    "lexical_terms",
    "reciprocal_rank_fusion",
    "fuse",
    "rerank",
    "candidate_pool",
    "dedupe_preserving_order",
]

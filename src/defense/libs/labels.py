"""Canonical label vocabularies shared across stages.

``post_type`` used to exist as three independent copies: the Stage-2 prompt's
inline list, the Stage-1 stub heuristic, and the router's rule. Keeping one
tuple here means Stage 1's own classification, the Stage-2 LLM prompt and the
router gate cannot silently drift apart.

The prototype phrases are the text embedded for zero-shot cosine classification
in Stage 1 real mode (text_analyzer._classify_post_type_by_prototype); they are
descriptions of the label, not training data.
"""

from __future__ import annotations

# Ordered so "other" stays last — it is the explicit fallback, never a match.
POST_TYPES: tuple[str, ...] = (
    "complaint",
    "news",
    "opinion",
    "promotion",
    "humor",
    "personal",
    "political",
    "religious",
    "other",
)

POST_TYPE_SET: frozenset[str] = frozenset(POST_TYPES)

# Label -> phrase embedded as the zero-shot prototype. "other" is excluded: it
# is what we emit when nothing clears the similarity floor.
POST_TYPE_PROTOTYPES: dict[str, str] = {
    "complaint": "a complaint about corruption, injustice, negligence or poor service",
    "news": "a news report of an event, citing what happened, where and when",
    "opinion": "the writer's personal opinion, analysis or argument about an issue",
    "promotion": "an advertisement promoting a product, service, offer or price",
    "humor": "a joke, meme, sarcasm or something written to be funny",
    "personal": "a personal life update such as a birthday, wedding, family news or thanks",
    "political": "politics, government, elections, political parties or leaders",
    "religious": "religion, faith, prayer, scripture or a religious occasion",
}


# ---------------------------------------------------------------------------
# Per-comment label provenance
# ---------------------------------------------------------------------------
# How a per-comment sentiment label was produced. Lives here, next to the other
# cross-stage vocabulary, because BOTH Stage 1 (which assigns the methods) and
# Stage 2 (which re-labels the top-N and recomputes the aggregates) have to
# agree on what counts as a model inference — the §5.1 lesson: one writer, one
# reader, one definition.
#
#   fast   — emoji list + a 14-word Bangla/Banglish lexicon; free, no model
#   stub   — _stub_sentiment: a label derived from
#            `sum(ord(c) for c in text[:50]) % 100`. Deterministic and
#            reproducible, and NOT sentiment.
#   model  — a transformer (BanglaBERT / BanglishBERT / XLM-R) actually ran
#   llm    — the stage1 LLM, or Stage-2's context-aware stance pass
#   emoji  — an emoji-only reaction, kept as signal but never sent to the LLM
#   failed — classification raised; no label was produced
COMMENT_METHODS: tuple[str, ...] = (
    "fast", "stub", "model", "llm", "emoji", "failed",
)

#: Methods where a model or an LLM actually produced the label.
INFERRED_METHODS: frozenset[str] = frozenset({"model", "llm"})


def label_provenance(method_counts: dict[str, int]) -> dict:
    """Summarise where a post's comment labels came from.

    Exists so a ``sentiment_breakdown`` chart can state its own provenance. In
    the shipped configuration the majority of comment labels are not model
    output, and a chart that cannot say so is the §4 failure in miniature: an
    output that reports success regardless of what actually ran.
    """
    total = sum(method_counts.values())
    inferred = sum(n for m, n in method_counts.items() if m in INFERRED_METHODS)
    return {
        "total": total,
        "inferred": inferred,               # model or LLM
        "heuristic": total - inferred,      # fast path, hash stub, emoji
        "inferred_share": round(inferred / total, 4) if total else 0.0,
        "by_method": dict(method_counts),
    }


__all__ = [
    "POST_TYPES",
    "POST_TYPE_SET",
    "POST_TYPE_PROTOTYPES",
    "COMMENT_METHODS",
    "INFERRED_METHODS",
    "label_provenance",
]

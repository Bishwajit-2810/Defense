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

__all__ = ["POST_TYPES", "POST_TYPE_SET", "POST_TYPE_PROTOTYPES"]

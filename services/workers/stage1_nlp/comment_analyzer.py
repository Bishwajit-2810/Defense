"""Per-comment NLP analysis and comment-thread aggregation.

analyze_comments():
  1. Runs analyze_text on every embedded comment.
  2. Aggregates sentiment into a positive / negative / neutral breakdown.
  3. Extracts cross-comment themes by collecting keywords weighted by likes.
  4. Selects up to 3 representative comments — highest-liked comment per
     sentiment class — for the output payload.
"""

from __future__ import annotations

import logging
from collections import Counter
from typing import TYPE_CHECKING, Any

from .text_analyzer import analyze_text

if TYPE_CHECKING:
    from .models import ModelRegistry

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Representative-comment selection
# ---------------------------------------------------------------------------

def _select_representative(
    analyzed_comments: list[dict],
) -> list[dict]:
    """Return up to 3 representative comments, one per sentiment class.

    Within each class the comment with the highest like count is chosen.
    """
    buckets: dict[str, dict | None] = {
        "positive": None,
        "negative": None,
        "neutral": None,
    }

    for item in analyzed_comments:
        sentiment = item.get("sentiment") or "neutral"
        likes = item.get("likes", 0)
        existing = buckets.get(sentiment)
        if existing is None or likes > existing.get("likes", -1):
            buckets[sentiment] = {
                "id": item.get("id", ""),
                "text": item.get("text", ""),
                "sentiment": sentiment,
                "likes": likes,
            }

    return [v for v in buckets.values() if v is not None]


# ---------------------------------------------------------------------------
# Theme extraction
# ---------------------------------------------------------------------------

def _extract_themes(analyzed_comments: list[dict]) -> list[str]:
    """Aggregate keywords across all comments, weight by likes.

    The top-5 keywords (by weighted frequency) become the themes.
    """
    counter: Counter[str] = Counter()
    for item in analyzed_comments:
        likes = item.get("likes", 0)
        weight = max(1, likes)  # every comment contributes at least 1
        for kw in item.get("keywords", []):
            counter[kw] += weight

    return [kw for kw, _ in counter.most_common(5)]


# ---------------------------------------------------------------------------
# Top keyword aggregation (unweighted frequency for top_keywords)
# ---------------------------------------------------------------------------

def _aggregate_top_keywords(analyzed_comments: list[dict]) -> list[str]:
    counter: Counter[str] = Counter()
    for item in analyzed_comments:
        for kw in item.get("keywords", []):
            counter[kw] += 1
    return [kw for kw, _ in counter.most_common(5)]


# ---------------------------------------------------------------------------
# Public entry-point
# ---------------------------------------------------------------------------

async def analyze_comments(
    comments: list[dict],
    registry: Any,  # ModelRegistry — avoid circular import
) -> dict:
    """Analyse all embedded comments and return a comment_analysis dict.

    Parameters
    ----------
    comments:
        The raw list from the upstream payload (each element has at least
        "id", "text", "likes", and optionally "authorUsername").
    registry:
        The shared ModelRegistry instance.

    Returns
    -------
    {
        "analyzed": int,
        "coverage": float,             # filled by worker (needs commentCount)
        "sentiment_breakdown": {...},
        "themes": [...],
        "top_keywords": [...],
        "representative_comments": [...]
    }
    """
    if not comments:
        return {
            "analyzed": 0,
            "coverage": 0.0,
            "sentiment_breakdown": {"positive": 0, "negative": 0, "neutral": 0},
            "themes": [],
            "top_keywords": [],
            "representative_comments": [],
        }

    analyzed_list: list[dict] = []
    sentiment_counts: dict[str, int] = {"positive": 0, "negative": 0, "neutral": 0}

    for comment in comments:
        text = comment.get("text") or ""
        try:
            nlp = await analyze_text(text, registry)
        except Exception as exc:
            logger.warning("Comment NLP failed for id=%s: %s", comment.get("id"), exc)
            nlp = {
                "sentiment": "neutral",
                "sentiment_score": 0.0,
                "keywords": [],
            }

        sentiment = nlp.get("sentiment") or "neutral"
        # Normalise to known keys
        if sentiment not in sentiment_counts:
            sentiment = "neutral"
        sentiment_counts[sentiment] += 1

        analyzed_list.append(
            {
                "id": comment.get("id", ""),
                "text": text,
                "likes": comment.get("likes", 0),
                "sentiment": sentiment,
                "sentiment_score": nlp.get("sentiment_score", 0.0),
                "keywords": nlp.get("keywords", []),
                "topics": nlp.get("topics", []),
            }
        )

    themes = _extract_themes(analyzed_list)
    top_keywords = _aggregate_top_keywords(analyzed_list)
    representative = _select_representative(analyzed_list)

    return {
        "analyzed": len(analyzed_list),
        # coverage is 0.0 here; the worker fills the real value using
        # engagement.commentCount after this function returns.
        "coverage": 0.0,
        "sentiment_breakdown": sentiment_counts,
        "themes": themes,
        "top_keywords": top_keywords,
        "representative_comments": representative,
    }

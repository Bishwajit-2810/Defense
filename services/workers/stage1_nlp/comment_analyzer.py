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
import re
from collections import Counter
from typing import TYPE_CHECKING, Any

from .text_analyzer import analyze_sentiment

if TYPE_CHECKING:
    from .models import ModelRegistry

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Hybrid per-comment classifier
# ---------------------------------------------------------------------------
# Strategy (chosen for full per-comment coverage at scale):
#   * "fast"  — emoji-only / very short comments are scored by an emoji +
#               tiny multilingual lexicon heuristic. No model call.
#   * "model" — substantive comments go through analyze_sentiment(), which uses
#               the XLM-R sentiment model in real mode (the deterministic stub
#               when MODEL_STUB_MODE is on).
# This keeps thousands of low-signal emoji reactions off the heavy path while
# every comment still receives a sentiment label.

# Emoji are matched as substrings (variation selectors / ZWJ make per-char
# membership unreliable). Laughing/clown emoji read as mocking in this corpus,
# so they lean negative.
_POS_EMOJI: tuple[str, ...] = (
    "❤", "🥰", "😍", "👍", "💚", "💙", "💛", "🧡", "💜", "🤍", "🖤", "🔥",
    "💝", "🌹", "🥀", "🙏", "✨", "💪", "👏", "🤝", "🫶", "💯", "🎉", "😊",
    "☺", "🙂", "😁", "😄", "😎", "💕", "💖", "♥",
)
_NEG_EMOJI: tuple[str, ...] = (
    "😡", "🤬", "👎", "💩", "🤮", "🤢", "😠", "😤", "🖕", "🤡", "😒", "🙄",
    "😞", "😢", "😭", "💔", "⚠", "🚫", "❌", "🤣", "😂", "😆",
)

# Tiny multilingual seed lexicon for short Banglish/English/Bangla comments.
_POS_LEX: frozenset[str] = frozenset(
    {"good", "valo", "bhalo", "love", "nice", "best", "thanks", "support",
     "ভালো", "সুন্দর", "ভালোবাসা", "শুভকামনা", "সৎ"}
)
_NEG_LEX: frozenset[str] = frozenset(
    {"bad", "fake", "chor", "golam", "vondo", "vand", "fuck", "stupid", "liar",
     "মিথ্যা", "ইতর", "ভন্ড", "চোর", "গোলাম", "অসভ্য"}
)

# A comment is "short" (→ fast path) when, after stripping emoji/punctuation,
# it has <= 2 word tokens or fewer than 4 textual characters.
_WORD_RE = re.compile(r"[0-9A-Za-zঀ-৿]+")

# ---------------------------------------------------------------------------
# Free emotion heuristic (emoji + tiny lexicon)
# ---------------------------------------------------------------------------
# Per-comment emotion runs at ZERO model/LLM cost so every comment is labelled.
# Stage-2 later upgrades only the top-N comments' emotion inside the existing
# stance LLM call (no extra calls). Taxonomy matches the post-level emotion set
# the dashboard already colours: anger, sadness, joy, fear, disgust, surprise,
# neutral.
_EMOTION_EMOJI: dict[str, tuple[str, ...]] = {
    "anger":    ("😡", "🤬", "😠", "😤", "🖕", "👎"),
    "sadness":  ("😢", "😭", "💔", "😞", "🥀", "😔", "😟"),
    "joy":      ("❤", "🥰", "😍", "😊", "😁", "😄", "🙂", "☺", "😎", "🥳",
                 "💕", "💖", "💚", "💙", "💛", "🧡", "💜", "♥", "🔥", "🎉",
                 "👍", "👏", "🙏", "🫶", "💯", "✨"),
    "fear":     ("😨", "😰", "😱", "😧", "😬"),
    "disgust":  ("🤮", "🤢", "💩", "🙄", "😒"),
    "surprise": ("😮", "😲", "😯", "🤯", "😳", "‼", "⁉"),
}

# Tiny multilingual seed lexicon (Bangla / Banglish / English) per emotion.
_EMOTION_LEX: dict[str, frozenset[str]] = {
    "anger":    frozenset({"angry", "rege", "raag", "furious", "hate", "ghrina",
                           "চোর", "ইতর", "অসভ্য", "রাগ", "ঘৃণা"}),
    "sadness":  frozenset({"sad", "kosto", "dukkho", "crying", "miss", "kadtesi",
                           "দুঃখ", "কষ্ট", "শোক", "কান্না"}),
    "joy":      frozenset({"happy", "khushi", "valo", "bhalo", "love", "best",
                           "nice", "khusi", "আনন্দ", "খুশি", "ভালোবাসা", "দারুণ"}),
    "fear":     frozenset({"scared", "voy", "bhoy", "afraid", "terrified",
                           "ভয়", "আতঙ্ক"}),
    "disgust":  frozenset({"disgusting", "ghenna", "jaghonno", "ottyacharito",
                           "ঘেন্না", "জঘন্য", "বিরক্ত"}),
    "surprise": frozenset({"wow", "omg", "really", "obak", "ki", "ascharjo",
                           "অবাক", "আশ্চর্য", "তাজ্জব"}),
}

_EMOTIONS: tuple[str, ...] = ("anger", "sadness", "joy", "fear", "disgust", "surprise")

# All labels that may appear in emotion_breakdown (heuristic + LLM never emit
# anything outside this set).
_EMOTION_LABELS: frozenset[str] = frozenset(_EMOTIONS) | {"neutral"}


def _empty_emotion_breakdown() -> dict[str, int]:
    """Zeroed emotion_breakdown with a stable key order (neutral last)."""
    return {emo: 0 for emo in (*_EMOTIONS, "neutral")}


def _fast_emotion(text: str, tokens: list[str]) -> str:
    """Emoji + lexicon heuristic → one emotion label (or "neutral").

    Scores every emotion by emoji occurrences + lexicon hits and returns the
    top scorer; ties / no signal fall back to "neutral".
    """
    low = {t.lower() for t in tokens}
    best, best_score = "neutral", 0
    for emo in _EMOTIONS:
        score = sum(text.count(e) for e in _EMOTION_EMOJI.get(emo, ()))
        score += sum(1 for t in low if t in _EMOTION_LEX.get(emo, frozenset()))
        if score > best_score:
            best, best_score = emo, score
    return best


def _emoji_counts(text: str) -> tuple[int, int]:
    pos = sum(text.count(e) for e in _POS_EMOJI)
    neg = sum(text.count(e) for e in _NEG_EMOJI)
    return pos, neg


def _is_short(text: str, tokens: list[str]) -> bool:
    stripped = _WORD_RE.sub("", text)  # what remains is emoji/punctuation
    textual_chars = sum(len(t) for t in tokens)
    return len(tokens) <= 2 or textual_chars < 4 or (not tokens and stripped)


def _fast_classify(text: str, tokens: list[str]) -> tuple[str, float]:
    """Emoji + lexicon heuristic for short/emoji comments → (label, score)."""
    pos_e, neg_e = _emoji_counts(text)
    low = {t.lower() for t in tokens}
    pos = pos_e + sum(1 for t in low if t in _POS_LEX)
    neg = neg_e + sum(1 for t in low if t in _NEG_LEX)

    if pos > neg:
        return "positive", round(min(1.0, 0.4 + 0.15 * (pos - neg)), 3)
    if neg > pos:
        return "negative", round(max(-1.0, -0.4 - 0.15 * (neg - pos)), 3)
    return "neutral", 0.0


async def classify_comment(text: str, registry: Any) -> dict:
    """Classify one comment's sentiment + emotion via the hybrid router.

    Returns {"sentiment", "sentiment_score", "emotion", "method", "keywords"}.
    The emotion label is always derived from the free emoji+lexicon heuristic
    here; Stage-2 upgrades the top-N comments' emotion via the LLM later.
    """
    tokens = _WORD_RE.findall(text or "")
    emotion = _fast_emotion(text or "", tokens)

    if _is_short(text or "", tokens):
        label, score = _fast_classify(text or "", tokens)
        return {
            "sentiment": label,
            "sentiment_score": score,
            "emotion": emotion,
            "method": "fast",
            "keywords": [],
        }

    label, score, _conf = await analyze_sentiment(text, registry)
    # Cheap keyword extraction (longest unique tokens) — no model needed.
    uniq: list[str] = []
    seen: set[str] = set()
    for t in tokens:
        if len(t) >= 4 and t not in seen:
            seen.add(t)
            uniq.append(t)
    uniq.sort(key=len, reverse=True)
    return {
        "sentiment": label,
        "sentiment_score": score,
        "emotion": emotion,
        "method": "model",
        "keywords": uniq[:5],
    }


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
            "emotion_breakdown": _empty_emotion_breakdown(),
            "method_breakdown": {"fast": 0, "model": 0},
            "themes": [],
            "top_keywords": [],
            "representative_comments": [],
            "comments": [],
        }

    analyzed_list: list[dict] = []
    per_comment: list[dict] = []
    sentiment_counts: dict[str, int] = {"positive": 0, "negative": 0, "neutral": 0}
    emotion_counts: dict[str, int] = _empty_emotion_breakdown()
    method_counts: dict[str, int] = {"fast": 0, "model": 0}

    for comment in comments:
        text = comment.get("text") or ""
        try:
            nlp = await classify_comment(text, registry)
        except Exception as exc:
            logger.warning("Comment NLP failed for id=%s: %s", comment.get("id"), exc)
            nlp = {
                "sentiment": "neutral",
                "sentiment_score": 0.0,
                "emotion": "neutral",
                "method": "fast",
                "keywords": [],
            }

        sentiment = nlp.get("sentiment") or "neutral"
        # Normalise to known keys
        if sentiment not in sentiment_counts:
            sentiment = "neutral"
        sentiment_counts[sentiment] += 1
        emotion = nlp.get("emotion") or "neutral"
        if emotion not in emotion_counts:
            emotion = "neutral"
        emotion_counts[emotion] += 1
        method = nlp.get("method", "fast")
        method_counts[method] = method_counts.get(method, 0) + 1

        likes = comment.get("likes", 0)
        score = nlp.get("sentiment_score", 0.0)
        analyzed_list.append(
            {
                "id": comment.get("id", ""),
                "text": text,
                "likes": likes,
                "sentiment": sentiment,
                "sentiment_score": score,
                "emotion": emotion,
                "keywords": nlp.get("keywords", []),
                "topics": nlp.get("topics", []),
            }
        )
        # The persisted per-comment record (one row per comment downstream).
        per_comment.append(
            {
                "id": comment.get("id", ""),
                "text": text,
                "likes": likes,
                "author": comment.get("authorUsername"),
                "parent_id": comment.get("parentId"),
                "sentiment": sentiment,
                "sentiment_score": score,
                "emotion": emotion,
                "method": method,
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
        "emotion_breakdown": emotion_counts,
        "method_breakdown": method_counts,
        "themes": themes,
        "top_keywords": top_keywords,
        "representative_comments": representative,
        # Per-comment sentiment for every embedded comment (full coverage of
        # the stored sample). Bulky — the API strips it from list responses.
        "comments": per_comment,
    }

"""Per-comment NLP analysis and comment-thread aggregation.

analyze_comments():
  1. Runs analyze_text on every embedded comment.
  2. Aggregates sentiment into a positive / negative / neutral breakdown.
  3. Extracts cross-comment themes by collecting keywords weighted by likes.
  4. Selects up to 3 representative comments — highest-liked comment per
     sentiment class — for the output payload.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import re
from collections import Counter
from typing import TYPE_CHECKING, Any

from libs.labels import label_provenance
from libs.stance_scoring import aggregate_target_stances, score_comment_deterministic
from libs.stance_targets import Targets, load_targets, unmatched_targets

from .llm_analyzer import classify_comments_llm
from .text_analyzer import analyze_sentiment_batch, analyze_sentiment_engine

if TYPE_CHECKING:
    from .models import ModelRegistry

logger = logging.getLogger(__name__)

# LLM-backed Stage-1 comment labelling (STAGE1_LLM=true). Only the most-engaged
# *substantive* comments get the LLM (batched) so a post with thousands of
# comments can't stall Stage 1; the rest keep their instant heuristic/stub label,
# so coverage stays 100%. Mirrors the Stage-2 stance cap (COMMENT_STANCE_*).
#: 0 = no cap — EVERY non-emoji comment reaches the LLM (§6.3). This used to
#: default to 60, which meant only 28.7% of comments ever got an LLM label
#: while the output still described itself as full per-comment coverage. Full
#: coverage over speed is the trade being chosen deliberately; set a positive
#: value for a fast demo run.
_LLM_COMMENT_MAX = int(os.getenv("STAGE1_LLM_COMMENT_MAX", "0"))
#: Smaller batches than before (was 40): a failed batch loses less work, and
#: concurrency now supplies the throughput that batch size used to.
_LLM_COMMENT_BATCH = int(os.getenv("STAGE1_LLM_BATCH", os.getenv("STAGE1_LLM_COMMENT_BATCH", "25")))
#: Batches in flight at once. The old loop was strictly sequential, so raising
#: the cap simply stalled the post.
_LLM_CONCURRENCY = max(1, int(os.getenv("STAGE1_LLM_CONCURRENCY", "3")))
#: Retries per batch, so one bad batch does not lose the rest.
_LLM_BATCH_RETRIES = max(0, int(os.getenv("STAGE1_LLM_BATCH_RETRIES", "1")))

# Watchlist-driven target stance (stance_targets.md). Matching is pure string
# work, so it runs in Stage 1 on EVERY comment of every post — including
# bypassed posts, which means mention *volume* is available even where the
# premium stance pass never runs. Absent config file => feature off, and the
# pipeline behaves exactly as before.
_STANCE_TARGETS_PATH = os.getenv("STANCE_TARGETS_FILE", "config/stance_targets.yml")
_TARGETS_CACHE: Targets | None = None


def get_targets() -> Targets:
    """The loaded watchlist, cached per process. Empty when no file is present."""
    global _TARGETS_CACHE
    if _TARGETS_CACHE is None:
        try:
            _TARGETS_CACHE = load_targets(_STANCE_TARGETS_PATH)
            if _TARGETS_CACHE:
                logger.info(
                    "stance watchlist loaded: %s targets from %s (version=%s)",
                    len(_TARGETS_CACHE.targets), _STANCE_TARGETS_PATH,
                    _TARGETS_CACHE.version,
                )
        except Exception as exc:
            # A malformed watchlist must not take down analysis, but it must be
            # loud — a silently-disabled watchlist is the §5.1 failure again.
            logger.error("stance watchlist failed to load from %s: %s",
                         _STANCE_TARGETS_PATH, exc)
            _TARGETS_CACHE = Targets()
    return _TARGETS_CACHE


def reset_targets_cache() -> None:
    """Drop the cached watchlist — for tests and for a config reload."""
    global _TARGETS_CACHE
    _TARGETS_CACHE = None


# ---------------------------------------------------------------------------
# Hybrid per-comment classifier
# ---------------------------------------------------------------------------
# Strategy (chosen for full per-comment coverage at scale):
#   * "fast"  — emoji-only / very short comments are scored by an emoji +
#               tiny multilingual lexicon heuristic. No model call.
#   * "model" — substantive comments go through analyze_sentiment_engine(),
#               which uses the XLM-R sentiment model in real mode.
#   * "stub"  — the same substantive path when MODEL_STUB_MODE is on (or the
#               model is unavailable). Reported honestly rather than as "model":
#               `_stub_sentiment` derives its label from a hash of the text, so
#               counting it as a model inference overstated the model's reach by
#               thousands of comments per run.
# This keeps thousands of low-signal emoji reactions off the heavy path while
# every comment still receives a sentiment label. `label_provenance` (in
# libs/labels.py) turns the resulting method_breakdown into the mix a chart can
# report about itself.

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
    "😞", "😢", "😭", "💔", "⚠", "🚫", "❌",
)

# Laughing emoji are a *named decision*, not an accident. On this corpus of
# political content they overwhelmingly read as mockery, so they count as
# negative — but they used to count as negative for SENTIMENT while mapping to
# no emotion at all, so a mocking comment came out `negative` / `neutral` and
# the two tables silently disagreed. Both now follow this one switch.
#   COMMENT_LAUGH_SENTIMENT=negative (default) | positive | neutral
_LAUGH_EMOJI: tuple[str, ...] = ("🤣", "😂", "😆", "😹")
_LAUGH_SENTIMENT = os.getenv("COMMENT_LAUGH_SENTIMENT", "negative").lower()
if _LAUGH_SENTIMENT not in ("negative", "positive", "neutral"):
    _LAUGH_SENTIMENT = "negative"
#: The emotion laughter maps to, kept consistent with the sentiment reading:
#: mockery is disgust, genuine laughter is joy.
_LAUGH_EMOTION = {"negative": "disgust", "positive": "joy", "neutral": None}[
    _LAUGH_SENTIMENT
]

if _LAUGH_SENTIMENT == "negative":
    _NEG_EMOJI = _NEG_EMOJI + _LAUGH_EMOJI
elif _LAUGH_SENTIMENT == "positive":
    _POS_EMOJI = _POS_EMOJI + _LAUGH_EMOJI

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

# Laughter joins the emotion table under the same switch that decides its
# sentiment, so `sentiment_breakdown` and `emotion_breakdown` can no longer
# disagree about what a 🤣 means.
if _LAUGH_EMOTION is not None:
    _EMOTION_EMOJI[_LAUGH_EMOTION] = _EMOTION_EMOJI[_LAUGH_EMOTION] + _LAUGH_EMOJI

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


#: Comment kinds. The old code had one boolean (`_is_short`) that conflated two
#: genuinely different things — an emoji-only reaction and a one-word text
#: comment — and labelled both with the emoji+lexicon heuristic. They differ in
#: exactly the way that matters for cost: an emoji-only comment has no text for
#: an LLM to read, so sending it is the cheapest possible way to waste tokens.
KIND_EMOJI = "emoji"              # no word tokens at all
KIND_SHORT = "short"              # 1-2 word tokens, or < 4 textual characters
KIND_SUBSTANTIVE = "substantive"  # enough text to be worth a model


def _comment_kind(text: str, tokens: list[str] | None = None) -> str:
    """Classify a comment by how much text it actually contains."""
    if tokens is None:
        tokens = _WORD_RE.findall(text or "")
    if not tokens:
        # Only emoji, punctuation and whitespace (or nothing at all).
        return KIND_EMOJI
    if len(tokens) <= 2 or sum(len(t) for t in tokens) < 4:
        return KIND_SHORT
    return KIND_SUBSTANTIVE


def _is_short(text: str, tokens: list[str]) -> bool:
    """Back-compat predicate: everything that skips the model path."""
    return _comment_kind(text, tokens) != KIND_SUBSTANTIVE


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


async def classify_comment(
    text: str,
    registry: Any,
    sentiment_override: str | None = None,
    _precomputed: tuple | None = None,
) -> dict:
    """Classify one comment's sentiment + emotion via the hybrid router.

    Returns {"sentiment", "sentiment_score", "emotion", "method", "kind",
    "keywords", "emotion_method"}.

    **Emotion is the free emoji+lexicon heuristic for every comment**, even in
    real mode where an emotion pipeline is loaded and used for the *post*
    (§5.13). That is a deliberate cost decision — running a transformer emotion
    head over 10k comments per post would dominate Stage 1 — but it was
    invisible: `emotion_breakdown` looked like model output. `emotion_method`
    now says which it is, so the chart can disclose it the way
    `method`/`provenance` do for sentiment. Stage 2 upgrades the emotion of the
    comments it re-labels, inside the stance call it already makes.

    ``sentiment_override`` forces a specific sentiment model on the model path
    (the fast emoji/lexicon path is model-independent).
    """
    tokens = _WORD_RE.findall(text or "")
    emotion = _fast_emotion(text or "", tokens)
    kind = _comment_kind(text or "", tokens)

    if kind != KIND_SUBSTANTIVE:
        label, score = _fast_classify(text or "", tokens)
        return {
            "sentiment": label,
            "sentiment_score": score,
            "emotion": emotion,
            # Emoji-only reactions are tagged distinctly from short text
            # comments: they carry real crowd signal (❤️ and 🤬 both mean
            # something) so they are kept, but they are excluded from every LLM
            # batch — there is no text in them for an LLM to read.
            "method": "emoji" if kind == KIND_EMOJI else "fast",
            "kind": kind,
            # Emotion is always the free heuristic at Stage 1 — never a model.
            "emotion_method": "heuristic",
            "keywords": [],
        }

    if _precomputed is not None:
        # Already scored in a batched forward pass by analyze_comments (§5.11).
        label, score, _conf, engine = _precomputed
    else:
        label, score, _conf, engine = await analyze_sentiment_engine(
            text, registry, sentiment_override
        )
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
        # "model" only when a transformer actually ran. This used to be
        # hardcoded regardless of MODEL_STUB_MODE, so `method_breakdown` claimed
        # 8,513 model inferences in runs where zero models were loaded.
        "method": engine,
        "kind": kind,
        # Sentiment may be a model; emotion is the heuristic either way (§5.13).
        "emotion_method": "heuristic",
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
    """Aggregate keywords across all comments, weighted by likes (§5.13).

    Weighting is **sub-linear** (``1 + log10(1 + likes)``), not raw likes. With
    raw likes a single 946-like comment outweighed 946 ordinary ones, so
    "themes" was effectively the keyword list of the most-liked comment wearing
    the label of a cross-comment aggregate. This corpus is heavily skewed — 64%
    of comments have zero likes, the mean is 4.0 and the max is 946 — which is
    exactly the shape where a linear weight collapses to a top-1 selector.

    Log weighting keeps engagement meaningful (a popular comment still counts
    for more) without letting one comment dictate the answer: 946 likes is worth
    ~4x an unliked comment, not 946x.
    """
    counter: Counter[str] = Counter()
    for item in analyzed_comments:
        likes = max(0, int(item.get("likes") or 0))
        weight = 1.0 + math.log10(1 + likes)
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

def _apply_labels(batch: list[dict], labels: list) -> int:
    """Merge a batch's LLM labels back onto its records, index-aligned.

    ``classify_comments_llm`` returns one label per input in order, and the
    whole merge rests on that alignment — a shifted list would attach every
    comment's sentiment to its neighbour, silently and unrecoverably. Assert it
    rather than trusting ``zip`` to truncate the mismatch away.
    """
    if len(labels) != len(batch):
        raise ValueError(
            f"LLM returned {len(labels)} labels for {len(batch)} comments — "
            "index alignment is the merge contract; refusing to mislabel"
        )
    applied = 0
    for r, label in zip(batch, labels):
        if not isinstance(label, dict):
            continue
        r["sentiment"] = label["sentiment"]
        r["sentiment_score"] = label["sentiment_score"]
        r["emotion"] = label["emotion"]
        if label.get("keywords"):
            r["keywords"] = label["keywords"]
        r["method"] = "llm"
        applied += 1
    return applied


async def _llm_upgrade_comments(
    records: list[dict],
    registry: Any,
    backend_override: str | None,
    progress_cb: Any = None,
) -> int:
    """Re-label substantive comments via the `stage1` LLM (in place). Returns #labelled.

    **Every non-emoji comment reaches the LLM by default** (§6.3): the cap is 0.
    Emoji-only reactions are excluded — there is no text in them to read — and
    short comments keep the free heuristic. Coverage of the LLM pass is
    therefore "all substantive comments", not "the top 60 by likes".

    Batches run through a bounded-concurrency queue rather than the old strictly
    sequential loop, which is what made raising the cap impractical: with the
    caps lifted, a 2,857-comment thread is ~115 batches, and running those one
    after another stalls the post for minutes.

    Each batch retries independently, so one bad batch does not cost the rest.
    A batch that exhausts its retries leaves its comments on the Stage-1
    heuristic/stub label — degraded, but labelled and counted as such.

    ``progress_cb(done, total)`` is awaited after each batch so the Trace tab can
    show ``batch k/N`` instead of appearing hung.
    """
    llm = registry.get_llm_client()
    if llm is None:
        return 0

    substantive = [r for r in records if r.get("kind") == KIND_SUBSTANTIVE]
    if not substantive:
        return 0
    if 0 < _LLM_COMMENT_MAX < len(substantive):
        # A positive cap still selects the most-engaged comments — a demo knob,
        # not the default.
        targets = sorted(
            substantive, key=lambda r: int(r.get("likes") or 0), reverse=True
        )[:_LLM_COMMENT_MAX]
        logger.info(
            "stage1 LLM comment cap active: %s of %s substantive comments "
            "(STAGE1_LLM_COMMENT_MAX=%s; 0 means no cap)",
            len(targets), len(substantive), _LLM_COMMENT_MAX,
        )
    else:
        targets = substantive

    batches = [
        targets[i : i + _LLM_COMMENT_BATCH]
        for i in range(0, len(targets), _LLM_COMMENT_BATCH)
    ]
    total = len(batches)
    semaphore = asyncio.Semaphore(_LLM_CONCURRENCY)
    done = 0
    labelled = 0
    lock = asyncio.Lock()

    async def _run_batch(index: int, batch: list[dict]) -> int:
        nonlocal done, labelled
        async with semaphore:
            for attempt in range(_LLM_BATCH_RETRIES + 1):
                try:
                    labels = await classify_comments_llm(
                        [r["text"] for r in batch], llm, backend_override
                    )
                    applied = _apply_labels(batch, labels)
                    break
                except Exception as exc:
                    if attempt < _LLM_BATCH_RETRIES:
                        logger.warning(
                            "stage1 LLM batch %s/%s failed (attempt %s), retrying: %s",
                            index + 1, total, attempt + 1, exc,
                        )
                        continue
                    logger.warning(
                        "stage1 LLM batch %s/%s failed after %s attempts, "
                        "leaving %s comments on their heuristic label: %s",
                        index + 1, total, attempt + 1, len(batch), exc,
                    )
                    applied = 0
        async with lock:
            done += 1
            labelled += applied
            current = done
        if progress_cb is not None:
            try:
                await progress_cb(current, total)
            except Exception as exc:  # progress must never break analysis
                logger.debug("comment batch progress callback failed: %s", exc)
        return applied

    await asyncio.gather(
        *(_run_batch(i, b) for i, b in enumerate(batches)), return_exceptions=True
    )
    logger.info(
        "stage1 LLM comment pass: %s/%s comments labelled across %s batches "
        "(batch=%s, concurrency=%s)",
        labelled, len(targets), total, _LLM_COMMENT_BATCH, _LLM_CONCURRENCY,
    )
    return labelled


async def analyze_comments(
    comments: list[dict],
    registry: Any,  # ModelRegistry — avoid circular import
    sentiment_override: str | None = None,
    backend_override: str | None = None,
    progress_cb: Any = None,
) -> dict:
    """Analyse all embedded comments and return a comment_analysis dict.

    Parameters
    ----------
    comments:
        The raw list from the upstream payload (each element has at least
        "id", "text", "likes", and optionally "authorUsername").
    registry:
        The shared ModelRegistry instance.
    progress_cb:
        Optional ``async (done, total) -> None`` called after each LLM batch, so
        a caller with a Redis handle can publish ``batch k/N`` to the trace
        without this module needing to know about Redis.

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
            "sentiment_breakdown_substantive": {"positive": 0, "negative": 0, "neutral": 0},
            "reaction_only": 0,
            "emotion_breakdown": _empty_emotion_breakdown(),
            "method_breakdown": {},
            "provenance": label_provenance({}),
            "themes": [],
            "top_keywords": [],
            "representative_comments": [],
            "comments": [],
        }

    # 1. Baseline pass — every comment gets an instant heuristic/stub label
    #    (full coverage), holding all fields in one record per comment.
    #
    # Substantive comments are sentiment-scored in BATCHES (§5.11): one forward
    # pass per model instead of one per comment. Short/emoji comments never
    # touch a model, so they are excluded from the batch entirely.
    kinds = [_comment_kind(c.get("text") or "") for c in comments]
    substantive_idx = [i for i, k in enumerate(kinds) if k == KIND_SUBSTANTIVE]
    batched: dict[int, tuple] = {}
    if substantive_idx:
        try:
            scored = await analyze_sentiment_batch(
                [comments[i].get("text") or "" for i in substantive_idx],
                registry,
                sentiment_override,
            )
            batched = dict(zip(substantive_idx, scored))
        except Exception as exc:
            # Fall back to the per-comment path; slower, same answers.
            logger.warning("batched comment sentiment failed, falling back: %s", exc)

    records: list[dict] = []
    for comment in comments:
        text = comment.get("text") or ""
        try:
            nlp = await classify_comment(
                text, registry, sentiment_override,
                _precomputed=batched.get(len(records)),
            )
        except Exception as exc:
            logger.warning("Comment NLP failed for id=%s: %s", comment.get("id"), exc)
            nlp = {
                "sentiment": "neutral",
                "sentiment_score": 0.0,
                "emotion": "neutral",
                # Not "fast" — nothing classified this comment. Tagging a
                # failure as a heuristic hides it inside a legitimate bucket.
                "method": "failed",
                "kind": _comment_kind(text),
                "emotion_method": "heuristic",
                "keywords": [],
            }
        records.append(
            {
                "id": comment.get("id", ""),
                "text": text,
                "likes": comment.get("likes", 0),
                "author": comment.get("authorUsername"),
                "parent_id": comment.get("parentId"),
                "sentiment": nlp.get("sentiment") or "neutral",
                "sentiment_score": nlp.get("sentiment_score", 0.0),
                "emotion": nlp.get("emotion") or "neutral",
                "keywords": nlp.get("keywords", []),
                "method": nlp.get("method", "fast"),
                "kind": nlp.get("kind") or _comment_kind(text),
                "emotion_method": nlp.get("emotion_method", "heuristic"),
            }
        )

    # 1b. Watchlist matching — free string work, every comment, every post.
    # The deterministic scorer fills in now; Stage 2 overwrites with LLM verdicts
    # for routed posts. Target stance lives in its OWN field and is never merged
    # into `sentiment` — a comment can be positive in tone while opposing a
    # listed target (stance_targets.md §2).
    targets = get_targets()
    if targets:
        matched_any: set[str] = set()
        for r in records:
            matches = targets.match(r["text"])
            if not matches:
                continue
            matched_any.update(m.target_id for m in matches)
            alias_by_target = {m.target_id: m.alias for m in matches}
            scored = score_comment_deterministic(r["text"], matches)
            for entry in scored:
                entry["alias"] = alias_by_target.get(entry["target"])
            r["target_stances"] = scored
        missing = unmatched_targets(targets, matched_any)
        if missing:
            # Almost certainly alias coverage, not absence of discussion.
            logger.info(
                "stance_targets_unmatched: %s matched nothing in this post — "
                "check alias coverage before reading it as silence",
                ", ".join(missing),
            )

    # 2. LLM upgrade pass (STAGE1_LLM=true) — re-label the top-N substantive
    #    comments via the stage1 LLM so they match the LLM engine. No-op otherwise.
    if registry.llm_mode:
        await _llm_upgrade_comments(records, registry, backend_override, progress_cb)

    # 3. Aggregate from the (possibly upgraded) records.
    sentiment_counts: dict[str, int] = {"positive": 0, "negative": 0, "neutral": 0}
    emotion_counts: dict[str, int] = _empty_emotion_breakdown()
    # Seeded empty, not with a zeroed "model" bucket: the breakdown should
    # report the methods that ran, not imply one that did not.
    method_counts: dict[str, int] = {}
    # Emoji-only reactions are real crowd signal (❤️ and 🤬 both mean something)
    # and are kept — but they are a different thing from a written opinion, so
    # they get their own count and the substantive comments get their own
    # series. A chart can then show "text opinion" and "emoji reactions"
    # separately instead of blending them into one indistinguishable bar.
    substantive_counts: dict[str, int] = {"positive": 0, "negative": 0, "neutral": 0}
    reaction_only = 0
    for r in records:
        s = r["sentiment"] if r["sentiment"] in sentiment_counts else "neutral"
        r["sentiment"] = s
        sentiment_counts[s] += 1
        if r.get("kind") == KIND_EMOJI:
            reaction_only += 1
        else:
            substantive_counts[s] += 1
        e = r["emotion"] if r["emotion"] in emotion_counts else "neutral"
        r["emotion"] = e
        emotion_counts[e] += 1
        m = r.get("method") or "fast"
        method_counts[m] = method_counts.get(m, 0) + 1

    themes = _extract_themes(records)
    top_keywords = _aggregate_top_keywords(records)
    representative = _select_representative(records)

    # The persisted per-comment records (one row per comment downstream).
    per_comment = [
        {
            "id": r["id"],
            "text": r["text"],
            "likes": r["likes"],
            "author": r["author"],
            "parent_id": r["parent_id"],
            "sentiment": r["sentiment"],
            "sentiment_score": r["sentiment_score"],
            "emotion": r["emotion"],
            "method": r["method"],
            "kind": r["kind"],
            # Emotion is the free heuristic at Stage 1 even in real mode; Stage 2
            # upgrades only the comments it re-labels (§5.13).
            "emotion_method": r.get("emotion_method", "heuristic"),
            # Own field, never folded into `sentiment` — see stance_targets.md §2.
            # Absent (not empty) when the comment mentions no listed target, so
            # "nobody discussed X" stays distinguishable from "all neutral on X".
            **({"target_stances": r["target_stances"]} if r.get("target_stances") else {}),
        }
        for r in records
    ]

    return {
        "analyzed": len(records),
        # coverage is 0.0 here; the worker fills the real value using
        # engagement.commentCount after this function returns.
        "coverage": 0.0,
        "sentiment_breakdown": sentiment_counts,
        # The same counts over written comments only — emoji-only reactions
        # excluded. This is the series to chart when the question is "what did
        # people SAY", as opposed to "how did the crowd react".
        "sentiment_breakdown_substantive": substantive_counts,
        "reaction_only": reaction_only,
        "emotion_breakdown": emotion_counts,
        "method_breakdown": method_counts,
        # Surfaced next to the breakdowns the way `coverage` is surfaced next to
        # the comment count, so "what produced these labels?" is answerable from
        # the same object that carries the labels.
        "provenance": label_provenance(method_counts),
        # Per-target rollup — the actual product output (stance_targets.md §4.3).
        # Absent targets are omitted, not zero-filled.
        **(
            {"target_stances": aggregate_target_stances(per_comment, targets)}
            if targets else {}
        ),
        "themes": themes,
        "top_keywords": top_keywords,
        "representative_comments": representative,
        # Per-comment sentiment for every embedded comment (full coverage of
        # the stored sample). Bulky — the API strips it from list responses.
        "comments": per_comment,
    }

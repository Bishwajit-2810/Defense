"""Sentiment fusion — combine text, image, and reaction signals.

Golden rule 8 (from data_contract.md §4):
  - Text-only post (no image):          overall = text_sentiment (weight 1.0)
  - Image post with caption:            text × 0.6  +  image × 0.4
  - Image post, null caption:           image × 0.7  +  OCR-text × 0.3
  - Cross-check reactionBreakdown:      if SAD + ANGRY > 40 % of total reactions
    and overall is currently neutral or positive, nudge toward negative.

Returns (overall_sentiment: str, sentiment_score: float).
"""

from __future__ import annotations

# Threshold for the reaction-breakdown nudge
_NEGATIVE_REACTION_THRESHOLD = 0.40

_NEGATIVE_TYPES = {"sad", "angry"}


def _label_to_score(label: str | None, score: float | None) -> float:
    """Normalise a (label, score) pair to a float in [-1, 1].

    If score is already provided and non-zero it is used directly.
    Otherwise the label is mapped to a sign and a magnitude of 0.5.
    """
    if score is not None and score != 0.0:
        return float(score)
    if label == "positive":
        return 0.5
    if label == "negative":
        return -0.5
    return 0.0


def _score_to_label(score: float) -> str:
    if score > 0.1:
        return "positive"
    if score < -0.1:
        return "negative"
    return "neutral"


def _negative_reaction_ratio(reaction_breakdown: dict) -> float:
    """Return the fraction of reactions that are SAD or ANGRY."""
    if not reaction_breakdown:
        return 0.0
    total = sum(reaction_breakdown.values())
    if total == 0:
        return 0.0
    neg = sum(
        v for k, v in reaction_breakdown.items() if k.lower() in _NEGATIVE_TYPES
    )
    return neg / total


def fuse_sentiment(
    text_result: dict | None,
    image_result: dict | None,
    reaction_breakdown: dict,
    caption: str | None,
) -> tuple[str, float]:
    """Fuse text + image + reaction signals into a single (label, score) pair.

    Parameters
    ----------
    text_result:
        Output of analyze_text on the caption (or None when there is no
        caption).  May also carry OCR-derived sentiment when caption is null.
    image_result:
        Output of analyze_image (or None for text-only posts).
    reaction_breakdown:
        The raw reactionBreakdown dict from the upstream payload.
    caption:
        The raw caption string, used only to distinguish "null caption" posts
        from posts that have text.

    Returns
    -------
    (overall_sentiment: str, sentiment_score: float)
    """
    has_image = image_result is not None
    has_caption = caption is not None and caption.strip() != ""

    text_score = (
        _label_to_score(
            text_result.get("sentiment"),
            text_result.get("sentiment_score"),
        )
        if text_result
        else 0.0
    )
    img_score = (
        _label_to_score(
            image_result.get("image_sentiment"),
            image_result.get("image_sentiment_score"),
        )
        if image_result
        else 0.0
    )

    # -----------------------------------------------------------------------
    # Compute weighted fused score
    # -----------------------------------------------------------------------

    if not has_image:
        # Text-only post
        fused = text_score

    elif has_caption:
        # Image + caption → text 60 %, image 40 %
        fused = 0.6 * text_score + 0.4 * img_score

    else:
        # Image post with null caption:
        # We use text_result as the OCR-derived text path.
        # image 70 %, OCR-text 30 %
        fused = 0.7 * img_score + 0.3 * text_score

    fused = round(fused, 4)

    # -----------------------------------------------------------------------
    # Reaction-breakdown cross-check / nudge
    # -----------------------------------------------------------------------

    neg_ratio = _negative_reaction_ratio(reaction_breakdown)
    if neg_ratio > _NEGATIVE_REACTION_THRESHOLD:
        current_label = _score_to_label(fused)
        if current_label in ("neutral", "positive"):
            # Nudge toward negative proportionally to excess above threshold
            excess = neg_ratio - _NEGATIVE_REACTION_THRESHOLD
            nudge = min(excess * 0.5, 0.2)   # cap nudge at -0.2
            fused = round(fused - nudge, 4)

    overall = _score_to_label(fused)
    return overall, fused

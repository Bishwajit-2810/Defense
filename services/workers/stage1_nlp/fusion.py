"""Sentiment fusion — combine text, image, and reaction signals.

Golden rule 8 (from data_contract.md §4):
  - Text-only post (no image):          overall = text_sentiment (weight 1.0)
  - Image post with caption:            text × 0.6  +  image × 0.4
  - Image post, null caption:           image × 0.7  +  OCR-text × 0.3
  - Cross-check reactionBreakdown:      if SAD + ANGRY > 40 % of total reactions
    and overall is currently neutral or positive, nudge toward negative.

**Weights are renormalised over the terms that actually carry a signal.** The
weights above used to be applied unconditionally, so a term that was
structurally absent still consumed its share:

  - a null-caption post scored ``0.7 × image + 0.3 × 0.0`` — the OCR term was
    always zero because the worker analysed the (null) caption rather than the
    OCR text — so every image-only post's score was silently multiplied by 0.7,
    which can push a weak negative (−0.15 → −0.105) across the ±0.1 neutral
    boundary and flip its label;
  - in any configuration where the image produced no verdict (stub mode, a
    failed fetch, no CLIP/SigLIP), a captioned post scored ``0.6 × text +
    0.4 × 0.0``, shrinking a real text signal by 40% toward neutral.

A term is included only when it is a genuine model output — for images that
means ``status == "ok"`` (see vision_analyzer's STATUS_* constants), not merely
"a dict was returned". When no term qualifies the fused score is 0.0/neutral,
which is the honest answer for a post nothing could be measured on.

Returns (overall_sentiment: str, sentiment_score: float).
"""

from __future__ import annotations

from .vision_analyzer import USABLE_STATUSES

# Threshold for the reaction-breakdown nudge
_NEGATIVE_REACTION_THRESHOLD = 0.40

_NEGATIVE_TYPES = {"sad", "angry"}

# Golden rule 8 weights, applied only to the terms that are actually present.
_W_TEXT_WITH_IMAGE = 0.6
_W_IMAGE_WITH_CAPTION = 0.4
_W_IMAGE_NULL_CAPTION = 0.7
_W_OCR_NULL_CAPTION = 0.3


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


def image_has_signal(image_result: dict | None) -> bool:
    """True when the image term is a real model verdict rather than an absence.

    A result with no ``status`` predates the STATUS_* contract; it is trusted
    only if it actually carries a sentiment value.
    """
    if not image_result:
        return False
    status = image_result.get("status")
    if status is None:
        return image_result.get("image_sentiment") is not None
    return status in USABLE_STATUSES


def _text_has_signal(text_result: dict | None) -> bool:
    """True when the text term carries a label the analyzer actually produced.

    ``_empty_result()`` (a null caption) has ``sentiment: None`` and must not
    consume its weight.
    """
    return bool(text_result) and text_result.get("sentiment") is not None


def _weighted(terms: list[tuple[float, float]]) -> float:
    """Weighted mean over the present terms, renormalised by their weights."""
    total_weight = sum(w for w, _ in terms)
    if total_weight <= 0:
        return 0.0
    return sum(w * s for w, s in terms) / total_weight


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
    has_caption = caption is not None and caption.strip() != ""
    image_signal = image_has_signal(image_result)
    text_signal = _text_has_signal(text_result)

    text_score = (
        _label_to_score(
            text_result.get("sentiment"),
            text_result.get("sentiment_score"),
        )
        if text_signal
        else 0.0
    )
    img_score = (
        _label_to_score(
            image_result.get("image_sentiment"),
            image_result.get("image_sentiment_score"),
        )
        if image_signal
        else 0.0
    )

    # -----------------------------------------------------------------------
    # Compute weighted fused score over the terms that are actually present
    # -----------------------------------------------------------------------

    terms: list[tuple[float, float]] = []
    if not image_signal:
        # Text-only post, or an image that produced no verdict. Either way the
        # text carries the whole signal — it is not scaled down by a weight
        # reserved for a term that does not exist.
        if text_signal:
            terms.append((1.0, text_score))
    elif has_caption:
        # Image + caption → text 60 %, image 40 %
        if text_signal:
            terms.append((_W_TEXT_WITH_IMAGE, text_score))
        terms.append((_W_IMAGE_WITH_CAPTION, img_score))
    else:
        # Image post with null caption: image 70 %, OCR-derived text 30 %.
        # `text_result` is the OCR text path here (the worker analyses the OCR
        # output when there is no caption), so the OCR term is real whenever
        # the image carried readable text.
        terms.append((_W_IMAGE_NULL_CAPTION, img_score))
        if text_signal:
            terms.append((_W_OCR_NULL_CAPTION, text_score))

    fused = round(_weighted(terms), 4)

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

"""
Routing rules for the defense system router.

Determines whether a post's partial_result needs Stage-2 LLM processing
or can proceed directly to the assembler.

Golden Rule: Only single-digit % of posts should reach Stage-2.
"""

from __future__ import annotations


def should_use_llm(partial_result: dict, options: dict) -> tuple[bool, list[str]]:
    """Evaluate routing rules and decide if Stage-2 LLM is needed.

    Parameters
    ----------
    partial_result:
        The intermediate analysis dict produced by Stage-1 NLP.
    options:
        Per-request options dict (e.g. {"want_summary": True}).

    Returns
    -------
    (use_llm, reasons)
        use_llm  – True when ANY rule fires.
        reasons  – Human-readable list of which rules fired.
    """
    reasons: list[str] = []

    # Rule 1: uncertain classification (low confidence)
    overall_confidence = partial_result.get("overall_confidence")
    if overall_confidence is not None and overall_confidence < 0.65:
        reasons.append(
            f"low_confidence:{overall_confidence:.3f} (threshold 0.65)"
        )

    # Rule 2: could not determine post type
    if partial_result.get("post_type") is None:
        reasons.append("post_type:None (unclassified)")

    # Rule 3: caller explicitly requested a summary
    if options.get("want_summary", False):
        reasons.append("want_summary:requested by caller")

    # Rule 4: image post with no image sentiment analysed yet
    photo_urls = partial_result.get("photo_urls") or []
    has_photos = bool(photo_urls)
    image_sentiment_missing = partial_result.get("image_sentiment") is None
    if has_photos and image_sentiment_missing:
        reasons.append("image_no_sentiment:photo post missing image_sentiment")

    # Rule 5: high toxicity — needs LLM review
    toxicity_score = partial_result.get("toxicity_score")
    if toxicity_score is not None and toxicity_score > 0.7:
        reasons.append(f"high_toxicity:{toxicity_score:.3f} (threshold 0.7)")

    # Rule 6: long mixed-language (complex Banglish) post
    caption = partial_result.get("caption") or ""
    language = partial_result.get("language") or ""
    if len(caption) > 1500 and language == "mixed":
        reasons.append(
            f"long_mixed_text:length={len(caption)},language={language}"
        )

    return bool(reasons), reasons


def get_task_flags(partial_result: dict, options: dict) -> dict:
    """Derive task flags that tell Stage-2 what to compute.

    Parameters
    ----------
    partial_result:
        The intermediate analysis dict produced by Stage-1 NLP.
    options:
        Per-request options dict.

    Returns
    -------
    dict with keys:
        want_summary  – Generate a 2-3 sentence post summary.
        want_post_type – Classify the semantic post type via LLM.
        want_insight   – Refine topics/intents and produce a one-line insight.
        target_lang    – Language code for the summary (None = auto).
    """
    # Every post gets a summary: the product requirement is a summary +
    # sentiment for every post, not only image / low-confidence ones. Callers
    # can still pass want_summary explicitly, but the default is now True.
    want_summary: bool = options.get("want_summary", True)

    # Need post_type when it is absent or explicitly requested
    want_post_type: bool = (
        partial_result.get("post_type") is None
        or bool(options.get("want_post_type", False))
    )

    # Request insight when topics/intents are thin or explicitly asked for
    topics = partial_result.get("topics") or []
    want_insight: bool = (
        len(topics) < 2
        or bool(options.get("want_insight", False))
    )

    # Honour an explicit target language; fall back to post's detected language
    target_lang: str | None = options.get("target_lang") or partial_result.get("language") or None

    return {
        "want_summary": want_summary,
        "want_post_type": want_post_type,
        "want_insight": want_insight,
        "target_lang": target_lang,
    }

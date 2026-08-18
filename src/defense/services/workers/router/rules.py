"""
Routing rules for the defense system router.

Determines whether a post's partial_result needs Stage-2 LLM processing
or can proceed directly to the assembler.

Design target: keep the share of posts reaching Stage 2 low, so cheap Stage-1
NLP carries the bulk of the work. That share is a *measured* quantity, not an
assumption — the router exports it as ``stats:llm_routed / stats:total_processed``
and every rule below logs the field it read (router.py ``router_rules_evaluated``).
Whatever the number turns out to be, it must come from those counters.

Every rule reads its inputs from the Stage-1 result dict, so the names here have
to match what ``stage1_nlp/worker._build_result`` emits. Two of them did not, and
a third read a placeholder as a verdict, which routed 100% of posts to Stage 2:

  * Rule 1 read ``overall_confidence``; Stage 1 emits ``confidence``. The
    ``is not None`` guard therefore always short-circuited and the confidence
    gate never ran — a post with ``confidence: 0.0`` passed a 0.65 threshold.
  * Rule 2 read ``post_type is None``, which Stage 1 set deliberately as a
    "Stage 2 will fill this" placeholder. It fired for every post, and one rule
    firing is enough to route.
  * Rules 4 and 6 read ``photo_urls`` / ``caption``, which lived only in
    ``normalized_post``, never in the Stage-1 result the router passes in.

The readers below accept every shape the three stages emit (Stage 1's flat
``confidence``, the assembler's nested ``confidence.overall``, the legacy
``overall_confidence``) so a future rename cannot silently disable a gate again.
"""

from __future__ import annotations

import os
from defense.libs.common.config import get_settings
config = get_settings()

# Thresholds. Env-overridable because §7 of the assessment wants an accuracy-vs-cost
# sweep over CONFIDENCE_THRESHOLD, and a sweep needs a knob that is not a literal.
CONFIDENCE_THRESHOLD: float = float(config.router_confidence_threshold)
POST_TYPE_CONFIDENCE_THRESHOLD: float = float(
    config.router_post_type_confidence_threshold
)
TOXICITY_THRESHOLD: float = float(config.router_toxicity_threshold)
LONG_TEXT_CHARS: int = int(config.router_long_text_chars)

#: Whether an unqualified request should route to Stage 2 just to be summarised.
#: False: Stage 1 writes the summary for every post, so wanting a summary is not
#: by itself a reason to pay for the bigger model. Set ROUTER_SUMMARY_ROUTES=true
#: to go back to summarising everything on Stage 2 — and expect the routed share
#: to become 100%, because this rule alone is enough to route.
SUMMARY_ROUTES_TO_STAGE2: bool = bool(config.router_summary_routes)

#: How many comments per post Stage 2 analyses, ranked by reaction count.
#: **0 (the default) = no cap: every comment with text.** A positive value is a
#: speed/cost guard that leaves the rest of the thread with no model verdict at
#: all. See :func:`select_comments_for_stage2`.
COMMENT_TOP_N: int = int(config.router_comment_top_n)
#: Minimum word tokens required for a comment to be eligible for Stage 2 analysis (>2 words = 3).
COMMENT_MIN_WORDS: int = int(getattr(config, "router_comment_min_words", 0))

import re
_WORD_RE = re.compile(r"[0-9A-Za-zঀ-৿]+")


def _word_count(comment: dict) -> int:
    """Number of alphanumeric/Bangla word tokens in the comment text."""
    text = str(comment.get("text") or comment.get("comment_text") or "")
    return len(_WORD_RE.findall(text))

#: Comment kinds with nothing for a model to read. They are never ranked for a
#: slot — a 900-like ❤️ would take one and produce no verdict — and they are not
#: marked either, so Stage 2 keeps accounting for them exactly as it does today
#: (``escalation_reason: "no_text"``, counted in ``reaction_only``).
TEXTLESS_KINDS: frozenset[str] = frozenset({"emoji", "filtered", "link"})


# ---------------------------------------------------------------------------
# Field readers — tolerate every shape the pipeline emits for one concept
# ---------------------------------------------------------------------------

def _as_float(value: object) -> float | None:
    """Coerce to float, or None when the value is missing / not a number."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def read_overall_confidence(partial_result: dict) -> float | None:
    """Stage-1 overall confidence, whichever name/shape it arrived under.

    Accepts the flat ``confidence`` Stage 1 emits, the nested
    ``confidence: {"overall": …}`` the assembler emits, and the legacy
    ``overall_confidence``. None means genuinely absent.
    """
    conf = partial_result.get("confidence")
    if isinstance(conf, dict):
        conf = conf.get("overall")
    value = _as_float(conf)
    if value is None:
        value = _as_float(partial_result.get("overall_confidence"))
    return value


def read_photo_count(partial_result: dict) -> int:
    """Number of images on the post, from the flat list or the analysis block."""
    photo_urls = partial_result.get("photo_urls")
    if isinstance(photo_urls, (list, tuple)):
        return len(photo_urls)
    image_analysis = partial_result.get("image_analysis") or {}
    if isinstance(image_analysis, dict):
        count = _as_float(image_analysis.get("image_count"))
        if count is not None:
            return int(count)
    return 0


def read_image_sentiment(partial_result: dict) -> object | None:
    """The image sentiment verdict, flat or nested under image_analysis."""
    sentiment = partial_result.get("image_sentiment")
    if sentiment is not None:
        return sentiment
    image_analysis = partial_result.get("image_analysis") or {}
    images = image_analysis.get("images") if isinstance(image_analysis, dict) else None
    if isinstance(images, list) and images and isinstance(images[0], dict):
        return images[0].get("sentiment")
    return None


def read_text_length(partial_result: dict) -> int:
    """Caption length in characters, from ``caption_chars`` or a raw ``caption``."""
    chars = _as_float(partial_result.get("caption_chars"))
    if chars is not None:
        return int(chars)
    return len(partial_result.get("caption") or "")


def _is_code_mixed(partial_result: dict) -> bool:
    """True for Banglish / mixed-script text.

    Stage 1 records this as ``script == "mixed"`` or ``is_banglish``; the old rule
    compared ``language == "mixed"``, which Stage 1 never emits (language is a
    code like ``bn``), so the rule could not fire even given a caption.
    """
    return (
        partial_result.get("script") == "mixed"
        or bool(partial_result.get("is_banglish"))
        or partial_result.get("language") == "mixed"
    )


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------

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

    # Rule 1: uncertain classification (low confidence). An absent confidence is
    # treated as uncertain rather than ignored — silently skipping the gate is
    # exactly how it came to be inert.
    overall_confidence = read_overall_confidence(partial_result)
    if overall_confidence is None:
        reasons.append("no_confidence:stage-1 reported none")
    elif overall_confidence < CONFIDENCE_THRESHOLD:
        reasons.append(
            f"low_confidence:{overall_confidence:.3f} (threshold {CONFIDENCE_THRESHOLD})"
        )

    # Rule 2: post type unknown, or known with too little confidence to publish.
    post_type = partial_result.get("post_type")
    if post_type is None:
        reasons.append("post_type:None (unclassified)")
    else:
        post_type_confidence = _as_float(partial_result.get("post_type_confidence"))
        # A caller that supplies a post_type but no confidence for it is taken at
        # its word; only an explicit low number escalates.
        if (
            post_type_confidence is not None
            and post_type_confidence < POST_TYPE_CONFIDENCE_THRESHOLD
        ):
            reasons.append(
                f"low_post_type_confidence:{post_type_confidence:.3f} "
                f"({post_type}, threshold {POST_TYPE_CONFIDENCE_THRESHOLD})"
            )

    # Rule 3: the caller explicitly asked for a Stage-2 summary.
    #
    # This defaulted to True for a while, which made the gate a pass-through:
    # one rule firing is enough to route, so every post went to Stage 2 and
    # `estimated_llm_share` became the constant 1.0. The product requirement it
    # was serving — every post gets a summary — is still met, but by Stage 1,
    # which now writes one itself (stage1_nlp/worker.py) and hands it to the
    # assembler. Stage 2 re-summarises only when someone asks for the better
    # model, so the summary is no longer computed twice per post.
    if options.get("want_summary", SUMMARY_ROUTES_TO_STAGE2):
        reasons.append("want_summary:requested by caller")

    # Rule 4: image post with no image sentiment analysed yet
    photo_count = read_photo_count(partial_result)
    if photo_count and read_image_sentiment(partial_result) is None:
        reasons.append(
            f"image_no_sentiment:{photo_count} photo(s) missing image_sentiment"
        )

    # Rule 5: high toxicity — needs LLM review
    toxicity_score = _as_float(partial_result.get("toxicity_score"))
    if toxicity_score is not None and toxicity_score > TOXICITY_THRESHOLD:
        reasons.append(
            f"high_toxicity:{toxicity_score:.3f} (threshold {TOXICITY_THRESHOLD})"
        )

    # Rule 6: long mixed-language (complex Banglish) post
    text_length = read_text_length(partial_result)
    if text_length > LONG_TEXT_CHARS and _is_code_mixed(partial_result):
        reasons.append(
            f"long_mixed_text:length={text_length},"
            f"script={partial_result.get('script')},"
            f"is_banglish={bool(partial_result.get('is_banglish'))}"
        )

    return bool(reasons), reasons


def _likes(comment: dict) -> int:
    """The comment's reaction count, 0 when absent or unparseable."""
    try:
        return int(comment.get("likes") or 0)
    except (TypeError, ValueError):
        return 0


def select_comments_for_stage2(
    partial_result: dict,
    options: dict | None = None,
    *,
    limit: int | None = None,
    min_words: int | None = None,
    dedup: bool = True,
) -> dict:
    """Pick the comments Stage 2 will analyse: deduplicated top-N by reaction count.

    Filters out textless/emoji-only comments, comments below min_words threshold,
    and duplicate/repeat comments across the thread so Stage 2 receives fresh,
    unique, substantive comments.
    """
    from defense.libs.comment_groups import normalize

    comment_analysis = partial_result.get("comment_analysis") or {}
    comments = comment_analysis.get("comments") or []
    if not comments:
        return {}

    if limit is None:
        requested = (options or {}).get("comment_top_n")
        limit = int(requested) if requested is not None else COMMENT_TOP_N

    if min_words is None:
        req_min = (options or {}).get("comment_min_words")
        min_words = int(req_min) if req_min is not None else COMMENT_MIN_WORDS

    # Step 1: Collect non-textless comments that meet the min_words threshold
    candidates: list[tuple[int, dict]] = []
    for i, c in enumerate(comments):
        if c.get("kind") in TEXTLESS_KINDS:
            continue
        if min_words and min_words > 0 and _word_count(c) < min_words:
            c["stage2_selected"] = False
            c["stage2_skip_reason"] = "below_min_words"
            continue
        candidates.append((i, c))

    # Sort candidates by likes descending first so the most-liked occurrence is kept
    candidates.sort(key=lambda pair: (-_likes(pair[1]), pair[0]))

    # Step 2: Deduplicate identical/near-duplicate comment texts
    seen_keys: dict[str, str] = {}
    ranked: list[tuple[int, dict]] = []
    duplicates_count = 0

    for i, c in candidates:
        text = str(c.get("text") or c.get("comment_text") or "")
        norm_key = normalize(text)
        if not norm_key:
            c["stage2_selected"] = False
            c["stage2_skip_reason"] = "no_text"
            continue

        if dedup and norm_key in seen_keys:
            # Duplicate / repeat comment — skip analysis for repeat occurrences
            c["stage2_selected"] = False
            c["stage2_skip_reason"] = "duplicate"
            c["is_duplicate"] = True
            c["duplicate_of"] = seen_keys[norm_key]
            duplicates_count += 1
            continue

        rep_id = str(c.get("id") or i)
        seen_keys[norm_key] = rep_id
        ranked.append((i, c))

    # Step 3: Apply top-N limit to unique, substantive comments
    if limit and limit > 0:
        selected, skipped = ranked[:limit], ranked[limit:]
    else:
        selected, skipped = ranked, []

    for _, comment in selected:
        comment["stage2_selected"] = True
        comment.pop("stage2_skip_reason", None)
    for _, comment in skipped:
        comment["stage2_selected"] = False
        comment["stage2_skip_reason"] = "below_top_n"

    record = {
        "strategy": "top_reactions",
        "limit": limit if limit and limit > 0 else 0,
        "total": len(comments),
        "eligible": len(ranked),
        "selected": len(selected),
        "skipped": len(skipped),
        "duplicates": duplicates_count,
        # The reaction count of the lowest-ranked comment that made the cut, so
        # the cutoff is inspectable without re-sorting the thread.
        "cutoff_likes": _likes(selected[-1][1]) if selected else 0,
        "top_likes": _likes(selected[0][1]) if selected else 0,
    }
    comment_analysis["stage2_selection"] = record
    return record


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
    # Every post gets a summary — but Stage 1 already wrote one, so Stage 2
    # only writes another when the caller explicitly asks for the bigger model,
    # or when Stage 1 came back empty (its LLM was off, or its call failed).
    # Defaulting this to True meant every routed post paid for a second summary
    # that overwrote an identical first one.
    # An explicit False always wins — a caller that opted out must not be
    # overridden by the "Stage 1 has no summary" fallback.
    stage1_summary = (partial_result.get("post_summary") or "").strip()
    is_valid_stage1_summary = bool(stage1_summary) and len(stage1_summary) >= 6
    requested = options.get("want_summary")
    want_summary: bool = bool(requested) if requested is not None else not is_valid_stage1_summary

    # Need post_type when Stage 1 could not determine one, when it is not
    # confident enough to publish, or when the caller asks. Re-running the LLM
    # classifier on a post Stage 1 already typed confidently is a call paid for
    # nothing — this mirrors Rule 2 above.
    post_type = partial_result.get("post_type")
    post_type_confidence = _as_float(partial_result.get("post_type_confidence"))
    want_post_type: bool = (
        post_type is None
        or (
            post_type_confidence is not None
            and post_type_confidence < POST_TYPE_CONFIDENCE_THRESHOLD
        )
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

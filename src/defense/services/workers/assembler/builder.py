"""
Result builder for the defense assembler.

Merges normalized_post, stage1_result, and optional stage2_result into a
single canonical AnalysisResult document, then validates it against the
output JSON Schema before returning.

Golden rule: EVERY object that leaves this module is schema-valid.
"""

from __future__ import annotations

import sys
import os

from defense.contracts.schemas.validator import assert_valid_output

# ---------------------------------------------------------------------------
# Schema version — bump when the output schema changes
# ---------------------------------------------------------------------------
# 1.1: per-comment emotion label + comment_analysis.emotion_breakdown
# 1.2: text_sentiment / image_sentiment carry their own {label, score} object
# 1.3: Stage-2's insight-task output (refined topics/intents + `insight`) is
#      merged instead of dropped — see _merge_stage2_labels below
SCHEMA_VERSION = "1.3"

#: Stage-1 provenance forwarded verbatim into `processing`. These answer "what
#: actually produced this result?", which is the question every §5 finding in
#: PROJECT_ASSESSMENT turned out to hinge on. Keep in sync with
#: services/workers/stage1_nlp/worker._build_result — the test enforces it.
_STAGE1_PROVENANCE_KEYS: tuple[str, ...] = (
    "unit",
    "nlp_engine",             # stub | models | llm
    "llm_role",
    "stub_mode",              # MODEL_STUB_MODE — read back by the assembler
    "degraded_components",    # real-mode components that fell back (§9.10)
    "vision_used",
    "vision_produced_signal",
    "vision_model",
    "vision_status",
    "model_versions",
)


def _merge_stage2_labels(
    stage1_result: dict,
    stage2_result: dict | None,
) -> tuple[list, list, str | None]:
    """Return ``(topics, intents, insight)`` with Stage-2's refinements applied.

    Stage-2's *insight* task (``stage2_llm/worker._run_insight``) spends a real
    LLM call — up to ``INSIGHT_MAX_TOKENS`` per routed post — producing
    ``refined_topics``, ``intents`` and a one-line ``insight``. This function
    used not to exist: ``topics`` and ``intents`` were read from
    ``stage1_result`` only, and ``insight`` was read nowhere and was absent from
    the output schema, so the entire task's output was paid for and then
    discarded before it reached the API, Postgres, ClickHouse or the dashboard.

    That is the same defect as the ``processing`` whitelist documented below,
    one field-group over: one component writes, the next reads a different set,
    and the mismatch degrades to a silent omission rather than an error.

    Precedence mirrors ``post_type``: Stage 2 wins when it actually produced
    something, otherwise Stage 1's value stands. An empty Stage-2 list means the
    LLM declined to refine, not that Stage 1's labels should be erased.
    """
    topics: list = stage1_result.get("topics", [])
    intents: list = stage1_result.get("intents", [])
    insight: str | None = None

    if stage2_result is not None:
        s2_topics = stage2_result.get("topics")
        if s2_topics:
            topics = s2_topics
        s2_intents = stage2_result.get("intents")
        if s2_intents:
            intents = s2_intents
        s2_insight = stage2_result.get("insight")
        if isinstance(s2_insight, str) and s2_insight.strip():
            insight = s2_insight.strip()

    return topics, intents, insight


def _norm_component_sentiment(value: object) -> dict | None:
    """Normalise a per-component sentiment to ``{"label", "score"}`` or ``None``.

    Stage 1 emits ``{label, score}``; tolerate a bare label string (→ score
    ``None``) and pass ``None`` through (null-caption → text_sentiment null;
    text-only → image_sentiment null).
    """
    if isinstance(value, dict):
        return {"label": value.get("label"), "score": value.get("score")}
    if isinstance(value, str):
        return {"label": value, "score": None}
    return None


#: Fields of a canonical result that describe THE POST, not its analysis. On the
#: near-duplicate reuse path these must all come from the new post — they are
#: facts about a specific upload, and two posts that share a caption share none
#: of them (PROJECT_ASSESSMENT §13.3).
_POST_IDENTITY_FIELDS: tuple[str, ...] = (
    "post_id", "campaign_id", "platform", "platform_post_id", "media_type",
    "post_text", "created_at", "scraped_at", "engagement", "reaction_breakdown",
    "baseline_sentiment", "baseline_viral_potential",
)


def build_reused_result(
    normalized_post: dict,
    source_result: dict,
    source_post_id: str,
    score: float,
) -> dict:
    """Compose a canonical result for a near-duplicate from a prior analysis.

    Near-duplicate reuse skips Stage 1 and Stage 2 for a post whose caption is
    within cosine threshold of one already analysed. Ingestion used to implement
    that as a **verbatim SQL row copy**, which carried three defects (§13.3):

    * the copied document kept the SOURCE's ``post_id``, ``engagement`` and
      ``reaction_breakdown`` — facts about a different upload. ``/v1/search``
      returns that document, so a hit on the new post described the old one;
    * ``embedding_is_stub`` was left out of the INSERT column list, so an
      honestly-flagged stub row produced a copy claiming to be a real vector;
    * only Postgres was written, so reused posts were missing from every
      ClickHouse analytics aggregate while still counting in Postgres-backed
      reports.

    Composing here instead means the reused post takes the same validation and
    the same three-store fan-out as any other, with the post's own facts intact.

    **The comment analysis is NOT reused.** A near-duplicate is a caption match;
    the two threads are different people saying different things, and carrying
    the source's per-comment labels over would be the §13.3 defect in its worst
    form — fabricated data about comments that were never read. The new post's
    thread is reported as unanalysed, which is true, and ``processing.reused_from``
    says why.
    """
    # The canonical result's field names are a superset of the ones
    # `build_canonical_result` reads out of a stage-1 and a stage-2 result
    # (`overall_sentiment`, `topics`, `post_summary`, `post_type`, …), so the
    # prior analysis can simply be fed back in as those inputs. That means the
    # identity, engagement and reaction fields are taken from `normalized_post`
    # by the same code that handles every other post — the reuse path does not
    # get its own, subtly different, notion of which fields belong to the post.
    analysis = dict(source_result)

    # The comment thread was never analysed: a near-duplicate is a CAPTION
    # match, and these are different people saying different things. Inheriting
    # the source's per-comment labels would be fabricated data about comments
    # nobody read.
    n_comments = len(normalized_post.get("comments") or [])
    analysis["comment_analysis"] = {
        "analyzed": 0,
        "coverage": 0.0,
        "sentiment_breakdown": {"positive": 0, "negative": 0, "neutral": 0},
        "provenance": {
            "note": (
                "not analysed — this post's analysis was reused from a "
                "near-duplicate caption; its comment thread is its own"
            ),
            "stored_comments": n_comments,
        },
    }

    # Only claim Stage 2 ran if it ran for the SOURCE. `llm_used` is derived from
    # whether a stage-2 result was supplied, so handing one over unconditionally
    # would report an LLM call that never happened — on a corpus where §5.8
    # counts exactly that.
    source_proc = source_result.get("processing") or {}
    stage2_input = analysis if source_proc.get("llm_used") else None

    result = build_canonical_result(normalized_post, analysis, stage2_input)

    # Provenance, so a reused result is never mistaken for a fresh one.
    processing = dict(result.get("processing") or {})
    processing["reused_from"] = {
        "source_post_id": source_post_id,
        "similarity": round(float(score), 4),
        "reused": "post_level_analysis_only",
    }
    # No stage ran for THIS post. Carrying the source's timings would corrupt any
    # latency figure taken over the corpus.
    processing["stage1_ms"] = 0
    processing["stage2_ms"] = 0
    result["processing"] = processing

    assert_valid_output(result)
    return result


def build_canonical_result(
    normalized_post: dict,
    stage1_result: dict,
    stage2_result: dict | None,
) -> dict:
    """Merge all sources into the canonical output schema.

    Parameters
    ----------
    normalized_post:
        The normalized post dict produced by the ingestion/normalization
        layer.  Must contain at minimum the fields listed in the field
        mapping below.
    stage1_result:
        Output from the Stage-1 NLP worker.
    stage2_result:
        Output from the Stage-2 LLM worker, or None when Stage 2 was
        skipped (high-confidence NLP-only path).

    Returns
    -------
    dict
        A schema-valid AnalysisResult document.

    Raises
    ------
    ValueError
        If the assembled document fails output schema validation.  The
        message includes every validation error found.
    """
    # ------------------------------------------------------------------
    # Core identity fields — always from normalized_post
    # ------------------------------------------------------------------
    post_id: str = normalized_post["post_id"]
    campaign_id: str = normalized_post["campaign_id"]
    platform: str = normalized_post["platform"]
    platform_post_id: str = normalized_post["platform_post_id"]
    media_type: str = normalized_post["media_type"]

    # ------------------------------------------------------------------
    # Language — from Stage 1
    # ------------------------------------------------------------------
    # Null-caption (image-only) posts may yield no detected language; the
    # schema requires a string, so fall back to "und" (undetermined).
    language: str = stage1_result.get("language") or "und"

    # ------------------------------------------------------------------
    # Original post content (caption) — from normalized_post, surfaced on the
    # canonical result so the dashboard detail view can show the source text
    # next to the LLM summary. None for image-only posts with no caption.
    # ------------------------------------------------------------------
    post_text: str | None = normalized_post.get("caption") or None

    # ------------------------------------------------------------------
    # Semantic post_type — Stage 2 wins when available, else Stage 1
    # ------------------------------------------------------------------
    post_type: str | None
    if stage2_result is not None:
        post_type = stage2_result.get("post_type")
        if post_type is None:
            post_type = stage1_result.get("post_type")
    else:
        post_type = stage1_result.get("post_type")

    # ------------------------------------------------------------------
    # Post summary — Stage 2 only (None when Stage 2 was skipped)
    # ------------------------------------------------------------------
    post_summary: str | None = None
    post_summary_lang: str | None = None
    post_summary_source: str | None = None
    post_summary_grounding: str | None = None
    # True when the summary still ended at the model's token ceiling after
    # LLMClient exhausted its continuation budget (§6.1). A short summary is
    # fine; a half one that claims to be whole is not.
    post_summary_truncated: bool = False

    if stage2_result is not None:
        post_summary = stage2_result.get("post_summary")
        post_summary_lang = stage2_result.get("post_summary_lang")
        post_summary_source = stage2_result.get("post_summary_source")
        post_summary_grounding = stage2_result.get("post_summary_grounding")
        post_summary_truncated = bool(stage2_result.get("post_summary_truncated"))

    # ------------------------------------------------------------------
    # Sentiment fields — Stage 1
    # ------------------------------------------------------------------
    overall_sentiment: str = stage1_result["overall_sentiment"]
    sentiment_score: float = stage1_result["sentiment_score"]
    # Per-component sentiments carry their OWN {label, score} (the caption's and
    # the image's scores differ from the fused overall_sentiment/sentiment_score).
    # Normalise to {label, score}: a bare label becomes {label, score:None};
    # null stays null (null-caption → text_sentiment null; text-only → image null).
    text_sentiment: dict | None = _norm_component_sentiment(stage1_result.get("text_sentiment"))
    image_sentiment: dict | None = _norm_component_sentiment(stage1_result.get("image_sentiment"))

    # ------------------------------------------------------------------
    # Baseline fields — ALWAYS from normalized_post, NEVER overwritten
    # ------------------------------------------------------------------
    baseline_sentiment: float | None = normalized_post.get("baseline_sentiment")
    baseline_viral_potential: float | None = normalized_post.get("baseline_viral_potential")

    # ------------------------------------------------------------------
    # Rich NLP fields — Stage 1
    # ------------------------------------------------------------------
    emotion: dict = stage1_result.get("emotion") or {"primary": "neutral", "scores": {}}
    if emotion.get("primary") is None:
        emotion = {**emotion, "primary": "neutral"}
    # topics / intents / insight: Stage-2's insight task refines the first two
    # and produces the third. All three used to be dropped here — see
    # _merge_stage2_labels.
    topics, intents, insight = _merge_stage2_labels(stage1_result, stage2_result)
    entities: list = stage1_result.get("entities", [])
    brand_mentions: list = stage1_result.get("brand_mentions", [])
    keywords: list = stage1_result.get("keywords", [])

    # ------------------------------------------------------------------
    # Safety scores — Stage 1
    # ------------------------------------------------------------------
    toxicity_score: float = stage1_result.get("toxicity_score") or 0.0
    hate_speech_score: float = stage1_result.get("hate_speech_score") or 0.0

    # ------------------------------------------------------------------
    # Engagement block — from normalized_post
    # ------------------------------------------------------------------
    np_eng = normalized_post.get("engagement", {})
    engagement: dict = {
        "comment_count": np_eng.get("comment_count", 0),
        "stored_comments": np_eng.get("stored_comments", 0),
        "total_reactions": np_eng.get("total_reactions", 0),
        "share_count": np_eng.get("share_count", 0),
    }

    reaction_breakdown: dict = normalized_post.get("reaction_breakdown", {})
    shares: list = normalized_post.get("shares", [])

    # ------------------------------------------------------------------
    # Image analysis — Stage 1 (null for text-only posts)
    # ------------------------------------------------------------------
    image_analysis: dict | None = stage1_result.get("image_analysis")

    # ------------------------------------------------------------------
    # Comment analysis — Stage 1
    # ------------------------------------------------------------------
    comment_analysis: dict = stage1_result["comment_analysis"]

    # ------------------------------------------------------------------
    # Confidence — Stage 1
    # ------------------------------------------------------------------
    # Stage-1 may emit a scalar overall-confidence or a per-field object; the
    # output schema + API response model want a full per-field object.
    _conf = stage1_result.get("confidence")
    if isinstance(_conf, dict):
        _overall = _conf.get("overall", 0.0)
        confidence: dict = {
            "overall": _overall,
            "sentiment": _conf.get("sentiment", _overall),
            "language": _conf.get("language", _overall),
            "topics": _conf.get("topics", _overall),
        }
    else:
        _overall = float(_conf) if _conf is not None else 0.0
        confidence = {
            "overall": _overall,
            "sentiment": _overall,
            "language": _overall,
            "topics": _overall,
        }

    # ------------------------------------------------------------------
    # Processing metadata — merged from Stage 1 + Stage 2
    # ------------------------------------------------------------------
    s1_proc: dict = stage1_result.get("processing", {})
    s2_proc: dict = (stage2_result or {}).get("processing", {})

    # This dict used to be a hand-maintained whitelist, and it silently dropped
    # TEN keys Stage 1 emits — including `stub_mode`, which §5.9 calls "the only
    # signal that the vector is synthetic", and `nlp_engine`, which says whether
    # the NLP came from a model at all. Neither reached the canonical result, so
    # neither reached the API, the dashboard, or any consumer.
    #
    # That is the §5.1 failure a fifth time: one component writes a field, the
    # next reads a different (shorter) set, and the mismatch degrades to a silent
    # omission rather than an error. `assembler.py` even reads
    # `result["processing"]["stub_mode"]` back out — a key this function had
    # removed, so it was always None.
    #
    # Stage-1 provenance is now forwarded explicitly. `_STAGE1_PROVENANCE_KEYS`
    # is asserted against Stage 1's real output by
    # tests/test_provenance_survives.py, so adding a field there without adding
    # it here breaks a test instead of vanishing.
    processing: dict = {
        "stage1_ms": s1_proc.get("stage1_ms"),
        "stage2_ms": s2_proc.get("stage2_ms") if stage2_result is not None else None,
        "llm_used": stage2_result is not None,
        "llm_backend": s2_proc.get("llm_backend") if stage2_result is not None else None,
        "llm_model": s2_proc.get("llm_model") if stage2_result is not None else None,
        # {role: resolved model id} — summarization and classification no longer
        # share a model, so a single `llm_model` can't attribute the summary.
        "role_models": s2_proc.get("role_models") if stage2_result is not None else None,
        "schema_version": SCHEMA_VERSION,
    }
    # Forward every Stage-1 provenance field that exists, rather than naming a
    # subset here and losing the rest.
    for key in _STAGE1_PROVENANCE_KEYS:
        if key in s1_proc:
            processing[key] = s1_proc[key]

    # ------------------------------------------------------------------
    # Timestamps — from normalized_post
    # ------------------------------------------------------------------
    created_at: str = normalized_post["created_at"]
    scraped_at: str = normalized_post["scraped_at"]

    # ------------------------------------------------------------------
    # Assemble
    # ------------------------------------------------------------------
    result: dict = {
        "post_id": post_id,
        "campaign_id": campaign_id,
        "platform": platform,
        "platform_post_id": platform_post_id,
        "media_type": media_type,
        "language": language,
        "post_text": post_text,
        "post_type": post_type,
        "post_summary": post_summary,
        "post_summary_lang": post_summary_lang,
        "post_summary_source": post_summary_source,
        "post_summary_grounding": post_summary_grounding,
        "post_summary_truncated": post_summary_truncated,
        # Which detector produced `language` — fastText or the deterministic
        # script heuristic. Without it, `language_confidence` cannot be read in
        # light of what produced it.
        "language_method": stage1_result.get("language_method"),
        "overall_sentiment": overall_sentiment,
        "sentiment_score": sentiment_score,
        "text_sentiment": text_sentiment,
        "image_sentiment": image_sentiment,
        "baseline_sentiment": baseline_sentiment,
        "baseline_viral_potential": baseline_viral_potential,
        "emotion": emotion,
        "intents": intents,
        "topics": topics,
        # One-line Stage-2 insight; null when Stage 2 was skipped or the insight
        # task did not run / returned nothing.
        "insight": insight,
        "entities": entities,
        "brand_mentions": brand_mentions,
        "keywords": keywords,
        "toxicity_score": toxicity_score,
        "hate_speech_score": hate_speech_score,
        "engagement": engagement,
        "reaction_breakdown": reaction_breakdown,
        "shares": shares,
        "image_analysis": image_analysis,
        "comment_analysis": comment_analysis,
        "confidence": confidence,
        "processing": processing,
        "created_at": created_at,
        "scraped_at": scraped_at,
    }

    # ------------------------------------------------------------------
    # Schema validation — raises ValueError with all errors if invalid
    # ------------------------------------------------------------------
    assert_valid_output(result)

    return result

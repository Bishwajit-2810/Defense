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

# Allow the libs package to be imported when running as a standalone service.
_LIBS_ROOT = os.path.join(os.path.dirname(__file__), "..", "..", "..", "libs")
if _LIBS_ROOT not in sys.path:
    sys.path.insert(0, os.path.abspath(_LIBS_ROOT))

from schemas.validator import assert_valid_output

# ---------------------------------------------------------------------------
# Schema version — bump when the output schema changes
# ---------------------------------------------------------------------------
SCHEMA_VERSION = "1.1"  # 1.1: per-comment emotion label + comment_analysis.emotion_breakdown


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

    if stage2_result is not None:
        post_summary = stage2_result.get("post_summary")
        post_summary_lang = stage2_result.get("post_summary_lang")
        post_summary_source = stage2_result.get("post_summary_source")
        post_summary_grounding = stage2_result.get("post_summary_grounding")

    # ------------------------------------------------------------------
    # Sentiment fields — Stage 1
    # ------------------------------------------------------------------
    overall_sentiment: str = stage1_result["overall_sentiment"]
    sentiment_score: float = stage1_result["sentiment_score"]
    # Stage-1 may emit either a bare label or a {label, score} dict; the schema
    # wants the bare label string (the score lives in sentiment_score).
    _ts = stage1_result.get("text_sentiment")      # null for null-caption posts
    text_sentiment: str | None = _ts.get("label") if isinstance(_ts, dict) else _ts
    _is = stage1_result.get("image_sentiment")     # null for text-only posts
    image_sentiment: str | None = _is.get("label") if isinstance(_is, dict) else _is

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
    intents: list = stage1_result.get("intents", [])
    topics: list = stage1_result.get("topics", [])
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

    processing: dict = {
        "stage1_ms": s1_proc.get("stage1_ms"),
        "stage2_ms": s2_proc.get("stage2_ms") if stage2_result is not None else None,
        "llm_used": stage2_result is not None,
        "llm_backend": s2_proc.get("llm_backend") if stage2_result is not None else None,
        "llm_model": s2_proc.get("llm_model") if stage2_result is not None else None,
        "schema_version": SCHEMA_VERSION,
    }

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
        "overall_sentiment": overall_sentiment,
        "sentiment_score": sentiment_score,
        "text_sentiment": text_sentiment,
        "image_sentiment": image_sentiment,
        "baseline_sentiment": baseline_sentiment,
        "baseline_viral_potential": baseline_viral_potential,
        "emotion": emotion,
        "intents": intents,
        "topics": topics,
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

"""Measure the router's LLM-routing rate over the sample corpus.

This is the script behind PROJECT_ASSESSMENT.md §4.6. It runs the *real* stage
functions offline — no Redis, Postgres, ClickHouse or MinIO — so the number it
prints comes from the same code the workers run:

    normalize_post -> analyze_text / analyze_image -> fuse_sentiment
                   -> analyze_comments -> _build_result -> should_use_llm

Usage
-----
    # Stage-1 keyword stub (deterministic, no Ollama needed)
    python -m eval.measure_routing_rate

    # Stage-1 LLM, i.e. the shipped .env configuration (needs Ollama running)
    STAGE1_LLM=true python -m eval.measure_routing_rate

    # Sweep the gate — the accuracy-vs-cost curve starts here
    ROUTER_CONFIDENCE_THRESHOLD=0.8 python -m eval.measure_routing_rate

It also reports the **post-level vs comment-level LLM call split** (§6.7), which
is the number that decides what the routing rate means. Before §6.3, Stage 2 was
a handful of per-post calls, so post-level routing was the dominant cost lever
and the routing rate roughly *was* the cost story. With every non-emoji comment
reaching the LLM, comment labelling outnumbers post-level calls several-fold and
the gate governs a minority of total spend. The honest claim becomes *"cheap NLP
filters which comments and which posts deserve an LLM"* rather than *"only N% of
posts reach the LLM"* — and this script is what supports it.

Env
---
    CORPUS         Corpus path (default posts_with_details.json — all 50 posts,
                   which is what the ingestion path actually uploads). The 7
                   null-caption PHOTO posts are *included* and reported
                   separately: measuring on the caption-filtered 43 described a
                   population the running system never processes, and hid 1,307
                   comments (12.7%) from the comment-lane cost figures below.
                   Point at posts_text_only.json to reproduce the old numbers.
    MAX_COMMENTS   Comments analysed per post. Default 0 = ALL of them, matching
                   the shipped configuration since §6.3 lifted the caps. No
                   routing rule reads a comment field, so this affects the
                   comment-lane cost estimate and runtime, **not** the routing
                   rate — set it low (e.g. 3) with STAGE1_LLM=true to get the
                   routing rate in minutes rather than hours, and take the
                   comment-lane figures from a MAX_COMMENTS=0 run.
    OUT            Write the per-post rows as JSON to this path.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# The workers use bare imports (`from dlq import ...`), so their own directories
# have to be importable the way the containers set them up.
# NB: the tree moved under src/defense/ — these paths track that layout.
for _p in (
    ROOT,
    ROOT / "src",
    ROOT / "src" / "defense",
    ROOT / "src" / "defense" / "libs",
    ROOT / "src" / "defense" / "services" / "workers" / "stage1_nlp",
):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

os.environ.setdefault("MODEL_STUB_MODE", "true")
os.environ.setdefault("STAGE1_LLM", "false")

from services.ingestion.normalizer import normalize_post  # noqa: E402
from services.workers.router.rules import get_task_flags, should_use_llm  # noqa: E402
from services.workers.stage1_nlp import worker as stage1  # noqa: E402
from services.workers.stage1_nlp.models import ModelRegistry  # noqa: E402
from services.workers.stage1_nlp.text_analyzer import analyze_text  # noqa: E402

from services.workers.stage1_nlp.comment_analyzer import (  # noqa: E402
    KIND_EMOJI,
    _comment_kind,
)

CORPUS = Path(os.getenv("CORPUS", ROOT / "posts_with_details.json"))
#: 0 = every comment, which is the shipped configuration since §6.3.
MAX_COMMENTS = int(os.getenv("MAX_COMMENTS", "0"))

# Batch sizes the workers use, so the call estimate matches what would run.
STAGE1_BATCH = int(os.getenv("STAGE1_LLM_BATCH", "25"))
STANCE_BATCH = int(os.getenv("COMMENT_STANCE_BATCH", "25"))


async def _stage1_result(raw_post: dict, registry: ModelRegistry) -> dict:
    """Run Stage 1 exactly as the worker does and return its result dict."""
    post = dict(raw_post)
    all_comments = raw_post.get("comments") or []
    post["comments"] = all_comments if MAX_COMMENTS <= 0 else all_comments[:MAX_COMMENTS]
    (
        text_result,
        image_result,
        comment_analysis,
        post_summary,
        post_summary_lang,
        post_summary_grounding,
        overall_sentiment,
        sentiment_score,
    ) = await stage1._process_message(post, registry)
    return stage1._build_result(
        post=post,
        text_result=text_result,
        image_result=image_result,
        comment_analysis=comment_analysis,
        post_summary=post_summary,
        post_summary_lang=post_summary_lang,
        post_summary_grounding=post_summary_grounding,
        overall_sentiment=overall_sentiment,
        sentiment_score=sentiment_score,
        stage1_ms=0.0,
    )


async def main() -> None:
    posts = json.loads(CORPUS.read_text())
    registry = ModelRegistry()
    await analyze_text("warmup", registry)  # discard cold-import cost

    rows: list[dict] = []
    for raw_post in posts:
        normalized = normalize_post(raw_post)
        result = await _stage1_result(raw_post, registry)
        use_llm, reasons = should_use_llm(result, {})
        flags = get_task_flags(result, {}) if use_llm else {}

        # Comment-lane volume. Emoji-only comments never enter an LLM batch
        # (§6.2), so they are excluded from the count that drives cost.
        comments = raw_post.get("comments") or []
        if MAX_COMMENTS > 0:
            comments = comments[:MAX_COMMENTS]
        non_emoji = sum(
            1 for c in comments if _comment_kind(c.get("text") or "") != KIND_EMOJI
        )
        rows.append(
            {
                "stored_comments": len(comments),
                "non_emoji_comments": non_emoji,
                "emoji_comments": len(comments) - non_emoji,
                "post_id": result.get("post_id"),
                "media_type": result.get("media_type"),
                "confidence": result.get("confidence"),
                "post_type": result.get("post_type"),
                "post_type_confidence": result.get("post_type_confidence"),
                "toxicity_score": result.get("toxicity_score"),
                "photos": len(result.get("photo_urls") or []),
                "caption_chars": result.get(
                    "caption_chars", len(normalized.get("caption") or "")
                ),
                "script": result.get("script"),
                "use_llm": use_llm,
                "reasons": reasons,
                "want_post_type": flags.get("want_post_type", False),
            }
        )

    header = (
        f"{'post':<14}{'media':<11}{'conf':>6}{'post_type':>12}{'ptc':>6}"
        f"{'tox':>6}{'ph':>3}{'chars':>7}{'script':>8}  {'LLM':<4} reasons"
    )
    print(header)
    print("-" * len(header))
    for r in rows:
        print(
            f"{(r['post_id'] or '')[:13]:<14}{str(r['media_type']):<11}"
            f"{r['confidence']!s:>6}{str(r['post_type']):>12}"
            f"{r['post_type_confidence']!s:>6}{r['toxicity_score']!s:>6}"
            f"{r['photos']:>3}{r['caption_chars']:>7}{str(r['script']):>8}  "
            f"{'YES' if r['use_llm'] else 'no':<4} {', '.join(r['reasons'])}"
        )

    routed = [r for r in rows if r["use_llm"]]
    fired = Counter(reason.split(":")[0] for r in routed for reason in r["reasons"])
    if os.getenv("STAGE1_LLM", "false").lower() == "true":
        engine = "stage1-LLM"
    elif os.getenv("MODEL_STUB_MODE", "true").lower() == "true":
        engine = "keyword-stub"
    else:
        engine = "small-model suite"
    # The 7 null-caption PHOTO posts are in the corpus by default (they are what
    # ingestion uploads), so say so rather than letting the denominator drift
    # silently between runs pointed at different corpora.
    captionless = [r for r in rows if not r["caption_chars"]]
    captionless_note = (
        f"  ({len(captionless)} null-caption post(s) included, carrying "
        f"{sum(r['stored_comments'] for r in captionless):,} comments)"
        if captionless
        else "  (all posts captioned)"
    )
    print(
        f"\nCorpus:         {CORPUS.name}{captionless_note}"
        f"\nStage-1 engine: {engine}"
        f"\nROUTING RATE:   {len(routed)}/{len(rows)} = {len(routed) / len(rows):.0%}"
        f"   (bypassed Stage 2: {len(rows) - len(routed)})"
        f"\nrules fired:    {dict(fired)}"
        f"\nStage-1 typed:  {sum(1 for r in rows if r['post_type'])}/{len(rows)} posts"
        f"\nStage-2 post_type calls needed: {sum(1 for r in routed if r['want_post_type'])}"
    )

    # -----------------------------------------------------------------------
    # §6.7 — post-level vs comment-level LLM calls
    # -----------------------------------------------------------------------
    # What the routing gate actually governs. It decides which POSTS reach
    # Stage 2; it does not decide whether comments get an LLM, because Stage-1
    # comment labelling runs for every post.
    def _batches(n: int, size: int) -> int:
        return -(-n // size) if n > 0 else 0

    # Post-level: summary + insight for every routed post, post_type only when
    # Stage 1 was not confident enough. Comment summary is one call per routed
    # post with comments.
    post_calls = (
        2 * len(routed)
        + sum(1 for r in routed if r["want_post_type"])
        + sum(1 for r in routed if r["stored_comments"])
    )

    # Comment-level, Stage 1: EVERY post, routed or not (§6.3 step 4).
    stage1_comment_calls = sum(
        _batches(r["non_emoji_comments"], STAGE1_BATCH) for r in rows
    )
    # Comment-level, Stage 2 stance: routed posts only — this is the part the
    # gate still governs.
    stance_calls = sum(
        _batches(r["non_emoji_comments"], STANCE_BATCH) for r in routed
    )
    comment_calls = stage1_comment_calls + stance_calls
    total_calls = post_calls + comment_calls

    total_comments = sum(r["stored_comments"] for r in rows)
    total_non_emoji = sum(r["non_emoji_comments"] for r in rows)

    print(
        f"\nCOMMENT VOLUME"
        f"\n  stored comments:      {total_comments:,}"
        f"\n  emoji-only (skipped): {total_comments - total_non_emoji:,}"
        f" ({(total_comments - total_non_emoji) / total_comments:.1%})"
        if total_comments else "\nCOMMENT VOLUME\n  (no comments)"
    )
    if total_comments:
        print(f"  reaching the LLM:     {total_non_emoji:,}")

    print(
        f"\nLLM CALL SPLIT (per corpus run, cold cache)"
        f"\n  post-level:            {post_calls:>6,}  "
        f"({post_calls / total_calls:.1%})   summary + insight + post_type + comment-summary"
        f"\n  comment-level:         {comment_calls:>6,}  "
        f"({comment_calls / total_calls:.1%})"
        f"\n      Stage-1 labelling: {stage1_comment_calls:>6,}   all {len(rows)} posts"
        f" @ {STAGE1_BATCH}/batch"
        f"\n      Stage-2 stance:    {stance_calls:>6,}   {len(routed)} routed posts"
        f" @ {STANCE_BATCH}/batch"
        f"\n  TOTAL:                 {total_calls:>6,}"
        if total_calls else "\n(no LLM calls)"
    )
    if total_calls:
        gate_governed = post_calls + stance_calls
        print(
            f"\nWHAT THE ROUTING GATE GOVERNS"
            f"\n  Calls the gate decides:  {gate_governed:,}/{total_calls:,}"
            f" = {gate_governed / total_calls:.1%}"
            f"\n  (Stage-1 comment labelling runs for EVERY post, routed or not,"
            f"\n   so the gate cannot reduce it.)"
            f"\n\n  Read the routing rate as a measure of Stage-1 quality, and the"
            f"\n  cost story as: cheap NLP filters which COMMENTS and which POSTS"
            f"\n  deserve an LLM — not 'only N% of posts reach the LLM'."
        )

    out = os.getenv("OUT")
    if out:
        Path(out).write_text(json.dumps(rows, indent=1, ensure_ascii=False))
        print(f"per-post rows -> {out}")


if __name__ == "__main__":
    asyncio.run(main())

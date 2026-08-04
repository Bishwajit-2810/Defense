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

Env
---
    MAX_COMMENTS   Comments analysed per post (default 40). No routing rule reads
                   a comment field, so this only affects runtime.
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
for _p in (ROOT, ROOT / "libs", ROOT / "services" / "workers" / "stage1_nlp"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

os.environ.setdefault("MODEL_STUB_MODE", "true")
os.environ.setdefault("STAGE1_LLM", "false")

from services.ingestion.normalizer import normalize_post  # noqa: E402
from services.workers.router.rules import get_task_flags, should_use_llm  # noqa: E402
from services.workers.stage1_nlp import worker as stage1  # noqa: E402
from services.workers.stage1_nlp.models import ModelRegistry  # noqa: E402
from services.workers.stage1_nlp.text_analyzer import analyze_text  # noqa: E402

CORPUS = ROOT / "posts_with_details.json"
MAX_COMMENTS = int(os.getenv("MAX_COMMENTS", "40"))


async def _stage1_result(raw_post: dict, registry: ModelRegistry) -> dict:
    """Run Stage 1 exactly as the worker does and return its result dict."""
    post = dict(raw_post)
    post["comments"] = (raw_post.get("comments") or [])[:MAX_COMMENTS]
    (
        text_result,
        image_result,
        comment_analysis,
        overall_sentiment,
        sentiment_score,
    ) = await stage1._process_message(post, registry)
    return stage1._build_result(
        post=post,
        text_result=text_result,
        image_result=image_result,
        comment_analysis=comment_analysis,
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
        rows.append(
            {
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
    print(
        f"\nStage-1 engine: {engine}"
        f"\nROUTING RATE:   {len(routed)}/{len(rows)} = {len(routed) / len(rows):.0%}"
        f"   (bypassed Stage 2: {len(rows) - len(routed)})"
        f"\nrules fired:    {dict(fired)}"
        f"\nStage-1 typed:  {sum(1 for r in rows if r['post_type'])}/{len(rows)} posts"
        f"\nStage-2 post_type calls needed: {sum(1 for r in routed if r['want_post_type'])}"
    )

    out = os.getenv("OUT")
    if out:
        Path(out).write_text(json.dumps(rows, indent=1, ensure_ascii=False))
        print(f"per-post rows -> {out}")


if __name__ == "__main__":
    asyncio.run(main())

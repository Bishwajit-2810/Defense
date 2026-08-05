"""Sweep the router's confidence threshold and plot cost against it (P1.5).

Converts the cascade from an assertion into an empirical curve, which is the
only framing in which the routing work reads as a contribution rather than a
re-implementation of FrugalGPT/RouteLLM (PROJECT_ASSESSMENT §7.1, §8.3).

**What this script can and cannot produce.**

It measures the **cost axis** completely: for each threshold, the routing rate,
the post-level and comment-level LLM call counts, and the share of calls the gate
actually governs. All of that is real today.

It cannot produce the **accuracy axis**, because there are no gold labels — §7.2
is the one remaining research blocker. So the output is half a curve, and it says
so. Once ~300 comments are labelled (§9.9), join this CSV on `threshold` with a
macro-F1 per threshold and the plot is complete.

Two things worth knowing before reading the numbers
---------------------------------------------------
1. **The gate governs only 30-55% of LLM calls** (§6.8), because Stage-1 comment
   labelling runs for every post regardless of routing. A threshold sweep
   therefore moves a *minority* of total cost. The script reports both the
   gate-governed subtotal and the whole, so the curve is not oversold.
2. **The second lever is `STAGE1_LLM_COMMENT_MAX`**, and nobody has plotted it.
   `--sweep comment-cap` does that instead; it is arguably the more interesting
   curve now that cost is comment-dominated.

Usage
-----
    python -m eval.sweep_threshold                      # confidence threshold
    python -m eval.sweep_threshold --sweep comment-cap
    python -m eval.sweep_threshold --csv sweep.csv
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for _p in (ROOT, ROOT / "libs", ROOT / "services" / "workers" / "stage1_nlp"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

os.environ.setdefault("MODEL_STUB_MODE", "true")
os.environ.setdefault("STAGE1_LLM", "false")

DEFAULT_THRESHOLDS = [0.5, 0.55, 0.6, 0.65, 0.7, 0.75, 0.8, 0.85, 0.9]
DEFAULT_CAPS = [0, 10, 25, 40, 60, 100, 250]


async def _measure(corpus: Path, max_comments: int) -> dict:
    """Run the real stage functions once and return the routing + cost figures.

    Imports are done inside the function and the router rules re-imported each
    call, because the thresholds are read at module import — which is exactly
    what makes them sweepable (§4.5 item 5).
    """
    from services.ingestion.normalizer import normalize_post
    from services.workers.stage1_nlp import worker as stage1
    from services.workers.stage1_nlp.comment_analyzer import KIND_EMOJI, _comment_kind
    from services.workers.stage1_nlp.models import ModelRegistry

    # Re-import so the new env value is picked up.
    import services.workers.router.rules as rules
    rules = importlib.reload(rules)

    posts = json.loads(corpus.read_text())
    registry = ModelRegistry()

    routed = 0
    total = 0
    want_post_type = 0
    stage1_batches = 0
    stance_batches = 0
    non_emoji_total = 0
    routed_with_comments = 0
    batch = int(os.environ.get("STAGE1_LLM_BATCH", "25"))

    def _batches(n: int) -> int:
        return -(-n // batch) if n > 0 else 0

    for raw_post in posts:
        normalize_post(raw_post)
        post = dict(raw_post)
        all_comments = raw_post.get("comments") or []
        post["comments"] = all_comments if max_comments <= 0 else all_comments[:max_comments]

        (
            text_result, image_result, comment_analysis,
            overall_sentiment, sentiment_score,
        ) = await stage1._process_message(post, registry)
        result = stage1._build_result(
            post=post, text_result=text_result, image_result=image_result,
            comment_analysis=comment_analysis, overall_sentiment=overall_sentiment,
            sentiment_score=sentiment_score, stage1_ms=0.0,
        )

        use_llm, _reasons = rules.should_use_llm(result, {})
        flags = rules.get_task_flags(result, {}) if use_llm else {}

        total += 1
        non_emoji = sum(
            1 for c in post["comments"]
            if _comment_kind(c.get("text") or "") != KIND_EMOJI
        )
        non_emoji_total += non_emoji
        # Stage-1 comment labelling runs for EVERY post — the gate cannot touch it.
        stage1_batches += _batches(non_emoji)
        if use_llm:
            routed += 1
            want_post_type += 1 if flags.get("want_post_type") else 0
            stance_batches += _batches(non_emoji)
            if post["comments"]:
                routed_with_comments += 1

    post_level = 2 * routed + want_post_type + routed_with_comments
    comment_level = stage1_batches + stance_batches
    all_calls = post_level + comment_level
    gate_governed = post_level + stance_batches

    return {
        "posts": total,
        "routed": routed,
        "routing_rate": round(routed / total, 4) if total else 0.0,
        "non_emoji_comments": non_emoji_total,
        "post_level_calls": post_level,
        "comment_level_calls": comment_level,
        "stage1_comment_calls": stage1_batches,
        "stage2_stance_calls": stance_batches,
        "total_calls": all_calls,
        "gate_governed_calls": gate_governed,
        "gate_governed_share": round(gate_governed / all_calls, 4) if all_calls else 0.0,
    }


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, default=ROOT / "posts_text_only.json")
    parser.add_argument(
        "--sweep", choices=["threshold", "comment-cap"], default="threshold",
        help="threshold = ROUTER_CONFIDENCE_THRESHOLD; comment-cap = STAGE1_LLM_COMMENT_MAX",
    )
    parser.add_argument("--values", nargs="+", type=float)
    parser.add_argument(
        "--max-comments", type=int, default=0,
        help="0 = every comment (the shipped configuration since §6.3)",
    )
    parser.add_argument("--csv", type=Path, help="write the rows here for plotting")
    args = parser.parse_args()

    if args.sweep == "threshold":
        values = args.values or DEFAULT_THRESHOLDS
        env_key = "ROUTER_CONFIDENCE_THRESHOLD"
    else:
        values = args.values or DEFAULT_CAPS
        env_key = "STAGE1_LLM_COMMENT_MAX"

    print(f"Sweeping {env_key} over {values}")
    print(f"Corpus: {args.corpus.name}\n")

    rows: list[dict] = []
    for value in values:
        formatted = str(int(value)) if args.sweep == "comment-cap" else str(value)
        os.environ[env_key] = formatted
        # The comment cap is read at import in comment_analyzer, so reload it too.
        if args.sweep == "comment-cap":
            import services.workers.stage1_nlp.comment_analyzer as ca
            importlib.reload(ca)
        metrics = await _measure(args.corpus, args.max_comments)
        rows.append({env_key.lower(): formatted, **metrics})

    # --- Table ---------------------------------------------------------------
    cols = [
        env_key.lower(), "routed", "routing_rate", "post_level_calls",
        "comment_level_calls", "total_calls", "gate_governed_share",
    ]
    widths = {c: max(len(c), *(len(str(r.get(c, ""))) for r in rows)) for c in cols}
    print("  ".join(c.ljust(widths[c]) for c in cols))
    print("  ".join("-" * widths[c] for c in cols))
    for r in rows:
        print("  ".join(str(r.get(c, "")).ljust(widths[c]) for c in cols))

    print(
        "\nThis is the COST axis only. The accuracy axis needs gold labels "
        "(PROJECT_ASSESSMENT §7.2, §9.9)\nand does not exist yet — so this is "
        "half a curve, and should be presented as such.\n"
        "\nNote the gate_governed_share column: a threshold sweep moves only "
        "that fraction of total\nLLM cost, because Stage-1 comment labelling "
        "runs for every post regardless (§6.8)."
    )
    if args.max_comments > 0:
        print(
            f"\n⚠  --max-comments {args.max_comments} shrinks the comment lane, so the "
            "call counts and\n   gate_governed_share above are NOT the shipped "
            "figures — they overstate the gate's\n   reach. Re-run with "
            "--max-comments 0 for numbers to quote."
        )

    if args.csv:
        import csv

        with args.csv.open("w", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        print(f"\nrows → {args.csv}   (join on {env_key.lower()} with macro-F1 to complete the plot)")


if __name__ == "__main__":
    asyncio.run(main())

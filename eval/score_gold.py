"""Score any labelling system against the human-adjudicated gold set.

    python -m eval.score_gold                    # score every system
    python -m eval.score_gold --system heuristic # just one
    python -m eval.score_gold --json             # machine-readable

What it reports, and why each number is there
---------------------------------------------
* **accuracy** over the rows a human labelled — the headline, and meaningless
  without the next line.
* **adjudicated / total** — how much of the gold set is actually labelled. An
  accuracy computed over 12 rows is not an accuracy; the report says so instead
  of printing a confident percentage.
* **per-stratum accuracy** (kind, script) — a system can be 90% overall by being
  perfect on emoji and useless on Banglish. The corpus is 60% short reactions,
  so the aggregate hides exactly the failure that matters.
* **abstention rate and accuracy-when-answered** — a system that says
  "uncertain" instead of guessing should be rewarded for it, not punished. Both
  numbers are printed because either alone can be gamed: always abstaining gives
  perfect accuracy-when-answered, never abstaining gives 0% abstention.
* **gold-uncertain rows** are excluded from accuracy and counted separately: if
  a person could not tell, a model matching them is luck and a model
  contradicting them is not clearly wrong.

Systems are read from the gold file's own `predictions` block when present
(written by ``--predict``), so scoring never needs a model at hand.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

DEFAULT_GOLD = REPO / "eval" / "gold" / "comments_gold_300.json"
LABELS = ("positive", "negative", "neutral", "uncertain")


# ---------------------------------------------------------------------------
# Prediction
# ---------------------------------------------------------------------------

async def predict_all(rows: list[dict]) -> dict[str, dict[str, str]]:
    """Run every available labeller over the gold rows.

    Returns {system: {comment_id: label}}. A system that cannot run (no weights,
    no LLM) is simply absent — it does not contribute fabricated neutrals, which
    would score as ~40% accuracy on this corpus and look like a working model.
    """
    from defense.libs.ensemble import combine
    from defense.services.workers.stage1_nlp.comment_analyzer import classify_comment
    from defense.services.workers.stage1_nlp.models import ModelRegistry

    out: dict[str, dict[str, str]] = defaultdict(dict)
    registry = ModelRegistry()

    # 1. Stage-1 heuristic / small-model path — always available.
    for row in rows:
        res = await classify_comment(row["text"], registry)
        out["heuristic"][row["comment_id"]] = res.get("sentiment") or "neutral"

    # 2. The cheap HF heads, when weights are actually present. Driven by the
    #    configured roster, not a hardcoded pair: scoring two of the seven
    #    voters and calling the result "the ensemble" measures a system that is
    #    not the one in production.
    try:
        from defense.services.workers.stage2_llm.worker import _run_hf_classifier
        from defense.libs.common.config import get_settings

        config = get_settings()
        for name, model_id in config.stage2_classifier_roster:
            probe = [
                {"text": r["text"], "text_norm": r.get("text_norm"), "kind": r["kind"]}
                for r in rows
            ]
            voted = await _run_hf_classifier(probe, name, model_id)
            if not voted:
                print(f"  {name}: unavailable (stub mode or weights missing) — skipped")
                continue
            for row, c in zip(rows, probe):
                label = (c.get("parallel_labels") or {}).get(name, {}).get("sentiment")
                if label:
                    out[name][row["comment_id"]] = label
    except Exception as exc:  # pragma: no cover - depends on local weights
        print(f"  cheap classifiers unavailable: {exc}")

    # 3. The ensemble over whatever voted above.
    for row in rows:
        votes = {
            system: {"sentiment": preds[row["comment_id"]]}
            for system, preds in out.items()
            if row["comment_id"] in preds
        }
        if votes:
            out["ensemble"][row["comment_id"]] = combine(votes).label

    return {k: dict(v) for k, v in out.items()}


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def score(rows: list[dict], preds: dict[str, str]) -> dict:
    """Score one system's predictions against the adjudicated rows."""
    gold = [r for r in rows if r.get("label") in LABELS]
    certain = [r for r in gold if r["label"] != "uncertain"]

    answered = correct = abstained = 0
    by_kind: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    by_script: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    confusion: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))

    for row in certain:
        pred = preds.get(row["comment_id"])
        if pred is None:
            continue
        confusion[row["label"]][pred] += 1
        if pred == "uncertain":
            abstained += 1
            continue
        answered += 1
        hit = int(pred == row["label"])
        correct += hit
        for bucket, key in ((by_kind, row["kind"]), (by_script, row["script"])):
            bucket[key][0] += hit
            bucket[key][1] += 1

    scored = answered + abstained
    return {
        "gold_total": len(rows),
        "adjudicated": len(gold),
        "gold_uncertain": len(gold) - len(certain),
        "scored": scored,
        "answered": answered,
        "abstained": abstained,
        "abstention_rate": round(abstained / scored, 4) if scored else 0.0,
        # Abstentions count as misses here: this is "how often is it right about
        # a comment a person could label".
        "accuracy": round(correct / scored, 4) if scored else None,
        "accuracy_when_answered": round(correct / answered, 4) if answered else None,
        "by_kind": {
            k: {"n": n, "accuracy": round(c / n, 4)} for k, (c, n) in by_kind.items() if n
        },
        "by_script": {
            k: {"n": n, "accuracy": round(c / n, 4)} for k, (c, n) in by_script.items() if n
        },
        "confusion": {g: dict(p) for g, p in confusion.items()},
    }


def report(name: str, s: dict) -> None:
    if not s["adjudicated"]:
        print(f"\n{name}: no adjudicated rows yet — nothing to score.")
        return
    thin = s["adjudicated"] < 50
    print(f"\n{name}")
    print(f"  adjudicated      {s['adjudicated']}/{s['gold_total']}"
          + ("   ** too few rows to draw a conclusion **" if thin else ""))
    print(f"  accuracy         {s['accuracy']}")
    print(f"  when answered    {s['accuracy_when_answered']}  "
          f"(abstained on {s['abstention_rate'] * 100:.1f}%)")
    if s["gold_uncertain"]:
        print(f"  gold-uncertain   {s['gold_uncertain']} rows excluded from accuracy")
    if s["by_kind"]:
        print("  by kind          " + "  ".join(
            f"{k}={v['accuracy']}(n={v['n']})" for k, v in sorted(s["by_kind"].items())))
    if s["by_script"]:
        print("  by script        " + "  ".join(
            f"{k}={v['accuracy']}(n={v['n']})" for k, v in sorted(s["by_script"].items())))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--gold", type=Path, default=DEFAULT_GOLD)
    ap.add_argument("--system", help="Score only this system")
    ap.add_argument("--predict", action="store_true",
                    help="Run the labellers now and store their predictions in the gold file")
    ap.add_argument("--json", action="store_true", help="Machine-readable output")
    args = ap.parse_args()

    if not args.gold.exists():
        print(f"gold set not found: {args.gold}\nRun: python -m eval.build_gold_set", file=sys.stderr)
        return 1

    doc = json.loads(args.gold.read_text(encoding="utf-8"))
    rows = doc["comments"]

    if args.predict:
        print("running labellers over the gold set…")
        doc["predictions"] = asyncio.run(predict_all(rows))
        args.gold.write_text(
            json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"stored predictions for: {', '.join(doc['predictions'])}")

    predictions: dict[str, dict[str, str]] = doc.get("predictions") or {}
    if not predictions:
        print("no predictions stored — run with --predict first", file=sys.stderr)
        return 1

    adjudicated = sum(1 for r in rows if r.get("label") in LABELS)
    if not adjudicated:
        print(
            f"\n{args.gold.name}: 0 of {len(rows)} comments carry a human label.\n"
            "Nothing can be scored yet — that is the point of the file, not a bug.\n"
            "Fill in `label` (positive/negative/neutral/uncertain) per the protocol\n"
            "in its `_meta.protocol`, then re-run. The Details modal's\n"
            "'disagreed' comment filter is the fastest place to start: those are\n"
            "the comments the labellers could not agree on.",
            file=sys.stderr,
        )
        return 2

    results = {
        name: score(rows, preds)
        for name, preds in predictions.items()
        if not args.system or name == args.system
    }
    if args.json:
        print(json.dumps(results, indent=2))
    else:
        print(f"\nGold set: {args.gold.name} — {adjudicated}/{len(rows)} adjudicated")
        for name, s in sorted(results.items()):
            report(name, s)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Side-by-side bake-off for the `summary` role — pick the model by measurement.

§6.5 asks for a stronger, separate model for summarization. Which one is a
question with an answer, and the answer belongs in the defense as a table:
"we chose it because it scored X" is evidence; "we chose it because it is
bigger" is not.

What it does
------------
Runs N real Bangla posts from the corpus through each candidate model on the
`summary` role and records, per model:

  * **latency** p50 / p95 per post,
  * **length** in characters (a model that answers in 40 characters is not
    summarizing),
  * **truncation rate** — how often the reply still hit the token ceiling after
    auto-continuation (§6.1). This is language-correlated, so it is exactly the
    axis on which a Bangla summarizer differs from an English one,
  * **language fidelity** — whether the summary is written in the post's own
    script rather than silently switching to English,
  * **grounding** — the share of the summary's content words that appear in the
    caption, a cheap faithfulness proxy that needs no second model.

Faithfulness and fluency still want a human read; this script prints the
summaries alongside the numbers so that read is quick. It does not pretend to
be an LLM-judge — §7.2 is honest that no accuracy evaluation exists yet, and a
bake-off scored by the same family of model it is choosing between would not
fix that.

Usage
-----
    python -m eval.bakeoff_summary                        # defaults, 10 posts
    python -m eval.bakeoff_summary --posts 20 \
        --models qwen2.5:7b gemma4:26b gemma4:31b
    python -m eval.bakeoff_summary --json out.json

Needs Ollama running with the candidate models pulled (`ollama list`).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from libs.llm import LLMClient  # noqa: E402
from services.workers.stage2_llm.prompts import build_summary_messages  # noqa: E402

DEFAULT_CORPUS = ROOT / "posts_text_only.json"
DEFAULT_MODELS = ["qwen2.5:7b", "gemma4:26b", "gemma4:31b"]

_BANGLA = re.compile(r"[ঀ-৿]")
_WORD = re.compile(r"[0-9A-Za-zঀ-৿]+")


def _script_of(text: str) -> str:
    """Crude script label — enough to catch a model answering in the wrong one."""
    bn = len(_BANGLA.findall(text))
    total = len(_WORD.sub("", text)) + len(_WORD.findall(text))
    if not text.strip():
        return "empty"
    return "bn" if bn >= 5 else "latin"


def _grounding(summary: str, caption: str) -> float:
    """Share of the summary's content words that also appear in the caption.

    A blunt faithfulness proxy: a summary that invents its vocabulary wholesale
    is either paraphrasing heavily or hallucinating, and both are worth seeing
    next to the text. Not a metric to publish — a signal to read the summary.
    """
    s_words = {w for w in _WORD.findall(summary.lower()) if len(w) > 3}
    c_words = {w for w in _WORD.findall(caption.lower()) if len(w) > 3}
    if not s_words:
        return 0.0
    return round(len(s_words & c_words) / len(s_words), 3)


def _load_posts(corpus: Path, n: int) -> list[dict]:
    data = json.loads(corpus.read_text())
    if isinstance(data, dict):
        data = data.get("posts") or data.get("data") or []
    captioned = [p for p in data if (p.get("caption") or "").strip()]
    # Longest captions first: truncation and fidelity differences show up on
    # real prose, not on one-line posts.
    captioned.sort(key=lambda p: len(p.get("caption") or ""), reverse=True)
    return captioned[:n]


async def _summarize(llm: LLMClient, model: str, post: dict, max_tokens: int) -> dict:
    caption = (post.get("caption") or "").strip()
    messages = build_summary_messages(
        caption=caption,
        ocr_text="",
        image_description="",
        language="bn",
        target_lang="the post's own language",
    )
    t0 = time.perf_counter()
    try:
        resp = await llm.chat(
            role="summary",
            messages=messages,
            model=model,             # explicit override — bypasses the role default
            max_tokens=max_tokens,
            temperature=0.2,
        )
    except Exception as exc:
        return {"post_id": post.get("id"), "error": str(exc)[:200]}
    elapsed = time.perf_counter() - t0

    summary = (resp.get("content") or "").strip()
    return {
        "post_id": post.get("id"),
        "caption_chars": len(caption),
        "summary": summary,
        "summary_chars": len(summary),
        "latency_s": round(elapsed, 2),
        "truncated": bool(resp.get("truncated")),
        "continuations": resp.get("continuations", 0),
        "tokens": (resp.get("usage") or {}).get("total_tokens", 0),
        "script": _script_of(summary),
        "caption_script": _script_of(caption),
        "grounding": _grounding(summary, caption),
    }


def _summarise_runs(model: str, runs: list[dict]) -> dict:
    ok = [r for r in runs if "error" not in r]
    if not ok:
        return {"model": model, "posts": 0, "errors": len(runs)}
    lat = sorted(r["latency_s"] for r in ok)
    same_script = sum(1 for r in ok if r["script"] == r["caption_script"])
    return {
        "model": model,
        "posts": len(ok),
        "errors": len(runs) - len(ok),
        "latency_p50_s": round(statistics.median(lat), 2),
        "latency_p95_s": round(lat[max(0, int(len(lat) * 0.95) - 1)], 2),
        "mean_chars": round(statistics.mean(r["summary_chars"] for r in ok)),
        "empty": sum(1 for r in ok if not r["summary"]),
        "truncated": sum(1 for r in ok if r["truncated"]),
        "kept_language": f"{same_script}/{len(ok)}",
        "mean_grounding": round(statistics.mean(r["grounding"] for r in ok), 3),
        "mean_tokens": round(statistics.mean(r["tokens"] for r in ok)),
    }


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--posts", type=int, default=10)
    parser.add_argument("--models", nargs="+", default=DEFAULT_MODELS)
    parser.add_argument("--max-tokens", type=int, default=1024)
    parser.add_argument("--json", type=Path, help="write full per-post results here")
    parser.add_argument("--show", action="store_true", help="print each summary")
    args = parser.parse_args()

    posts = _load_posts(args.corpus, args.posts)
    print(f"corpus: {args.corpus.name} — {len(posts)} posts "
          f"(longest captions first)\n")

    llm = LLMClient()
    all_results: dict[str, list[dict]] = {}
    rows: list[dict] = []

    for model in args.models:
        print(f"── {model} " + "─" * max(0, 60 - len(model)))
        runs = []
        for i, post in enumerate(posts, 1):
            run = await _summarize(llm, model, post, args.max_tokens)
            runs.append(run)
            if "error" in run:
                print(f"  {i:2d}. ERROR {run['error']}")
            else:
                flag = " [TRUNCATED]" if run["truncated"] else ""
                print(f"  {i:2d}. {run['latency_s']:6.2f}s  "
                      f"{run['summary_chars']:4d} chars  "
                      f"script={run['script']}  ground={run['grounding']:.2f}{flag}")
                if args.show:
                    print(f"      {run['summary'][:300]}")
        all_results[model] = runs
        rows.append(_summarise_runs(model, runs))
        print()

    # --- Comparison table ---------------------------------------------------
    cols = ["model", "posts", "latency_p50_s", "latency_p95_s", "mean_chars",
            "empty", "truncated", "kept_language", "mean_grounding", "mean_tokens"]
    widths = {c: max(len(c), *(len(str(r.get(c, "—"))) for r in rows)) for c in cols}
    print("  ".join(c.ljust(widths[c]) for c in cols))
    print("  ".join("-" * widths[c] for c in cols))
    for r in rows:
        print("  ".join(str(r.get(c, "—")).ljust(widths[c]) for c in cols))

    print(
        "\nPick on faithfulness and Bangla fluency FIRST (read the summaries), "
        "then latency.\nSet SUMMARY_LOCAL_MODEL to the winner and record this "
        "table — it is the ablation\n§7.4 keeps asking for."
    )

    if args.json:
        args.json.write_text(json.dumps(
            {"summary": rows, "runs": all_results}, ensure_ascii=False, indent=2
        ))
        print(f"\nfull results → {args.json}")


if __name__ == "__main__":
    asyncio.run(main())

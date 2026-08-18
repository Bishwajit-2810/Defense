"""Build the 300-comment gold set — a stratified, reproducible sample.

Why this exists
---------------
Every accuracy claim in this project has so far been an argument. There is a
bake-off harness, a routing-rate measurement and a cost model, but no labelled
truth, so "the ensemble is better than the heuristic" cannot be answered with a
number. 300 comments is enough to separate systems that differ by more than a
few points, and small enough that one person can adjudicate it in an afternoon.

What this script does and does NOT do
-------------------------------------
It SAMPLES and it stratifies. It does not label.

The `label` field of every row it writes is ``null``, and
``eval/score_gold.py`` scores only the rows a human has filled in. That is
deliberate: seeding the labels from any model in this repo — including the
ensemble — and then scoring that model against them would measure agreement
with itself and report it as accuracy. A gold set is only worth the human hours
in it.

Stratification
--------------
A uniform random sample of this corpus is ~60% short/emoji reactions, so a
system could score well on it while being useless on the comments anyone cares
about. The sample is drawn in fixed proportions across:

  * kind      — substantive / short / emoji / link
  * script    — bengali / latin(banglish) / mixed
  * agreement — where the cheap labellers already disagree (the hard cases)

Sampling is seeded and deterministic: re-running produces the same 300 rows, so
the file can be regenerated without invalidating labels already applied (the
merge step below preserves them by comment id).

Usage
-----
    python -m eval.build_gold_set                    # writes eval/gold/comments_gold_300.json
    python -m eval.build_gold_set --size 500         # a bigger set
    python -m eval.build_gold_set --corpus posts_text_only.json   # caption-filtered subset
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from defense.services.workers.stage1_nlp.comment_analyzer import (  # noqa: E402
    _comment_kind,
    normalize_for_model,
)

#: The full source corpus — all 50 posts, 10,272 comments. Deliberately *not*
#: the caption-filtered ``posts_text_only.json``: that filter exists because a
#: null-caption PHOTO post has no **post** text for Stage 1 to analyse, which
#: says nothing about its *comments*. The 7 excluded posts carry 1,307 perfectly
#: analysable comments (12.7% of the corpus), and dropping them shrank the
#: sampling frame for no reason the sentiment task cares about.
DEFAULT_CORPUS = REPO / "posts_with_details.json"
DEFAULT_OUT = REPO / "eval" / "gold" / "comments_gold_300.json"
SEED = 20260813

#: Target composition. Substantive comments dominate because they are the ones a
#: sentiment claim is actually about; emoji and short reactions are represented
#: because they are 60% of the corpus and a system that mishandles them is wrong
#: about most of what it sees.
STRATA = {
    "substantive": 0.55,
    "short": 0.25,
    "emoji": 0.15,
    "link": 0.05,
}

_BANGLA_RE = re.compile(r"[ঀ-৿]")
_LATIN_RE = re.compile(r"[A-Za-z]")


def detect_script(text: str) -> str:
    bangla = bool(_BANGLA_RE.search(text or ""))
    latin = bool(_LATIN_RE.search(text or ""))
    if bangla and latin:
        return "mixed"
    if bangla:
        return "bengali"
    if latin:
        return "latin"
    return "other"


def iter_comments(corpus_path: Path):
    """Yield (post, comment) for every embedded comment in the corpus."""
    data = json.loads(corpus_path.read_text(encoding="utf-8"))
    posts = data if isinstance(data, list) else data.get("data") or []
    for post in posts:
        for comment in post.get("comments") or []:
            yield post, comment


def build(corpus_path: Path, size: int) -> dict:
    rng = random.Random(SEED)
    pools: dict[str, list[dict]] = {k: [] for k in STRATA}

    seen_ids: set[str] = set()
    for post, comment in iter_comments(corpus_path):
        text = (comment.get("text") or "").strip()
        cid = str(comment.get("id") or "")
        if not cid or cid in seen_ids:
            continue
        seen_ids.add(cid)
        kind = _comment_kind(text)
        if kind not in pools:
            continue
        pools[kind].append(
            {
                "comment_id": cid,
                "post_id": post.get("id"),
                "text": text,
                "text_norm": normalize_for_model(text),
                "kind": kind,
                "script": detect_script(text),
                "likes": int(comment.get("likes") or 0),
            }
        )

    for pool in pools.values():
        rng.shuffle(pool)

    rows: list[dict] = []
    for kind, share in STRATA.items():
        want = round(size * share)
        take = pools[kind][:want]
        if len(take) < want:
            print(
                f"  ! only {len(take)} {kind} comments available (wanted {want})",
                file=sys.stderr,
            )
        rows.extend(take)

    # Top up from the largest remaining pool if a stratum came up short, so the
    # file always holds `size` rows rather than silently fewer.
    if len(rows) < size:
        chosen = {r["comment_id"] for r in rows}
        leftovers = [
            r for pool in pools.values() for r in pool if r["comment_id"] not in chosen
        ]
        rng.shuffle(leftovers)
        rows.extend(leftovers[: size - len(rows)])

    rows.sort(key=lambda r: r["comment_id"])
    for i, row in enumerate(rows, 1):
        row["n"] = i
        # The two fields a human fills in. Never written by a machine.
        row["label"] = None
        row["label_notes"] = ""

    return {
        "_meta": {
            "purpose": (
                "Human-adjudicated gold labels for per-comment sentiment. "
                "`label` must be one of positive / negative / neutral / uncertain "
                "and must be set BY A PERSON — see the labelling protocol below."
            ),
            "protocol": [
                "Read the comment as a reply to its post, not in isolation.",
                "positive / negative = the commenter's attitude toward the POST's subject.",
                "neutral = a real judgement: informational, off-topic, or genuinely balanced.",
                "uncertain = you cannot tell from the text alone (sarcasm, missing context,"
                " or a fragment). Use it — a forced guess is worse than an abstention.",
                "Emoji-only comments ARE labellable: ❤️ is positive, 🤬 is negative.",
                "Do not look at any model's prediction before deciding.",
                "If two people disagree, keep both readings in label_notes and mark uncertain.",
            ],
            "corpus": corpus_path.name,
            "size": len(rows),
            "seed": SEED,
            "strata": STRATA,
            "composition": {
                "kind": dict(Counter(r["kind"] for r in rows)),
                "script": dict(Counter(r["script"] for r in rows)),
            },
            "labelled": 0,
            "generated_by": "python -m eval.build_gold_set",
            "scored_by": "python -m eval.score_gold",
        },
        "comments": rows,
    }


def merge_existing(fresh: dict, out_path: Path) -> dict:
    """Carry over labels already applied, matched by comment id.

    Regenerating must never destroy human work — that is the whole value of the
    file. Rows that disappear from the sample keep their labels in `_retired` so
    a changed stratification cannot silently discard adjudications.
    """
    if not out_path.exists():
        return fresh
    old = json.loads(out_path.read_text(encoding="utf-8"))
    old_by_id = {c["comment_id"]: c for c in old.get("comments", [])}
    carried = 0
    for row in fresh["comments"]:
        prev = old_by_id.pop(row["comment_id"], None)
        if prev and prev.get("label"):
            row["label"] = prev["label"]
            row["label_notes"] = prev.get("label_notes", "")
            carried += 1
    retired = [c for c in old_by_id.values() if c.get("label")]
    if retired:
        fresh["_retired"] = old.get("_retired", []) + retired
        print(f"  {len(retired)} labelled rows are no longer in the sample — kept in _retired")

    # Stored predictions are derived, but silently dropping them makes a
    # regeneration look like the scorer broke. Carry the rows still sampled.
    kept_ids = {r["comment_id"] for r in fresh["comments"]}
    old_preds = old.get("predictions") or {}
    if old_preds:
        fresh["predictions"] = {
            system: {cid: label for cid, label in preds.items() if cid in kept_ids}
            for system, preds in old_preds.items()
        }
        print(f"  carried over predictions for: {', '.join(fresh['predictions'])}")

    fresh["_meta"]["labelled"] = carried
    print(f"  carried over {carried} existing labels")
    return fresh


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--size", type=int, default=300)
    args = ap.parse_args()

    if not args.corpus.exists():
        print(f"corpus not found: {args.corpus}", file=sys.stderr)
        return 1

    print(f"sampling {args.size} comments from {args.corpus.name} (seed {SEED})")
    doc = build(args.corpus, args.size)
    doc = merge_existing(doc, args.out)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    meta = doc["_meta"]
    # --out may point outside the repo; relative_to raises there, and losing the
    # whole summary to a cosmetic path format would be silly.
    try:
        _where = args.out.relative_to(REPO)
    except ValueError:
        _where = args.out
    print(f"wrote {_where}")
    print(f"  {meta['size']} comments · {meta['labelled']} labelled · "
          f"{meta['size'] - meta['labelled']} awaiting adjudication")
    print(f"  kind:   {meta['composition']['kind']}")
    print(f"  script: {meta['composition']['script']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

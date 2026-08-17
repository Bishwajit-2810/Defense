"""Build the retrieval evaluation set — known-item queries with known answers.

Why this exists
---------------
There is no recall@k or MRR anywhere in this repo, so every statement about
retrieval quality — including the ones in RAG_STATE_AND_ROADMAP itself — is an
argument rather than a measurement. Hybrid fusion, reranking and comment vectors
are all changes to *ranking*, and a ranking change with no evaluation behind it
cannot be shown to help; it can only be assumed to.

What this set IS
----------------
**Known-item retrieval.** For each sampled post the script writes a query
describing that post, and records that post's id as the relevant answer. The
question it measures is "given a description of a post that exists, does the
retriever find it, and how far down?" — the standard proxy when there are no
human relevance judgements.

What this set is NOT
--------------------
It is **not** human relevance judgement, and every row says so
(``human_verified: false``). Three limits follow, and they are stated in the
file itself rather than left for a reader to discover:

1. **One relevant document per query, by construction.** Real recall needs all
   the relevant posts marked; here every OTHER post that also discusses the
   topic counts as a miss. So recall@k is a LOWER bound and MRR is pessimistic —
   useful for comparing retrievers against each other on the same set, not as an
   absolute quality number.
2. **The queries are derived from the analysis, not from operators.** They read
   like an analyst's question because they are built from `topics`, `entities`
   and the LLM-written `post_summary`, but nobody actually asked them.
3. **Leakage is bounded, not eliminated.** `post_summary` is indexed by the
   lexical arm, so a query lifted verbatim from it would be a gift to lexical
   search and a meaningless comparison. The builder therefore uses topics and
   entities — abstractions over the caption — and drops any query whose tokens
   are more than `--max-overlap` contained in the post's own indexed text.

Adjudicating it
---------------
A human can upgrade any row by adding post ids to `relevant_post_ids` and
setting `human_verified: true`. `score_retrieval.py` reports the verified subset
separately, so the set improves incrementally instead of needing 50 queries
adjudicated before it is worth anything.

Usage
-----
    python -m eval.build_retrieval_set                  # 50 queries from Postgres
    python -m eval.build_retrieval_set --size 100
    python -m eval.build_retrieval_set --corpus posts_with_details.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import re
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

DEFAULT_OUT = REPO / "eval" / "gold" / "retrieval_queries.json"

#: Fixed seed: re-running must produce the same queries, or a human's
#: adjudication of row 7 stops describing row 7.
SEED = 20260817

_WORD_RE = re.compile(r"[\wঀ-৿]+", re.UNICODE)


def _tokens(text: str) -> set[str]:
    return {t.casefold() for t in _WORD_RE.findall(text or "") if len(t) > 2}


# ---------------------------------------------------------------------------
# Query construction
# ---------------------------------------------------------------------------

#: Frames that turn a bag of topics into something shaped like an operator's
#: question. Varied so the set does not measure one phrasing.
_FRAMES = (
    "What are people saying about {subject}?",
    "Find posts about {subject}",
    "Public reaction to {subject}",
    "Which posts discuss {subject}?",
    "Sentiment around {subject}",
)


def _subject(result: dict) -> str | None:
    """A short subject phrase for one post, from its analysis.

    Entities first (a named person or organisation is the most discriminating
    thing a post has), then topics. The caption itself is deliberately unused:
    it is the indexed text, and a query copied out of it measures string
    matching rather than retrieval.
    """
    entities = [
        str(e.get("text") if isinstance(e, dict) else e).strip()
        for e in (result.get("entities") or [])
    ]
    entities = [e for e in entities if 2 < len(e) < 60]
    topics = [str(t).strip() for t in (result.get("topics") or []) if str(t).strip()]

    parts: list[str] = []
    parts.extend(entities[:2])
    parts.extend(t for t in topics[:2] if t.casefold() not in {p.casefold() for p in parts})
    if not parts:
        return None
    return " and ".join(parts[:3])


def _build_row(
    post_id: str,
    result: dict,
    frame: str,
    max_overlap: float,
) -> dict | None:
    """One evaluation row, or None if the query would be a giveaway."""
    subject = _subject(result)
    if not subject:
        return None
    query = frame.format(subject=subject)

    # Leakage guard: how much of the query already appears verbatim in the text
    # the lexical arm indexes. A query fully contained in the caption tells us
    # only that substring matching works.
    indexed = " ".join(
        [
            str(result.get("post_text") or ""),
            str(result.get("post_summary") or ""),
        ]
    )
    q_tokens = _tokens(subject)
    if not q_tokens:
        return None
    overlap = len(q_tokens & _tokens(indexed)) / len(q_tokens)
    if overlap > max_overlap:
        return None

    return {
        "query": query,
        "relevant_post_ids": [post_id],
        # Everything a reader needs to judge how much the number is worth.
        "human_verified": False,
        "derivation": "topics+entities of the target post",
        "lexical_overlap": round(overlap, 3),
        "subject": subject,
        "post_summary": (result.get("post_summary") or "")[:200],
    }


# ---------------------------------------------------------------------------
# Corpus sources
# ---------------------------------------------------------------------------


async def _from_postgres(limit: int) -> list[tuple[str, dict]]:
    """Read analysed posts straight from analysis_results."""
    from sqlalchemy import text as sql_text
    from sqlalchemy.ext.asyncio import create_async_engine

    from defense.libs.common.config import get_settings

    url = get_settings().database_url or ""
    for prefix in ("postgresql://", "postgres://"):
        if url.startswith(prefix):
            url = url.replace(prefix, "postgresql+asyncpg://", 1)
            break

    engine = create_async_engine(url, pool_pre_ping=True)
    try:
        async with engine.connect() as conn:
            rows = (
                await conn.execute(
                    sql_text(
                        """
                        SELECT DISTINCT ON (post_id) post_id, result
                        FROM analysis_results
                        ORDER BY post_id, created_at DESC
                        LIMIT :lim
                        """
                    ),
                    {"lim": limit},
                )
            ).mappings().all()
    finally:
        await engine.dispose()
    return [(r["post_id"], r["result"] or {}) for r in rows]


def _from_json(path: Path) -> list[tuple[str, dict]]:
    """Fall back to a raw corpus file when there is no database to read.

    The fields are thinner here — a raw post has no `topics` or `entities`, only
    a caption — so the subject is taken from the caption's longest words. That is
    a materially weaker query, and rows built this way say so in `derivation`.
    """
    posts = json.loads(path.read_text(encoding="utf-8"))
    out: list[tuple[str, dict]] = []
    for post in posts if isinstance(posts, list) else posts.values():
        pid = post.get("id")
        caption = post.get("caption") or ""
        if not pid or not caption:
            continue
        words = sorted({w for w in _WORD_RE.findall(caption) if len(w) > 4}, key=len, reverse=True)
        out.append(
            (
                pid,
                {
                    "topics": words[:3],
                    "entities": [],
                    "post_text": caption,
                    "post_summary": caption[:200],
                },
            )
        )
    return out


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--size", type=int, default=50, help="Number of queries to write.")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument(
        "--corpus",
        type=Path,
        default=None,
        help="Raw corpus JSON to use instead of Postgres.",
    )
    ap.add_argument(
        "--max-overlap",
        type=float,
        default=0.6,
        help="Drop a query whose tokens are more than this fraction present in "
        "the target's own indexed text (default 0.6).",
    )
    args = ap.parse_args(argv)

    if args.corpus:
        source = _from_json(args.corpus)
        derivation_note = "caption keywords (no analysis available)"
    else:
        try:
            source = asyncio.run(_from_postgres(limit=max(args.size * 10, 500)))
            derivation_note = "topics+entities from analysis_results"
        except Exception as exc:
            print(f"Postgres unavailable ({exc}); pass --corpus to build from a JSON file.")
            return 2

    if not source:
        print("No analysed posts found — nothing to build a retrieval set from.")
        return 2

    rng = random.Random(SEED)
    rng.shuffle(source)

    rows: list[dict] = []
    dropped_leaky = 0
    dropped_thin = 0
    for i, (post_id, result) in enumerate(source):
        if len(rows) >= args.size:
            break
        frame = _FRAMES[i % len(_FRAMES)]
        row = _build_row(post_id, result, frame, args.max_overlap)
        if row is None:
            if _subject(result) is None:
                dropped_thin += 1
            else:
                dropped_leaky += 1
            continue
        rows.append(row)

    payload = {
        "version": 1,
        "seed": SEED,
        "size": len(rows),
        "source": derivation_note,
        "human_verified_count": 0,
        "notes": [
            "KNOWN-ITEM retrieval set: one relevant post per query, derived from "
            "that post's own analysis. NOT human relevance judgement.",
            "recall@k is a LOWER bound: other posts on the same topic are "
            "unmarked and count as misses. Use it to compare retrievers on this "
            "set, not as an absolute quality figure.",
            "A human can upgrade a row by adding ids to relevant_post_ids and "
            "setting human_verified true; score_retrieval.py reports that subset "
            "separately.",
            f"Queries whose tokens overlapped the target's indexed text by more "
            f"than {args.max_overlap:.0%} were dropped ({dropped_leaky}); posts "
            f"with too little analysis to describe were skipped ({dropped_thin}).",
        ],
        "queries": rows,
    }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        f"Wrote {len(rows)} queries to {args.out} "
        f"(dropped {dropped_leaky} leaky, {dropped_thin} thin; from {len(source)} posts)"
    )
    if len(rows) < args.size:
        print(
            f"NOTE: asked for {args.size}, produced {len(rows)}. The corpus ran out "
            "of posts with usable topics/entities."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

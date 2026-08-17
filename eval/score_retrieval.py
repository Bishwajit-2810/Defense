"""Score retrieval: recall@k, MRR@k and nDCG@k, per retriever configuration.

This is the measurement RAG_STATE_AND_ROADMAP §3.8 says is missing — and the one
that makes hybrid fusion (§3.3) and reranking (§3.4) demonstrable instead of
plausible. It runs the SAME arm functions the MCP server serves agents from
(imported, not reimplemented), with the same RRF weights and the same per-arm
over-fetch, so what it measures is what the agents get. The one deliberate
difference: where `semantic_search` falls back to post-level vectors if the chunk
table is empty, the chunk configurations here fail loudly instead — a silent
fallback would report `chunked` as a chunking result when no chunk was read.

Configurations compared
-----------------------
    vector    post-level pgvector cosine kNN alone — what the system did before
    lexical   full-text + trigram alone
    chunk     chunk-level kNN, each post ranked by its best-matching passage
    hybrid    post-level vector + lexical, fused by reciprocal rank fusion
    chunked   chunk-level vector + lexical, fused the same way — the §3.5
              before/after against `hybrid`, holding everything else constant
    reranked  chunked, then a cross-encoder pass (only if RETRIEVAL_RERANK=true)

Reading the output
------------------
The absolute numbers are worth less than the differences between rows: the gold
set marks ONE relevant post per query, so every other on-topic post counts as a
miss and every figure is a lower bound (see build_retrieval_set.py). Comparing
configurations on the same set is exactly what it is good for.

The header line reports whether the corpus embeddings are stubs. If they are,
the `vector` row is measuring a hash function and the `hybrid` row is measuring
"lexical, with noise mixed in" — which is still the honest comparison to publish,
just not the one anyone wants.

Usage
-----
    python -m eval.score_retrieval
    python -m eval.score_retrieval --k 10 --set eval/gold/retrieval_queries.json
    python -m eval.score_retrieval --verified-only
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
from pathlib import Path
from typing import Any, Callable, Awaitable

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

DEFAULT_SET = REPO / "eval" / "gold" / "retrieval_queries.json"


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def recall_at_k(ranked: list[str], relevant: set[str], k: int) -> float:
    """Fraction of the relevant set that appears in the top k."""
    if not relevant:
        return 0.0
    return len(set(ranked[:k]) & relevant) / len(relevant)


def reciprocal_rank(ranked: list[str], relevant: set[str], k: int) -> float:
    """1/rank of the first relevant hit in the top k; 0 if there is none."""
    for i, doc_id in enumerate(ranked[:k], start=1):
        if doc_id in relevant:
            return 1.0 / i
    return 0.0


def ndcg_at_k(ranked: list[str], relevant: set[str], k: int) -> float:
    """Binary-gain nDCG@k.

    With one relevant document this collapses to 1/log2(rank+1) — nearly the
    same ordering signal as MRR. It is reported anyway because the set is
    designed to grow multi-relevant as humans adjudicate it, and a metric added
    later cannot be compared against runs made before it existed.
    """
    if not relevant:
        return 0.0
    dcg = sum(
        1.0 / math.log2(i + 1)
        for i, doc_id in enumerate(ranked[:k], start=1)
        if doc_id in relevant
    )
    ideal = sum(1.0 / math.log2(i + 1) for i in range(1, min(len(relevant), k) + 1))
    return dcg / ideal if ideal else 0.0


# ---------------------------------------------------------------------------
# Retriever configurations — the real arms, imported from the MCP server
# ---------------------------------------------------------------------------


async def _run_config(
    session: Any,
    name: str,
    query: str,
    pool: int,
    limit: int,
) -> list[str]:
    """Ranked post ids for one configuration."""
    from defense.libs.retrieval import fuse, rerank
    from defense.mcp_servers.retrieval_mcp import server as srv
    from defense.libs.embeddings import embed_text_with_provenance, to_pgvector_literal

    where_parts: list[str] = ["1=1"]
    params: dict[str, Any] = {}

    if name == "lexical":
        rows = await srv._lexical_arm(session, query, where_parts, params, pool)
        return [r["post_id"] for r in rows][:limit]

    qvec = to_pgvector_literal(embed_text_with_provenance(query)[0])

    # `chunked` and `reranked` swap the post-level vector arm for the chunk-level
    # one and change nothing else, so the difference against `hybrid` is
    # attributable to chunking rather than to a second simultaneous change.
    use_chunks = name in ("chunk", "chunked", "reranked")
    if use_chunks:
        vector_rows = await srv._chunk_arm(session, qvec, where_parts, params, pool)
        if not vector_rows:
            raise RuntimeError(
                "no chunk rows — post_chunks is empty or absent; run "
                "deploy/backfill_embeddings.py"
            )
    else:
        vector_rows = await srv._vector_arm(session, qvec, where_parts, params, pool)

    if name in ("vector", "chunk"):
        return [r["post_id"] for r in vector_rows][:limit]

    # Mirror the production path exactly: THREE arms, full-text and trigram
    # fused separately rather than merged into one lexical score. A harness that
    # fuses differently from the server measures a system nobody runs.
    fts_rows = await srv._fts_arm(session, query, where_parts, params, pool)
    trgm_rows = await srv._trgm_arm(session, query, where_parts, params, pool)
    lexical_rows = [*trgm_rows, *fts_rows]
    arms = {"vector": [r["post_id"] for r in vector_rows]}
    if fts_rows:
        arms["fts"] = [r["post_id"] for r in fts_rows]
    if trgm_rows:
        arms["trgm"] = [r["post_id"] for r in trgm_rows]
    # The same down-weight semantic_search applies to a hash-stub query vector.
    # A no-op with real embeddings (weight 1.0 either way) — it matters for the
    # stub-mode columns, which without it were measuring a fusion the server does
    # not run in stub mode and crediting the result to hybrid retrieval.
    _query_is_stub = embed_text_with_provenance(query)[1]
    fused = fuse(arms, weights={"vector": 0.2 if _query_is_stub else 1.0})

    if name in ("hybrid", "chunked"):
        return [pid for pid, _s, _a in fused][:limit]

    if name == "reranked":
        by_id = {r["post_id"]: r for r in [*lexical_rows, *vector_rows]}
        candidates = [
            {
                "post_id": pid,
                "text": (by_id.get(pid) or {}).get("matched_chunk")
                or (by_id.get(pid) or {}).get("post_text")
                or (by_id.get(pid) or {}).get("post_summary")
                or "",
            }
            for pid, _s, _a in fused[:pool]
        ]
        ranked, was_reranked = rerank(query, candidates, text_key="text", limit=limit)
        if not was_reranked:
            # Silently returning the fused order would report the reranker's
            # score as the fusion's. Say it did not run instead.
            raise RuntimeError(
                "reranker did not run (RETRIEVAL_RERANK is false, or the model "
                "is unavailable) — the 'reranked' row would duplicate 'hybrid'"
            )
        return [r["post_id"] for r in ranked][:limit]

    raise ValueError(f"unknown configuration {name!r}")


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


def _query_encoder_is_stub() -> bool:
    """Whether the QUERY vectors this run will produce are hash stubs.

    Separate from the corpus check below, and the more dangerous of the two. The
    corpus flag is read from a column, so stub rows in the database are visible.
    Nothing recorded whether the encoder embedding the QUERIES was real — and
    `uv run` without `--extra embeddings` leaves `sentence_transformers`
    uninstalled, at which point `embed_text_with_provenance` logs one line and
    falls back to a hash.

    Every dense arm then ranks noise while the table prints normally: `vector`
    and `chunk` collapse toward chance and `chunked` silently becomes `lexical`
    with a rounding error, which is a result somebody could publish as
    "chunking added nothing". Checked up front and reported in the header.
    """
    from defense.libs.embeddings import embed_text_with_provenance

    return embed_text_with_provenance("encoder provenance probe")[1]


async def _corpus_stub_share(session: Any) -> tuple[int, int]:
    """``(stub_vectors, total_vectors)`` — the caveat on every number below."""
    from sqlalchemy import text as sql_text

    row = (
        await session.execute(
            sql_text(
                """
                SELECT count(*) FILTER (WHERE COALESCE(embedding_is_stub, FALSE)) AS stubs,
                       count(*) AS total
                FROM analysis_results
                WHERE embedding IS NOT NULL
                """
            )
        )
    ).mappings().first()
    return (int(row["stubs"] or 0), int(row["total"] or 0)) if row else (0, 0)


async def run(
    queries: list[dict],
    configs: list[str],
    k: int,
    pool: int,
) -> dict:
    from defense.mcp_servers.retrieval_mcp import server as srv

    query_stub = _query_encoder_is_stub()

    async with srv._AsyncSessionLocal() as session:
        stubs, total = await _corpus_stub_share(session)

        results: dict[str, dict] = {}
        for name in configs:
            recalls: list[float] = []
            rrs: list[float] = []
            ndcgs: list[float] = []
            failures: list[str] = []
            for row in queries:
                relevant = set(row.get("relevant_post_ids") or [])
                if not relevant:
                    continue
                try:
                    ranked = await _run_config(session, name, row["query"], pool, k)
                except Exception as exc:
                    failures.append(str(exc))
                    break
                recalls.append(recall_at_k(ranked, relevant, k))
                rrs.append(reciprocal_rank(ranked, relevant, k))
                ndcgs.append(ndcg_at_k(ranked, relevant, k))

            if failures:
                results[name] = {"skipped": failures[0]}
                continue
            n = len(recalls) or 1
            results[name] = {
                "queries": len(recalls),
                f"recall@{k}": round(sum(recalls) / n, 4),
                f"mrr@{k}": round(sum(rrs) / n, 4),
                f"ndcg@{k}": round(sum(ndcgs) / n, 4),
            }

    return {
        "k": k,
        "candidate_pool": pool,
        "corpus_vectors": total,
        "corpus_stub_vectors": stubs,
        "query_encoder_is_stub": query_stub,
        "configs": results,
    }


def _print_report(report: dict, k: int, verified: int, total_q: int) -> None:
    stubs = report["corpus_stub_vectors"]
    vectors = report["corpus_vectors"]
    print()
    print("=" * 74)
    print("RETRIEVAL EVALUATION")
    print("=" * 74)
    print(f"queries: {total_q}  ({verified} human-verified)   k={k}   pool={report['candidate_pool']}")
    if report.get("query_encoder_is_stub"):
        print(
            "WARNING: the QUERY encoder is a hash stub — sentence_transformers is\n"
            "         not importable, so every dense arm below ranks NOISE. 'vector'\n"
            "         and 'chunk' measure a hash function and 'chunked' degenerates\n"
            "         to 'lexical'. Re-run with:  uv run --extra embeddings python -m\n"
            "         eval.score_retrieval    (this is NOT the same as the corpus\n"
            "         stub check below: the stored vectors can be perfectly real)."
        )
    if vectors and stubs:
        print(
            f"WARNING: {stubs}/{vectors} corpus vectors are hash STUBS. The "
            "'vector' row below measures a hash function, and every row that "
            "includes it is diluted by that."
        )
    elif not vectors:
        print("WARNING: no post embeddings in the database — the vector arm has nothing to search.")
    print("-" * 74)
    print(f"{'config':<12}{'queries':>9}{'recall@'+str(k):>13}{'mrr@'+str(k):>11}{'ndcg@'+str(k):>12}")
    print("-" * 74)
    for name, m in report["configs"].items():
        if "skipped" in m:
            print(f"{name:<12}{'skipped':>9}   {m['skipped'][:44]}")
            continue
        print(
            f"{name:<12}{m['queries']:>9}{m[f'recall@{k}']:>13.4f}"
            f"{m[f'mrr@{k}']:>11.4f}{m[f'ndcg@{k}']:>12.4f}"
        )
    print("-" * 74)
    print(
        "One relevant post per query by construction, so these are LOWER bounds. "
        "Compare rows against each other, not against 1.0."
    )
    print()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--set", dest="set_path", type=Path, default=DEFAULT_SET)
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument(
        "--pool",
        type=int,
        default=None,
        help="Candidates fetched per arm. Defaults to the same over-fetch "
             "semantic_search uses for this k (RETRIEVAL_CANDIDATE_MULTIPLIER), "
             "so the harness measures the server's pool rather than one of its own.",
    )
    ap.add_argument(
        "--configs",
        default="vector,lexical,chunk,hybrid,chunked,reranked",
        help="Comma-separated configurations to score.",
    )
    ap.add_argument(
        "--verified-only",
        action="store_true",
        help="Score only rows a human has adjudicated (human_verified true).",
    )
    ap.add_argument("--json", dest="json_out", type=Path, default=None)
    args = ap.parse_args(argv)

    if not args.set_path.exists():
        print(
            f"No retrieval set at {args.set_path}. Build one first:\n"
            "    python -m eval.build_retrieval_set"
        )
        return 2

    payload = json.loads(args.set_path.read_text(encoding="utf-8"))
    queries = payload.get("queries") or []
    verified = sum(1 for q in queries if q.get("human_verified"))
    if args.verified_only:
        queries = [q for q in queries if q.get("human_verified")]
        if not queries:
            print(
                "No human-verified rows in the set. Adjudicate some (set "
                "human_verified true) or drop --verified-only — but note that "
                "the unverified rows are derived, not judged."
            )
            return 2

    configs = [c.strip() for c in args.configs.split(",") if c.strip()]

    from defense.libs.retrieval import candidate_pool

    pool = args.pool if args.pool else candidate_pool(args.k)

    try:
        report = asyncio.run(run(queries, configs, args.k, pool))
    except Exception as exc:
        print(f"Retrieval evaluation could not run: {exc}")
        # The most common way this fails is DATABASE_URL pointing at port 5432 on
        # the host, where many machines run their OWN Postgres — which has no
        # `defense` role. The compose Postgres publishes on 5433 precisely to
        # avoid that collision. Printing the fix beats leaving it to be
        # rediscovered from an error that names neither port.
        if "defense" in str(exc) and "does not exist" in str(exc):
            print(
                "\nThat is almost certainly DATABASE_URL pointing at the wrong "
                "server: port 5432 on this host is probably its own Postgres, not "
                "the project's. The compose Postgres publishes on 5433:\n\n"
                "    DATABASE_URL=postgresql://defense:defense@localhost:5433/defense\n\n"
                "Set that in .env (it is the default in .env.example), or pass it "
                "for one run:\n\n"
                "    DATABASE_URL=postgresql://defense:defense@localhost:5433/defense \\\n"
                "        python -m eval.score_retrieval\n"
            )
        return 2

    report["human_verified"] = verified
    report["total_queries"] = len(queries)
    _print_report(report, args.k, verified, len(queries))

    if args.json_out:
        args.json_out.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"Wrote {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

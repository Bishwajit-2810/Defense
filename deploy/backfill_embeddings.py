"""Backfill post and comment vectors without re-running the pipeline.

Both kinds of vector are normally written at analysis time — the caption vector
by Stage 1 into `analysis_results.embedding`, the comment vectors by
assembler/persistence.py `_persist_comment_embeddings`. This script rewrites them
in place, which is what you need in three situations:

* **Rows analysed before comment vectors existed.** They have text in `comments`
  and nothing in `comment_embeddings`, so `search_comments` cannot reach them —
  and an empty search result reads as "nobody said that", a different and much
  worse claim than "this comment was never indexed".
* **After flipping EMBEDDING_STUB_MODE (or MODEL_STUB_MODE) to false.** Every
  existing row holds a hash stub. Leaving them means an index that mixes hashes
  and semantic vectors, whose cosine distances are computed, comparable and
  meaningless — `coverage_stats` will report the mixture, but only re-embedding
  fixes it.
* **After changing EMBEDDING_MODEL.** Same problem, same fix: `--all`.

It reuses the live code paths rather than reimplementing them — the same batch
encoder, the same near-duplicate grouping — so a backfilled row is
indistinguishable from one the pipeline wrote, including its `embedding_is_stub`,
`embedding_model` and `embedding_dim` provenance.

Idempotent: re-running replaces vectors rather than duplicating them.

Usage
-----
    python deploy/backfill_embeddings.py                    # whatever is missing
    python deploy/backfill_embeddings.py --all              # re-embed everything
    python deploy/backfill_embeddings.py --posts-only
    python deploy/backfill_embeddings.py --comments-only
    python deploy/backfill_embeddings.py --limit-posts 10   # a smoke test
    DATABASE_URL=postgresql://... python deploy/backfill_embeddings.py
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

from defense.libs.comment_groups import group_indices, representative_of  # noqa: E402
from defense.libs.common.config import get_settings  # noqa: E402
from defense.libs.embeddings import (  # noqa: E402
    STUB_MODEL_NAME,
    active_model_name,
    embed_texts_with_provenance,
    to_pgvector_literal,
)

_POST_UPDATE = text(
    """
    UPDATE analysis_results
       SET embedding         = CAST(:embedding AS vector),
           embedding_is_stub = :embedding_is_stub,
           embedding_model   = :embedding_model,
           embedding_dim     = :embedding_dim,
           updated_at        = NOW()
     WHERE post_id = :post_id
    """
)

_UPSERT = text(
    """
    INSERT INTO comment_embeddings
        (post_id, comment_id, campaign_id, tenant_id, embedding,
         embedding_is_stub, embedding_model, embedding_dim, represented_by)
    VALUES
        (:post_id, :comment_id, :campaign_id, :tenant_id, CAST(:embedding AS vector),
         :embedding_is_stub, :embedding_model, :embedding_dim, :represented_by)
    ON CONFLICT (post_id, comment_id) DO UPDATE SET
        campaign_id       = EXCLUDED.campaign_id,
        tenant_id         = EXCLUDED.tenant_id,
        embedding         = EXCLUDED.embedding,
        embedding_is_stub = EXCLUDED.embedding_is_stub,
        embedding_model   = EXCLUDED.embedding_model,
        embedding_dim     = EXCLUDED.embedding_dim,
        represented_by    = EXCLUDED.represented_by
    """
)


def _async_url(url: str) -> str:
    for prefix in ("postgresql://", "postgres://"):
        if url.startswith(prefix):
            return url.replace(prefix, "postgresql+asyncpg://", 1)
    return url


async def _backfill_posts(
    conn, only_missing: bool, limit_posts: int | None, upgrade_stubs: bool = False
) -> tuple[int, bool]:
    """Re-embed caption vectors on analysis_results. Returns ``(rows, is_stub)``.

    The source text is `result->>'post_text'` — the same caption Stage 1 passes
    to `analyze_text`, so a backfilled vector is the one the pipeline would have
    written. Falls back to `post_summary` for the null-caption image posts, which
    otherwise contribute nothing to embed and would keep their stub silently.
    """
    if not only_missing:
        missing_clause = ""
    elif upgrade_stubs:
        missing_clause = "AND (embedding IS NULL OR COALESCE(embedding_is_stub, FALSE))"
    else:
        missing_clause = "AND embedding IS NULL"
    rows = (
        await conn.execute(
            text(
                f"""
                SELECT post_id,
                       COALESCE(NULLIF(result->>'post_text', ''),
                                NULLIF(result->>'post_summary', '')) AS src
                FROM analysis_results
                WHERE COALESCE(NULLIF(result->>'post_text', ''),
                               NULLIF(result->>'post_summary', '')) IS NOT NULL
                {missing_clause}
                ORDER BY post_id
                {'LIMIT :lim' if limit_posts else ''}
                """
            ),
            {"lim": limit_posts} if limit_posts else {},
        )
    ).mappings().all()
    if not rows:
        return 0, False

    vectors, is_stub = embed_texts_with_provenance([r["src"] for r in rows])
    model_name = STUB_MODEL_NAME if is_stub else active_model_name()
    await conn.execute(
        _POST_UPDATE,
        [
            {
                "post_id": r["post_id"],
                "embedding": to_pgvector_literal(v),
                "embedding_is_stub": is_stub,
                "embedding_model": model_name,
                "embedding_dim": len(v),
            }
            for r, v in zip(rows, vectors)
        ],
    )
    return len(rows), is_stub


_CHUNK_INSERT = text(
    """
    INSERT INTO post_chunks
        (post_id, chunk_idx, campaign_id, tenant_id, text, char_start,
         embedding, embedding_is_stub, embedding_model, embedding_dim)
    VALUES
        (:post_id, :chunk_idx, :campaign_id, :tenant_id, :text, :char_start,
         CAST(:embedding AS vector), :embedding_is_stub, :embedding_model, :embedding_dim)
    """
)


async def _backfill_chunks(
    conn, only_missing: bool, limit_posts: int | None, upgrade_stubs: bool = False
) -> tuple[int, int, bool]:
    """Chunk captions and store one vector per chunk. ``(posts, chunks, is_stub)``.

    Delete-then-insert per post, matching the live path: re-chunking an edited
    caption can produce fewer pieces than before, and an upsert would strand the
    surplus tail rows in the index still describing text the post no longer has.
    """
    from defense.libs.chunking import chunk_text

    if not only_missing:
        missing_clause = ""
    elif upgrade_stubs:
        missing_clause = """
        AND NOT EXISTS (
            SELECT 1 FROM post_chunks pc
            WHERE pc.post_id = ar.post_id
              AND NOT COALESCE(pc.embedding_is_stub, FALSE)
        )
        """
    else:
        missing_clause = (
            "AND NOT EXISTS (SELECT 1 FROM post_chunks pc WHERE pc.post_id = ar.post_id)"
        )

    rows = (
        await conn.execute(
            text(
                f"""
                SELECT ar.post_id, ar.campaign_id, ar.tenant_id,
                       COALESCE(NULLIF(ar.result->>'post_text', ''),
                                NULLIF(ar.result->>'post_summary', '')) AS src
                FROM analysis_results ar
                WHERE COALESCE(NULLIF(ar.result->>'post_text', ''),
                               NULLIF(ar.result->>'post_summary', '')) IS NOT NULL
                {missing_clause}
                ORDER BY ar.post_id
                {'LIMIT :lim' if limit_posts else ''}
                """
            ),
            {"lim": limit_posts} if limit_posts else {},
        )
    ).mappings().all()
    if not rows:
        return 0, 0, False

    total_chunks = 0
    is_stub = False
    for row in rows:
        chunks = chunk_text(row["src"])
        if not chunks:
            continue
        vectors, is_stub = embed_texts_with_provenance([c.text for c in chunks])
        model_name = STUB_MODEL_NAME if is_stub else active_model_name()
        await conn.execute(
            text("DELETE FROM post_chunks WHERE post_id = :pid"), {"pid": row["post_id"]}
        )
        await conn.execute(
            _CHUNK_INSERT,
            [
                {
                    "post_id": row["post_id"],
                    "chunk_idx": c.idx,
                    "campaign_id": row["campaign_id"],
                    "tenant_id": row["tenant_id"] or "default",
                    "text": c.text,
                    "char_start": c.start,
                    "embedding": to_pgvector_literal(v),
                    "embedding_is_stub": is_stub,
                    "embedding_model": model_name,
                    "embedding_dim": len(v),
                }
                for c, v in zip(chunks, vectors)
            ],
        )
        total_chunks += len(chunks)

    return len(rows), total_chunks, is_stub


async def _post_ids(
    conn, only_missing: bool, limit_posts: int | None, upgrade_stubs: bool = False
) -> list[str]:
    """Posts that have comment text, optionally only those with no vectors yet.

    ``upgrade_stubs`` widens "missing" to include posts whose vectors are hashes.
    It is set when a real model is loaded, so a run right after the stub-mode
    flip upgrades the corpus without needing ``--all`` — and a run in stub mode
    does not pointlessly rewrite hashes with identical hashes.
    """
    if not only_missing:
        missing_clause = ""
    elif upgrade_stubs:
        # A post counts as done only if it has a NON-stub vector. After the flip
        # the rows exist but hold hashes, and treating those as present would
        # leave the index permanently half-hashed.
        missing_clause = """
        AND NOT EXISTS (
            SELECT 1 FROM comment_embeddings ce
            WHERE ce.post_id = c.post_id
              AND NOT COALESCE(ce.embedding_is_stub, FALSE)
        )
        """
    else:
        missing_clause = (
            "AND NOT EXISTS (SELECT 1 FROM comment_embeddings ce "
            "WHERE ce.post_id = c.post_id)"
        )
    sql = text(
        f"""
        SELECT DISTINCT c.post_id
        FROM comments c
        WHERE c.text IS NOT NULL AND c.text <> ''
        {missing_clause}
        ORDER BY c.post_id
        {'LIMIT :lim' if limit_posts else ''}
        """
    )
    params = {"lim": limit_posts} if limit_posts else {}
    return [r[0] for r in (await conn.execute(sql, params)).all()]


async def _embed_post(conn, post_id: str) -> tuple[int, int, bool]:
    """Embed one post's thread. Returns ``(rows, encoded, is_stub)``."""
    rows = (
        await conn.execute(
            text(
                """
                SELECT c.comment_id, c.text, p.campaign_id, p.tenant_id
                FROM comments c
                JOIN posts p ON p.id = c.post_id
                WHERE c.post_id = :pid AND c.text IS NOT NULL AND c.text <> ''
                ORDER BY c.comment_id
                """
            ),
            {"pid": post_id},
        )
    ).mappings().all()
    if not rows:
        return 0, 0, False

    cap = get_settings().comment_embedding_max_per_post
    if cap and len(rows) > cap:
        rows = rows[:cap]

    texts = [r["text"] for r in rows]
    # The same grouping the LLM path uses: "সুন্দর" three hundred times under one
    # post is one encoder call, not three hundred.
    groups, _stats = group_indices(texts)
    rep_of = representative_of(groups)
    encode_idx = [i for i in range(len(rows)) if i not in rep_of]

    vectors, is_stub = embed_texts_with_provenance([texts[i] for i in encode_idx])
    by_index = dict(zip(encode_idx, vectors))
    model_name = STUB_MODEL_NAME if is_stub else active_model_name()

    payload = []
    for i, row in enumerate(rows):
        source = rep_of.get(i, i)
        vec = by_index.get(source)
        if vec is None:  # pragma: no cover
            continue
        payload.append(
            {
                "post_id": post_id,
                "comment_id": row["comment_id"],
                "campaign_id": row["campaign_id"],
                "tenant_id": row["tenant_id"] or "default",
                "embedding": to_pgvector_literal(vec),
                "embedding_is_stub": is_stub,
                "embedding_model": model_name,
                "embedding_dim": len(vec),
                "represented_by": rows[source]["comment_id"] if source != i else None,
            }
        )

    await conn.execute(_UPSERT, payload)
    return len(payload), len(encode_idx), is_stub


async def main_async(args: argparse.Namespace) -> int:
    url = _async_url(args.database_url or get_settings().database_url or "")
    engine = create_async_engine(url, pool_pre_ping=True)

    # Which mode we are writing IN decides what counts as needing a backfill.
    # A real model means existing stubs are stale; a stub model means rewriting
    # a hash with the identical hash, which is pure work for no change.
    model_name = active_model_name()
    upgrade_stubs = model_name != STUB_MODEL_NAME
    print(f"Embedding model: {model_name}")
    if upgrade_stubs:
        print("Real model loaded — existing STUB vectors will be upgraded.")

    total_rows = total_encoded = post_rows = chunk_rows = 0
    stub_seen = False
    started = time.monotonic()
    try:
        async with engine.begin() as conn:
            if not args.comments_only:
                post_rows, post_stub = await _backfill_posts(
                    conn, not args.all, args.limit_posts, upgrade_stubs
                )
                stub_seen = stub_seen or (post_stub and post_rows > 0)
                print(f"Post caption vectors: {post_rows} rewritten")

            if not args.comments_only:
                chunk_posts, chunk_rows, chunk_stub = await _backfill_chunks(
                    conn, not args.all, args.limit_posts, upgrade_stubs
                )
                stub_seen = stub_seen or (chunk_stub and chunk_rows > 0)
                print(f"Caption chunks: {chunk_rows} across {chunk_posts} post(s)")

            if not args.posts_only:
                post_ids = await _post_ids(
                    conn, not args.all, args.limit_posts, upgrade_stubs
                )
                if post_ids:
                    print(f"Embedding comments for {len(post_ids)} post(s)…")
                    for n, post_id in enumerate(post_ids, start=1):
                        rows, encoded, is_stub = await _embed_post(conn, post_id)
                        total_rows += rows
                        total_encoded += encoded
                        stub_seen = stub_seen or is_stub
                        if n % 10 == 0 or n == len(post_ids):
                            print(f"  {n}/{len(post_ids)} posts — {total_rows} vectors")
                else:
                    print("Comment vectors: nothing missing.")
    finally:
        await engine.dispose()

    if not post_rows and not total_rows and not chunk_rows:
        print(
            "\nNothing to backfill. Use --all to re-embed regardless (e.g. after "
            "changing EMBEDDING_MODEL)."
        )
        return 0

    elapsed = time.monotonic() - started
    print(
        f"\nDone in {elapsed:.1f}s: {post_rows} post vectors, {chunk_rows} chunk "
        f"vectors, {total_rows} comment vectors from {total_encoded} encoder inputs "
        f"({total_rows - total_encoded} propagated from near-duplicates)"
    )
    if stub_seen:
        print(
            "\nWARNING: these are STUB vectors (deterministic hashes, not semantic).\n"
            "Semantic search and clustering will return arbitrary neighbours; only\n"
            "the lexical arm of hybrid retrieval is meaningful. To fix:\n"
            "    uv sync --extra embeddings      # or --extra ml\n"
            "    .env: EMBEDDING_STUB_MODE=false, HF_OFFLINE=false\n"
            "    python deploy/backfill_embeddings.py"
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--all", action="store_true", help="Re-embed everything, not just what is missing or stubbed.")
    ap.add_argument("--posts-only", action="store_true", help="Caption vectors only.")
    ap.add_argument("--comments-only", action="store_true", help="Comment vectors only.")
    ap.add_argument("--limit-posts", type=int, default=None, help="Stop after this many posts.")
    ap.add_argument("--database-url", default=None, help="Override DATABASE_URL.")
    args = ap.parse_args(argv)
    if args.posts_only and args.comments_only:
        ap.error("--posts-only and --comments-only are mutually exclusive")
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())

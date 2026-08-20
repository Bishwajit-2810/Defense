"""
Persistence layer for the defense assembler.

Four async functions — one per storage backend — each accepting a
schema-validated AnalysisResult dict and the appropriate client/engine
instance.  The assembler calls them all in parallel via asyncio.gather.

Backends
--------
- PostgreSQL  (SQLAlchemy async)  — canonical result row + pgvector embedding
                                    (semantic search), job status
- ClickHouse  (clickhouse-driver, sync wrapped in executor) — analytics row
- MinIO/S3    (boto3, sync wrapped in executor) — raw JSON blob

Semantic search lives in Postgres via the pgvector extension: the embedding is
written into analysis_results.embedding (vector column) in the same upsert as
the canonical result. This replaces the former standalone Qdrant service.
"""

from __future__ import annotations

import asyncio
from defense.libs.common.config import get_settings
import json
import structlog
import os
import sys
from datetime import datetime, timezone

# Repo root on path for `libs.*` (no-op when PYTHONPATH already provides it).

from defense.libs.embeddings import (  # noqa: E402
    EMBEDDING_DIM,
    STUB_MODEL_NAME,
    active_model_name,
    embed_texts_with_provenance,
    stub_embedding,
    to_pgvector_literal,
)
from defense.libs.repos.posts import PostRepository

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

#: Refuse to persist a non-semantic vector unless explicitly allowed.
#: Default ON (permissive) because offline/CI runs depend on the column being
#: populated — but set EMBEDDING_ALLOW_STUB=false before demoing semantic search
#: or cluster reports, and the write fails loudly instead of quietly seeding the
# If we can't fetch embeddings, use a mock so DBs don't blow up
_ALLOW_STUB_EMBEDDING = get_settings().embedding_allow_stub


class StubEmbeddingRefused(RuntimeError):
    """Raised when a stub vector would be persisted but stubs are disallowed."""


def _resolve_embedding(
    embedding: list | None,
    post_id: str,
    is_stub: bool | None = None,
) -> tuple[list[float], bool]:
    """Return ``(vector, is_stub)`` for the semantic-search column.

    Uses the Stage-1 embedding when present and of the expected dimension;
    otherwise falls back to the shared deterministic stub (seeded from the
    post_id) so the column is always populated and round-trips still work.

    **The second element is the point.** A stub vector is "deterministic unit
    vector seeded from the text hash (not semantic)" — kNN over a table of them
    returns arbitrary neighbours, and nothing downstream could previously tell,
    because the row looked identical to a real one. The flag travels with the
    row so search and report paths can say so.

    ``is_stub`` is the **producer's** answer, and it wins whenever it is given.
    This function used to derive the flag from the vector's *dimension* alone,
    reasoning that a correctly-sized vector must be a real one. It is not: the
    stub is EMBEDDING_DIM-sized by construction, so with ``MODEL_STUB_MODE=true``
    — the default — every row the pipeline wrote was recorded as
    ``embedding_is_stub = FALSE``, the inverse of the truth, for the entire
    corpus (PROJECT_ASSESSMENT §13.2). The dimension heuristic survives only as
    the fallback for callers that do not pass the flag.
    """
    if embedding and len(embedding) == EMBEDDING_DIM:
        # A usable vector. Whether it is semantic is the producer's to say —
        # Stage 1 knows, this layer cannot.
        if is_stub and not _ALLOW_STUB_EMBEDDING:
            # The gap that made EMBEDDING_ALLOW_STUB=false a weaker switch than
            # it reads as (RAG_STATE_AND_ROADMAP §3.1). The refusal below used
            # to sit only on the "no usable vector" path, so it fired when the
            # producer sent NOTHING and stayed silent in the case the flag
            # exists for: a correctly-sized, correctly-flagged hash stub, which
            # is what every post produces in the default configuration. An
            # operator who set the flag to keep noise out of the index got a
            # full index of noise and no error.
            raise StubEmbeddingRefused(
                f"post {post_id}: refusing to persist a non-semantic stub embedding "
                "(EMBEDDING_ALLOW_STUB=false). Stage 1 flagged this vector as the "
                "deterministic hash stub. Set MODEL_STUB_MODE=false and install the "
                "embedding model (`uv sync --extra ml`), or re-enable stubs knowing "
                "that semantic search will return arbitrary neighbours."
            )
        return [float(v) for v in embedding], bool(is_stub)
    if embedding:
        log.warning(
            "stage1 embedding dim %d != EMBEDDING_DIM %d for post %s — using stub",
            len(embedding), EMBEDDING_DIM, post_id,
        )
    if not _ALLOW_STUB_EMBEDDING:
        raise StubEmbeddingRefused(
            f"post {post_id}: refusing to persist a non-semantic stub embedding "
            "(EMBEDDING_ALLOW_STUB=false). Load a real embedding model, or "
            "re-enable stubs knowing that semantic search will return noise."
        )
    return stub_embedding(post_id), True


def _iso_to_dt(ts: str | None) -> datetime | None:
    """Parse an ISO-8601 datetime string to a timezone-aware datetime.

    Returns None when ts is None or unparseable.
    """
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _sentiment_label(value: object) -> str | None:
    """Extract the label from a {label, score} component sentiment (or None).

    The canonical JSON carries the {label, score} object, but the ClickHouse
    analysis_events.text_sentiment/image_sentiment columns are Nullable(String)
    for fast group-by — so analytics stores just the label.
    """
    if isinstance(value, dict):
        return value.get("label")
    if isinstance(value, str):
        return value
    return None


# ---------------------------------------------------------------------------
# PostgreSQL
# ---------------------------------------------------------------------------

async def persist_postgres(
    result: dict,
    engine,
    embedding: list | None = None,
    embedding_is_stub: bool | None = None,
) -> None:
    """Upsert the canonical result + semantic-search embedding into analysis_results.

    The pgvector ``embedding`` column is written in the same upsert (this is the
    semantic-search index that replaced Qdrant). Uses ON CONFLICT (post_id) DO
    UPDATE so re-assembling a post is idempotent — the latest result wins.

    Parameters
    ----------
    result:
        Schema-validated AnalysisResult dict.
    engine:
        An async SQLAlchemy engine (created with ``create_async_engine``).
    embedding:
        The Stage-1 document embedding (``stage1_result["embedding"]``). The
        canonical result itself stays schema-pure, so the vector is passed
        alongside it rather than embedded in it.
    embedding_is_stub:
        Whether that vector is the deterministic hash stub, **as reported by the
        component that produced it**. Omit it and the dimension heuristic
        applies, which is wrong for every stub (§13.2).
    """
    from sqlalchemy.ext.asyncio import AsyncSession

    post_id: str = result["post_id"]
    campaign_id: str = result["campaign_id"]
    tenant_id: str = result.get("tenant_id") or "default"
    schema_version: str = (result.get("processing") or {}).get("schema_version", "")
    embedding_vec, embedding_is_stub = _resolve_embedding(
        embedding, post_id, embedding_is_stub
    )
    # Which vector space this row lands in. Derived from the provenance flag
    # rather than from the configured model name: with MODEL_STUB_MODE=false and
    # the library missing, `EMBEDDING_MODEL` names a model that produced nothing.
    embedding_model = STUB_MODEL_NAME if embedding_is_stub else active_model_name()

    if embedding_is_stub:
        log.debug("persisting stub (non-semantic) embedding", post_id=post_id)

    async with engine.begin() as conn:
        session = AsyncSession(bind=conn)
        repo = PostRepository(session)
        await repo.upsert_analysis(
            post_id=post_id,
            campaign_id=campaign_id,
            result_json=result,
            embedding=embedding_vec,
            embedding_is_stub=embedding_is_stub,
            schema_version=schema_version,
            tenant_id=tenant_id,
            embedding_model=embedding_model,
            embedding_dim=len(embedding_vec),
        )
        await _persist_post_chunks(
            repo, session, result, campaign_id=campaign_id, tenant_id=tenant_id
        )
        await _persist_comment_embeddings(
            repo, session, result, campaign_id=campaign_id, tenant_id=tenant_id
        )

    log.debug("postgres: upserted", post_id=post_id, embedding=True)


# Why every enrichment write below is wrapped in `session.begin_nested()`.
#
# `persist_postgres` opens ONE connection-level transaction and binds a Session
# to it, so the Session joins that transaction rather than owning one
# (SQLAlchemy's `conditional_savepoint` mode). Postgres aborts an entire
# transaction on any statement error, and a Session joined this way has no
# savepoint of its own to retreat to — so catching an enrichment's database
# error and returning left the transaction poisoned. The outer commit silently
# became a rollback and took the analysis row and the chunks with it: a post
# whose comment vectors failed lost its canonical result entirely, with
# `postgres: upserted` logged on the way out.
#
# `session.rollback()` is NOT the fix — in this mode it unwinds the whole joined
# transaction, which is the same data loss by a shorter route. An explicit
# SAVEPOINT is: `begin_nested()` rolls back only the statements inside it and
# leaves the transaction usable, which is what "enrichment failure is logged and
# swallowed" has to mean if the analysis row is to survive it.
def _log_enrichment_failure(what: str, post_id: str, exc: Exception) -> None:
    """Report an enrichment write that was rolled back to its savepoint."""
    log.warning(
        f"{what}_failed",
        post_id=post_id,
        error=str(exc),
        detail="rolled back to savepoint; the analysis row for this post is unaffected",
    )


# ---------------------------------------------------------------------------
# Chunk vectors (RAG_STATE_AND_ROADMAP §3.5)
# ---------------------------------------------------------------------------


async def _persist_post_chunks(
    repo: PostRepository,
    session,
    result: dict,
    campaign_id: str | None,
    tenant_id: str,
) -> int:
    """Split this post's caption and store one vector per chunk.

    The source is `post_text` — the caption Stage 1 embeds — falling back to
    `post_summary` for null-caption image posts, which otherwise get no chunk row
    at all and would be invisible to chunk retrieval while looking present.

    Swallows its own failures for the same reason the comment path does: chunks
    are an enrichment over a post-level vector that has already been written, and
    a post whose caption could not be chunked is still a correctly analysed post.
    A database failure goes through :func:`_abandon_enrichment`, without which
    swallowing it would discard the analysis row too.
    """
    if not get_settings().retrieval_chunks:
        return 0

    post_id: str = result["post_id"]
    source = (result.get("post_text") or "").strip() or (
        result.get("post_summary") or ""
    ).strip()
    if not source:
        return 0

    from defense.libs.chunking import chunk_text

    chunks = chunk_text(source)
    if not chunks:
        return 0

    try:
        vectors, is_stub = embed_texts_with_provenance([c.text for c in chunks])
    except Exception as exc:
        log.warning("post_chunk_embedding_failed", post_id=post_id, error=str(exc))
        return 0

    model_name = STUB_MODEL_NAME if is_stub else active_model_name()
    rows = [
        {
            "post_id": post_id,
            "chunk_idx": c.idx,
            "campaign_id": campaign_id,
            "tenant_id": tenant_id,
            "text": c.text,
            "char_start": c.start,
            "embedding": vec,
            "embedding_is_stub": is_stub,
            "embedding_model": model_name,
            "embedding_dim": len(vec),
        }
        for c, vec in zip(chunks, vectors)
    ]

    try:
        async with session.begin_nested():
            await repo.replace_post_chunks(post_id, rows)
    except Exception as exc:
        _log_enrichment_failure("post_chunk_upsert", post_id, exc)
        return 0

    log.debug(
        "postgres: post chunks written",
        post_id=post_id, chunks=len(rows), chars=len(source), is_stub=is_stub,
    )
    return len(rows)


# ---------------------------------------------------------------------------
# Comment vectors (RAG_STATE_AND_ROADMAP §3.2)
# ---------------------------------------------------------------------------


async def _persist_comment_embeddings(
    repo: PostRepository,
    session,
    result: dict,
    campaign_id: str | None,
    tenant_id: str,
) -> int:
    """Embed this post's comments and upsert them into ``comment_embeddings``.

    Runs on the SAME session as the analysis upsert, inside the assembler's
    existing transaction, so a post never lands with an analysis row and no
    comment vectors — or the reverse.

    Two economies, both of which matter at 10k comments:

    * **One model call per thread.** ``embed_texts_with_provenance`` batches the
      whole post through the transformer; a loop over ``embed_text`` would pay
      the per-call overhead once per comment.
    * **Near-duplicates share a vector.** ``comment_groups.group_indices`` is
      already the corpus's answer to "সুন্দর" appearing three hundred times
      under one post — the same grouping that keeps those off the LLM path keeps
      them off the encoder. The duplicates are still stored, pointing at the
      representative via ``represented_by``, because a comment with no row is a
      comment that comment search cannot find.

    Failure here is logged and swallowed. Comment vectors are an enrichment: a
    post whose thread could not be embedded is still a correctly analysed post,
    and taking down the assembler's Postgres write over it would trade the
    canonical result for the index. A DATABASE failure has to go through
    :func:`_abandon_enrichment` for that to hold — catching it without rolling
    back to the savepoint made the swallow do the very thing described above.
    """
    if not get_settings().comment_embeddings_enabled:
        return 0

    comments = ((result.get("comment_analysis") or {}).get("comments")) or []
    post_id: str = result["post_id"]

    # A comment with no id cannot be keyed, and one with no text cannot be
    # embedded into anything meaningful.
    usable = [
        (str(c.get("id") or "").strip(), str(c.get("text") or "").strip())
        for c in comments
    ]
    usable = [(cid, txt) for cid, txt in usable if cid and txt]
    if not usable:
        return 0

    cap = get_settings().comment_embedding_max_per_post
    truncated = 0
    if cap and len(usable) > cap:
        truncated = len(usable) - cap
        usable = usable[:cap]
        # WARNING, not debug. This is the same class of defect §3.6 was about:
        # a cap that drops the tail of a thread and reports it only at a level
        # nobody runs in production leaves comment search unable to reach those
        # comments while `comment_vector_coverage` looks like an encoder
        # shortfall. `coverage_stats` derives and discloses the corpus-wide
        # version of this from the cap and the `comments` table.
        log.warning(
            "comment_embedding_truncated",
            post_id=post_id, cap=cap, dropped=truncated, usable=len(usable) + truncated,
            detail="comments beyond the cap have no vector and are unreachable "
                   "by search_comments; raise COMMENT_EMBEDDING_MAX_PER_POST to embed them",
        )

    from defense.libs.comment_groups import group_indices, representative_of

    texts = [txt for _cid, txt in usable]
    groups, _stats = group_indices(texts)
    rep_of = representative_of(groups)

    # Only the representatives (and the ungrouped) go through the encoder.
    encode_idx = [i for i in range(len(usable)) if i not in rep_of]
    try:
        vectors, is_stub = embed_texts_with_provenance([texts[i] for i in encode_idx])
    except Exception as exc:
        log.warning(
            "comment_embedding_failed", post_id=post_id, error=str(exc),
            comments=len(usable),
        )
        return 0

    by_index = dict(zip(encode_idx, vectors))
    model_name = STUB_MODEL_NAME if is_stub else active_model_name()

    rows: list[dict] = []
    for i, (comment_id, _txt) in enumerate(usable):
        source = rep_of.get(i, i)
        vec = by_index.get(source)
        if vec is None:  # pragma: no cover — every source index was encoded
            continue
        rows.append(
            {
                "post_id": post_id,
                "comment_id": comment_id,
                "campaign_id": campaign_id,
                "tenant_id": tenant_id,
                "embedding": vec,
                "embedding_is_stub": is_stub,
                "embedding_model": model_name,
                "embedding_dim": len(vec),
                "represented_by": usable[source][0] if source != i else None,
            }
        )

    try:
        async with session.begin_nested():
            await repo.upsert_comment_embeddings(rows)
    except Exception as exc:
        _log_enrichment_failure("comment_embedding_upsert", post_id, exc)
        return 0

    log.debug(
        "postgres: comment vectors upserted",
        post_id=post_id,
        rows=len(rows),
        encoded=len(encode_idx),
        propagated=len(rows) - len(encode_idx),
        truncated=truncated,
        is_stub=is_stub,
    )
    return len(rows)


# ---------------------------------------------------------------------------
# ClickHouse
# ---------------------------------------------------------------------------

#: Reaction types the analytics reaction-mix aggregate reports on, in the order
#: the ClickHouse columns are declared. The canonical result's
#: `reaction_breakdown` is lower-cased by the normalizer, but Stage 1 re-emits it
#: upper-cased, so the lookup below is case-insensitive rather than trusting
#: either.
_REACTION_TYPES: tuple[str, ...] = ("like", "love", "haha", "wow", "sad", "angry", "care")


def _reaction_columns(result: dict) -> dict[str, int]:
    """Map `reaction_breakdown` onto the per-type ClickHouse columns.

    These columns exist because `analytics_mcp.get_reaction_mix` queried a
    `reaction_events` table that no migration created and no writer populated —
    the tool raised in any non-stub deployment. The data was always available on
    the canonical result; it simply had nowhere to land.
    """
    raw = result.get("reaction_breakdown") or {}
    lowered = {str(k).lower(): v for k, v in raw.items()} if isinstance(raw, dict) else {}
    out: dict[str, int] = {}
    for kind in _REACTION_TYPES:
        try:
            out[f"{kind}_count"] = int(lowered.get(kind) or 0)
        except (TypeError, ValueError):
            out[f"{kind}_count"] = 0
    return out


def _clickhouse_insert_sync(result: dict, ch_client) -> None:
    """Synchronous ClickHouse insert (called via executor)."""
    eng = result.get("engagement", {})
    proc = result.get("processing", {})
    comment_analysis = result.get("comment_analysis", {})

    post_id: str = result["post_id"]
    campaign_id: str = result["campaign_id"]
    tenant_id: str = result.get("tenant_id") or "default"
    platform: str = result["platform"]
    media_type: str = result["media_type"]
    language: str = result["language"]
    overall_sentiment: str = result["overall_sentiment"]
    sentiment_score: float = float(result.get("sentiment_score", 0.0))
    toxicity_score: float = float(result.get("toxicity_score", 0.0))
    hate_speech_score: float = float(result.get("hate_speech_score", 0.0))
    comment_count: int = int(eng.get("comment_count", 0))
    total_reactions: int = int(eng.get("total_reactions", 0))
    stored_comments: int = int(eng.get("stored_comments", 0))
    coverage: float = float(comment_analysis.get("coverage", 0.0))
    llm_used: bool = bool(proc.get("llm_used", False))
    ensemble: dict = comment_analysis.get("ensemble") or {}
    # No `llm_model` here: `analysis_events` has `llm_backend` but no model
    # column. This used to read `proc["llm_model"]` into a local that was never
    # put in the row — dead since the column list was written. Per-model token
    # spend is dimensioned in Redis instead (`usage:tokens:{backend}:{model}`,
    # §5.8), which is where the cost breakdown is actually answered.

    # Parse timestamps; fall back to epoch if unparseable
    created_at_dt = _iso_to_dt(result.get("created_at")) or datetime(1970, 1, 1, tzinfo=timezone.utc)
    scraped_at_dt = _iso_to_dt(result.get("scraped_at")) or datetime(1970, 1, 1, tzinfo=timezone.utc)

    row = {
        "post_id": post_id,
        "campaign_id": campaign_id,
        "tenant_id": tenant_id,
        "platform": platform,
        "media_type": media_type,
        "language": language,
        "overall_sentiment": overall_sentiment,
        "sentiment_score": sentiment_score,
        # analysis_events.text_sentiment/image_sentiment are Nullable(String) for
        # aggregation — store just the label from the {label, score} object.
        "text_sentiment": _sentiment_label(result.get("text_sentiment")),
        "image_sentiment": _sentiment_label(result.get("image_sentiment")),
        "toxicity_score": toxicity_score,
        "hate_speech_score": hate_speech_score,
        "comment_count": comment_count,
        "stored_comments": stored_comments,
        "total_reactions": total_reactions,
        "coverage": coverage,
        "llm_used": 1 if llm_used else 0,
        "llm_backend": proc.get("llm_backend"),
        "topics": result.get("topics") or [],
        "keywords": result.get("keywords") or [],
        # Per-type reaction counts — the reaction-mix aggregate's only source.
        **_reaction_columns(result),
        # Alerting and label quality, dimensioned over time. Without these two
        # columns a watchlist alert only existed inside one JSON document, so
        # "are attacks on X increasing?" had no query that could answer it.
        "watchlist_alert": 1 if result.get("watchlist_alert") else 0,
        "label_agreement": float((ensemble or {}).get("mean_agreement") or 0.0),
        "created_at": created_at_dt.replace(tzinfo=None),   # CH expects naive UTC datetimes
        "scraped_at": scraped_at_dt.replace(tzinfo=None),
    }

    # Explicit column list so the table's DEFAULT (inserted_at) is honoured and
    # the row dict maps 1:1 to columns.
    ch_client.execute(
        "INSERT INTO analysis_events "
        "(post_id, campaign_id, tenant_id, platform, media_type, language, overall_sentiment, "
        "sentiment_score, text_sentiment, image_sentiment, toxicity_score, "
        "hate_speech_score, comment_count, stored_comments, total_reactions, coverage, "
        "llm_used, llm_backend, topics, keywords, "
        "like_count, love_count, haha_count, wow_count, sad_count, angry_count, care_count, "
        "watchlist_alert, label_agreement, "
        "created_at, scraped_at) VALUES",
        [row],
    )

    # Per-comment rows go in on the SAME connection, sequentially. clickhouse-driver
    # forbids concurrent queries on one Client ("Simultaneous queries on single
    # connection detected"), so this must not run as a parallel executor job.
    _clickhouse_insert_comments_sync(result, ch_client)


def _clickhouse_insert_comments_sync(result: dict, ch_client) -> int:
    """Bulk-insert one row per analysed comment into ``comment_sentiments``.

    Returns the number of rows inserted (0 when the post has no embedded
    comments). Called via executor since clickhouse-driver is synchronous.
    """
    comment_analysis = result.get("comment_analysis", {})
    comments = comment_analysis.get("comments") or []
    if not comments:
        return 0

    post_id: str = result["post_id"]
    campaign_id: str = result["campaign_id"]
    tenant_id: str = result.get("tenant_id") or "default"
    platform: str = result["platform"]

    rows = [
        {
            # `comment_sentiments` is a ReplacingMergeTree keyed
            # (post_id, comment_id), so an EMPTY comment_id is not a harmless
            # blank — every id-less comment on a post collapses into one
            # surviving row on merge, silently. Fall back to the row's position,
            # which is stable for a given result document and unique within the
            # post. Latent today (all 10,272 corpus comments carry an `id`), the
            # same way `m.facebook.com` was latent in platform_from_url.
            "comment_id": str(c.get("id") or "") or f"{post_id}#idx{i}",
            "post_id": post_id,
            "campaign_id": campaign_id,
            "tenant_id": tenant_id,
            "platform": platform,
            "sentiment": c.get("sentiment") or "neutral",
            "sentiment_score": float(c.get("sentiment_score") or 0.0),
            "emotion": c.get("emotion") or "neutral",
            "method": c.get("method") or "fast",
            "likes": int(c.get("likes") or 0),
            "author": c.get("author"),
            # How much the labellers agreed, and whether this label was copied
            # from a near-duplicate. Both are needed to answer "which of these
            # labels should a human check?" in SQL rather than by eye.
            "label_agreement": float(c.get("label_agreement") or 0.0),
            "label_source": c.get("label_source") or "",
        }
        for i, c in enumerate(comments)
    ]

    ch_client.execute(
        "INSERT INTO comment_sentiments "
        "(comment_id, post_id, campaign_id, tenant_id, platform, sentiment, sentiment_score, "
        "emotion, method, likes, author, label_agreement, label_source) VALUES",
        rows,
    )
    return len(rows)



async def persist_clickhouse(result: dict, ch_client) -> None:
    """Insert an analytics row into ClickHouse.

    The clickhouse-driver client is synchronous so this function runs the
    insert in a thread-pool executor to avoid blocking the event loop.

    Table ``analysis_events`` columns
    -----------------------------------
    post_id, campaign_id, platform, media_type, language,
    overall_sentiment, sentiment_score, text_sentiment, image_sentiment,
    toxicity_score, hate_speech_score, comment_count, stored_comments,
    total_reactions, coverage, llm_used, llm_backend, topics, keywords,
    like_count, love_count, haha_count, wow_count, sad_count, angry_count,
    care_count, created_at, scraped_at

    (There is no ``llm_used_model`` column — this list named one for a while,
    which is the kind of drift §11.3b's test now catches for table names.)

    Parameters
    ----------
    result:
        Schema-validated AnalysisResult dict.
    ch_client:
        A ``clickhouse_driver.Client`` instance.
    """
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(
        None,
        _clickhouse_insert_sync,
        result,
        ch_client,
    )
    log.debug("clickhouse: inserted", post_id=result.get("post_id"))


# ---------------------------------------------------------------------------
# MinIO / S3
# ---------------------------------------------------------------------------

def _minio_upload_sync(result: dict, s3_client, bucket: str) -> None:
    """Synchronous S3/MinIO upload (called via executor)."""
    post_id: str = result["post_id"]
    campaign_id: str = result["campaign_id"]
    key: str = f"results/{campaign_id}/{post_id}.json"

    body: bytes = json.dumps(result, ensure_ascii=False, indent=2).encode("utf-8")

    s3_client.put_object(
        Bucket=bucket,
        Key=key,
        Body=body,
        ContentType="application/json",
    )


async def persist_minio(result: dict, s3_client, bucket: str) -> None:
    """Upload the raw JSON result to MinIO at ``results/{campaign_id}/{post_id}.json``.

    boto3 is synchronous so the upload runs in a thread-pool executor.

    Parameters
    ----------
    result:
        Schema-validated AnalysisResult dict.
    s3_client:
        A boto3 S3 client created with ``endpoint_url`` pointing at MinIO.
    bucket:
        The target bucket name (e.g. ``"defense"``).
    """
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(
        None,
        _minio_upload_sync,
        result,
        s3_client,
        bucket,
    )
    log.debug(
        "minio: uploaded post_id=%s to bucket=%s",
        result.get("post_id"),
        bucket,
    )

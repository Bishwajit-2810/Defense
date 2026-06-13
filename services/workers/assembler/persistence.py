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
import json
import logging
import os
import sys
from datetime import datetime, timezone

# Repo root on path for `libs.*` (no-op when PYTHONPATH already provides it).
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from libs.embeddings import EMBEDDING_DIM, stub_embedding, to_pgvector_literal  # noqa: E402

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve_embedding(embedding: list | None, post_id: str) -> list[float]:
    """Return the semantic-search vector to persist.

    Uses the Stage-1 embedding when present and of the expected dimension;
    otherwise falls back to the shared deterministic stub (seeded from the
    post_id) so the column is always populated and round-trips still work.
    """
    if embedding and len(embedding) == EMBEDDING_DIM:
        return [float(v) for v in embedding]
    if embedding:
        log.warning(
            "stage1 embedding dim %d != EMBEDDING_DIM %d for post %s — using stub",
            len(embedding), EMBEDDING_DIM, post_id,
        )
    return stub_embedding(post_id)


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

async def persist_postgres(result: dict, engine, embedding: list | None = None) -> None:
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
    """
    from sqlalchemy import text

    post_id: str = result["post_id"]
    campaign_id: str = result["campaign_id"]
    schema_version: str = (result.get("processing") or {}).get("schema_version", "")
    result_json: str = json.dumps(result, ensure_ascii=False)
    embedding_lit: str = to_pgvector_literal(_resolve_embedding(embedding, post_id))

    upsert_sql = text(
        """
        INSERT INTO analysis_results
            (post_id, campaign_id, result, embedding, schema_version, created_at, updated_at)
        VALUES
            (:post_id, :campaign_id, CAST(:result AS jsonb), CAST(:embedding AS vector),
             :schema_version, NOW(), NOW())
        ON CONFLICT (post_id)
        DO UPDATE SET
            result         = EXCLUDED.result,
            embedding      = EXCLUDED.embedding,
            schema_version = EXCLUDED.schema_version,
            updated_at     = NOW()
        """
    )

    async with engine.begin() as conn:
        await conn.execute(
            upsert_sql,
            {
                "post_id": post_id,
                "campaign_id": campaign_id,
                "result": result_json,
                "embedding": embedding_lit,
                "schema_version": schema_version,
            },
        )

    log.debug("postgres: upserted post_id=%s (with embedding)", post_id)


# ---------------------------------------------------------------------------
# ClickHouse
# ---------------------------------------------------------------------------

def _clickhouse_insert_sync(result: dict, ch_client) -> None:
    """Synchronous ClickHouse insert (called via executor)."""
    eng = result.get("engagement", {})
    proc = result.get("processing", {})
    comment_analysis = result.get("comment_analysis", {})

    post_id: str = result["post_id"]
    campaign_id: str = result["campaign_id"]
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
    llm_model: str = proc.get("llm_model") or ""

    # Parse timestamps; fall back to epoch if unparseable
    created_at_dt = _iso_to_dt(result.get("created_at")) or datetime(1970, 1, 1, tzinfo=timezone.utc)
    scraped_at_dt = _iso_to_dt(result.get("scraped_at")) or datetime(1970, 1, 1, tzinfo=timezone.utc)

    row = {
        "post_id": post_id,
        "campaign_id": campaign_id,
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
        "created_at": created_at_dt.replace(tzinfo=None),   # CH expects naive UTC datetimes
        "scraped_at": scraped_at_dt.replace(tzinfo=None),
    }

    # Explicit column list so the table's DEFAULT (inserted_at) is honoured and
    # the row dict maps 1:1 to columns.
    ch_client.execute(
        "INSERT INTO analysis_events "
        "(post_id, campaign_id, platform, media_type, language, overall_sentiment, "
        "sentiment_score, text_sentiment, image_sentiment, toxicity_score, "
        "hate_speech_score, comment_count, stored_comments, total_reactions, coverage, "
        "llm_used, llm_backend, topics, keywords, created_at, scraped_at) VALUES",
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
    platform: str = result["platform"]

    rows = [
        {
            "comment_id": str(c.get("id") or ""),
            "post_id": post_id,
            "campaign_id": campaign_id,
            "platform": platform,
            "sentiment": c.get("sentiment") or "neutral",
            "sentiment_score": float(c.get("sentiment_score") or 0.0),
            "emotion": c.get("emotion") or "neutral",
            "method": c.get("method") or "fast",
            "likes": int(c.get("likes") or 0),
            "author": c.get("author"),
        }
        for c in comments
    ]

    ch_client.execute(
        "INSERT INTO comment_sentiments "
        "(comment_id, post_id, campaign_id, platform, sentiment, sentiment_score, "
        "emotion, method, likes, author) VALUES",
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
    overall_sentiment, sentiment_score, toxicity_score, hate_speech_score,
    comment_count, total_reactions, stored_comments, coverage,
    llm_used, llm_used_model, created_at, scraped_at

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
    log.debug("clickhouse: inserted post_id=%s", result.get("post_id"))


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

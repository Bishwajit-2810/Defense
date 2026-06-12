"""
Entry point for the Result Assembler worker.

Initialises all client connections from environment / .env, then hands
control to the consumer loop in assembler.run_assembler().

Environment variables (via libs/common/config.py / pydantic-settings)
----------------------------------------------------------------------
DATABASE_URL          – asyncpg DSN, e.g. postgresql+asyncpg://...
                        (semantic-search embeddings stored here via pgvector)
REDIS_URL             – redis://...
CLICKHOUSE_URL        – clickhouse://user:pass@host:9000/db
MINIO_ENDPOINT        – http://localhost:9002
MINIO_ACCESS_KEY      – minioadmin
MINIO_SECRET_KEY      – minioadmin
MINIO_BUCKET          – defense
LOG_LEVEL             – INFO (default)
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# Ensure the libs package is importable before anything else
# ---------------------------------------------------------------------------
_LIBS_ROOT = os.path.join(os.path.dirname(__file__), "..", "..", "..", "libs")
if _LIBS_ROOT not in sys.path:
    sys.path.insert(0, os.path.abspath(_LIBS_ROOT))

import structlog
from common.config import get_settings

# ---------------------------------------------------------------------------
# Configure structured logging early so all subsequent imports benefit
# ---------------------------------------------------------------------------
_settings_early = get_settings()
structlog.configure(
    wrapper_class=structlog.make_filtering_bound_logger(
        logging.getLevelName(_settings_early.log_level.upper())
    ),
)
log: structlog.BoundLogger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Heavy imports (after path setup)
# ---------------------------------------------------------------------------
import boto3
import redis.asyncio as aioredis
from clickhouse_driver import Client as ClickHouseClient
from sqlalchemy.ext.asyncio import create_async_engine

from assembler import run_assembler


# ---------------------------------------------------------------------------
# Client factories
# ---------------------------------------------------------------------------

def _make_postgres_engine(database_url: str):
    """Create an async SQLAlchemy engine for PostgreSQL.

    Handles both ``postgresql://`` and ``postgresql+asyncpg://`` DSNs.
    """
    url = database_url
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    elif url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+asyncpg://", 1)
    return create_async_engine(url, pool_pre_ping=True, pool_size=5, max_overflow=10)


def _make_clickhouse_client(clickhouse_url: str) -> ClickHouseClient:
    """Parse the ClickHouse DSN and return a synchronous driver Client.

    Supported format: ``clickhouse://user:password@host:port/database``
    """
    parsed = urlparse(clickhouse_url)
    host: str = parsed.hostname or "localhost"
    port: int = parsed.port or 9000
    user: str = parsed.username or "default"
    password: str = parsed.password or ""
    database: str = (parsed.path or "/default").lstrip("/") or "default"

    return ClickHouseClient(
        host=host,
        port=port,
        user=user,
        password=password,
        database=database,
    )


def _make_s3_client(endpoint: str, access_key: str, secret_key: str):
    """Return a boto3 S3 client configured for MinIO."""
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name="us-east-1",   # MinIO ignores region but boto3 requires it
    )


def _ensure_minio_bucket(s3_client, bucket: str) -> None:
    """Create the MinIO bucket if it does not already exist."""
    try:
        s3_client.head_bucket(Bucket=bucket)
    except Exception:
        try:
            s3_client.create_bucket(Bucket=bucket)
            log.info("minio: bucket created", bucket=bucket)
        except Exception as exc:
            log.warning("minio: bucket creation failed", bucket=bucket, error=str(exc))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> None:
    cfg = get_settings()

    log.info(
        "assembler_init",
        redis_url=cfg.redis_url,
        minio_endpoint=cfg.minio_endpoint,
        minio_bucket=cfg.minio_bucket,
    )

    # Build all clients
    engine = _make_postgres_engine(cfg.database_url)
    ch_client = _make_clickhouse_client(cfg.clickhouse_url)
    s3_client = _make_s3_client(
        cfg.minio_endpoint,
        cfg.minio_access_key,
        cfg.minio_secret_key,
    )
    redis_client = aioredis.from_url(cfg.redis_url, decode_responses=False)

    # Pre-flight: ensure the MinIO bucket exists
    _ensure_minio_bucket(s3_client, cfg.minio_bucket)

    # Graceful shutdown via SIGINT / SIGTERM
    shutdown_event = asyncio.Event()

    def _handle_signal(sig: int, frame) -> None:
        log.info("signal_received", sig=sig)
        shutdown_event.set()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    try:
        await run_assembler(
            engine=engine,
            ch_client=ch_client,
            s3_client=s3_client,
            bucket=cfg.minio_bucket,
            redis_client=redis_client,
            shutdown_event=shutdown_event,
        )
    finally:
        log.info("assembler_stopping")
        await redis_client.aclose()
        await engine.dispose()
        log.info("assembler_stopped")


if __name__ == "__main__":
    asyncio.run(main())

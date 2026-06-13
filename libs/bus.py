"""Pluggable message bus (architecture §1.2 / §7).

The architecture sanctions two backends — **Redis Streams (MVP)** and **Kafka
(prod)** — switchable by config. The pipeline ships on Redis Streams; this module
is the seam that lets a deployment flip to Kafka without changing producers and
consumers.

    bus = get_bus()                      # BUS_BACKEND env: "redis" (default) | "kafka"
    await bus.produce("nlp:stage1:queue", {"data": "..."})
    async for msg_id, fields in bus.consume("nlp:stage1:queue", group="g", consumer="c1"):
        ...
        await bus.ack("nlp:stage1:queue", "g", msg_id)

Both backends present the same envelope shape (a ``{field: value}`` dict, by
convention ``{"data": <json>}``) so worker code is backend-agnostic. The Redis
adapter mirrors the exact xadd/xreadgroup/xack semantics the workers already use;
the Kafka adapter lazily imports ``aiokafka`` (the ``kafka`` extra) and maps the
same calls onto a consumer group.
"""

from __future__ import annotations

import os
from typing import Any, AsyncIterator, Optional, Protocol


class Bus(Protocol):
    async def produce(self, stream: str, fields: dict) -> str: ...
    def consume(
        self, stream: str, *, group: str, consumer: str, block_ms: int = 2000, count: int = 1
    ) -> AsyncIterator[tuple[str, dict]]: ...
    async def ack(self, stream: str, group: str, msg_id: Any) -> None: ...


# ---------------------------------------------------------------------------
# Redis Streams backend (MVP default)
# ---------------------------------------------------------------------------


class RedisStreamBus:
    """Redis Streams adapter — wraps the xadd/xreadgroup/xack semantics."""

    def __init__(self, redis: Any) -> None:
        self._redis = redis

    async def ensure_group(self, stream: str, group: str) -> None:
        try:
            await self._redis.xgroup_create(stream, group, id="0", mkstream=True)
        except Exception as exc:  # BUSYGROUP — already exists
            if "BUSYGROUP" not in str(exc):
                raise

    async def produce(self, stream: str, fields: dict) -> str:
        return await self._redis.xadd(stream, fields)

    async def consume(
        self, stream: str, *, group: str, consumer: str, block_ms: int = 2000, count: int = 1
    ) -> AsyncIterator[tuple[str, dict]]:
        await self.ensure_group(stream, group)
        while True:
            messages = await self._redis.xreadgroup(
                groupname=group, consumername=consumer,
                streams={stream: ">"}, count=count, block=block_ms,
            )
            if not messages:
                continue
            for _stream, entries in messages:
                for msg_id, fields in entries:
                    yield msg_id, fields

    async def ack(self, stream: str, group: str, msg_id: Any) -> None:
        await self._redis.xack(stream, group, msg_id)


# ---------------------------------------------------------------------------
# Kafka backend (prod) — lazy aiokafka
# ---------------------------------------------------------------------------


class KafkaBus:
    """Kafka adapter (prod). Requires the ``kafka`` extra (``aiokafka``).

    Maps stream→topic, group→consumer group. ``ack`` commits offsets. Kept thin;
    a deployment sets ``BUS_BACKEND=kafka`` + ``KAFKA_BOOTSTRAP_SERVERS``.
    """

    def __init__(self, bootstrap_servers: Optional[str] = None) -> None:
        try:
            import aiokafka  # noqa: F401
        except Exception as exc:  # pragma: no cover - exercised only when selected
            raise RuntimeError(
                "BUS_BACKEND=kafka requires the 'kafka' extra: uv sync --extra kafka"
            ) from exc
        self._bootstrap = bootstrap_servers or os.getenv(
            "KAFKA_BOOTSTRAP_SERVERS", "localhost:9092"
        )
        self._producer = None
        self._consumers: dict[str, Any] = {}

    async def _get_producer(self):
        if self._producer is None:
            from aiokafka import AIOKafkaProducer

            self._producer = AIOKafkaProducer(bootstrap_servers=self._bootstrap)
            await self._producer.start()
        return self._producer

    async def produce(self, stream: str, fields: dict) -> str:
        import json

        producer = await self._get_producer()
        payload = json.dumps(fields, default=str).encode("utf-8")
        md = await producer.send_and_wait(stream, payload)
        return f"{md.partition}-{md.offset}"

    async def consume(
        self, stream: str, *, group: str, consumer: str, block_ms: int = 2000, count: int = 1
    ) -> AsyncIterator[tuple[str, dict]]:
        import json

        from aiokafka import AIOKafkaConsumer

        kc = AIOKafkaConsumer(
            stream, bootstrap_servers=self._bootstrap, group_id=group,
            enable_auto_commit=False, auto_offset_reset="earliest",
        )
        await kc.start()
        self._consumers[stream] = kc
        try:
            async for rec in kc:
                try:
                    fields = json.loads(rec.value.decode("utf-8"))
                except Exception:
                    fields = {"data": rec.value.decode("utf-8", "replace")}
                yield f"{rec.partition}-{rec.offset}", fields
        finally:
            await kc.stop()

    async def ack(self, stream: str, group: str, msg_id: Any) -> None:
        kc = self._consumers.get(stream)
        if kc is not None:
            await kc.commit()


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def get_bus(redis: Any = None, backend: Optional[str] = None) -> Bus:
    """Return the configured bus. ``redis`` is required for the redis backend."""
    backend = (backend or os.getenv("BUS_BACKEND", "redis")).lower()
    if backend == "redis":
        if redis is None:
            raise ValueError("redis backend requires a redis client")
        return RedisStreamBus(redis)
    if backend == "kafka":
        return KafkaBus()
    raise ValueError(f"Unknown BUS_BACKEND {backend!r}; use 'redis' or 'kafka'")

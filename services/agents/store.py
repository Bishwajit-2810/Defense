"""
Agent run store — persists and retrieves AgentRun objects.

Uses Redis when available (24-hour TTL on each key), falling back to an
in-process dict when Redis is not configured.  Serialisation is done with
dataclasses.asdict so the on-wire format is a plain JSON object.
"""

from __future__ import annotations

import dataclasses
import json
import time
from typing import Optional

import structlog

from .runner import AgentRun

log = structlog.get_logger("agent-store")

# Redis key prefix and TTL
_KEY_PREFIX = "agent_run:"
_TTL_SECONDS = 86_400           # 24 hours
_LIST_KEY = "agent_run_index"   # sorted-set key for recent-run ordering


def _run_to_dict(run: AgentRun) -> dict:
    """Serialise an AgentRun to a JSON-compatible dict."""
    return dataclasses.asdict(run)


def _dict_to_run(data: dict) -> AgentRun:
    """Deserialise a dict back into an AgentRun."""
    return AgentRun(**data)


class AgentRunStore:
    """Stores and retrieves agent runs.

    Uses Redis if a client is supplied, otherwise falls back to an in-memory
    dict.  The in-memory store is appropriate for single-process dev/test; the
    Redis path is required for multi-replica production deployments.
    """

    def __init__(self, redis=None) -> None:
        self._redis = redis
        self._store: dict[str, dict] = {}   # in-memory fallback
        self._index: list[tuple[float, str]] = []  # [(created_at, run_id)]

    # ------------------------------------------------------------------
    # Save
    # ------------------------------------------------------------------

    async def save(self, run: AgentRun) -> None:
        """Persist an AgentRun (create or overwrite)."""
        data = _run_to_dict(run)
        key = f"{_KEY_PREFIX}{run.run_id}"

        if self._redis is not None:
            try:
                await self._redis.setex(
                    key,
                    _TTL_SECONDS,
                    json.dumps(data, default=str),
                )
                # Maintain a sorted set of run IDs ordered by creation time
                # so list_recent() can page without scanning all keys.
                await self._redis.zadd(
                    _LIST_KEY,
                    {run.run_id: run.created_at},
                )
                # Apply TTL to the index as well (best-effort — it's only used
                # for listing, not for correctness).
                await self._redis.expire(_LIST_KEY, _TTL_SECONDS * 2)
                log.debug("run_saved_redis", run_id=run.run_id, status=run.status)
                return
            except Exception as exc:
                log.warning("redis_save_failed", run_id=run.run_id, error=str(exc))
                # Fall through to in-memory store

        # In-memory path
        self._store[run.run_id] = data
        # Keep the index entries deduplicated
        self._index = [
            (ts, rid) for ts, rid in self._index if rid != run.run_id
        ]
        self._index.append((run.created_at, run.run_id))
        self._index.sort(key=lambda x: x[0])
        log.debug("run_saved_memory", run_id=run.run_id, status=run.status)

    # ------------------------------------------------------------------
    # Get
    # ------------------------------------------------------------------

    async def get(self, run_id: str) -> Optional[AgentRun]:
        """Retrieve a single AgentRun by ID, or None if not found / expired."""
        key = f"{_KEY_PREFIX}{run_id}"

        if self._redis is not None:
            try:
                raw = await self._redis.get(key)
                if raw is None:
                    return None
                data = json.loads(raw)
                return _dict_to_run(data)
            except Exception as exc:
                log.warning("redis_get_failed", run_id=run_id, error=str(exc))
                # Fall through to in-memory store

        data = self._store.get(run_id)
        if data is None:
            return None
        return _dict_to_run(data)

    # ------------------------------------------------------------------
    # List recent
    # ------------------------------------------------------------------

    async def list_recent(self, limit: int = 20) -> list[AgentRun]:
        """Return the most recent `limit` runs, newest first."""
        if self._redis is not None:
            try:
                # zrevrangebyscore with limit — newest timestamps first
                run_ids: list[str] = await self._redis.zrevrange(
                    _LIST_KEY, 0, limit - 1
                )
                runs: list[AgentRun] = []
                for rid in run_ids:
                    run = await self.get(rid)
                    if run is not None:
                        runs.append(run)
                return runs
            except Exception as exc:
                log.warning("redis_list_failed", error=str(exc))
                # Fall through to in-memory store

        # In-memory path — _index is sorted by created_at ascending
        recent_ids = [rid for _, rid in reversed(self._index)][:limit]
        runs = []
        for rid in recent_ids:
            data = self._store.get(rid)
            if data is not None:
                runs.append(_dict_to_run(data))
        return runs

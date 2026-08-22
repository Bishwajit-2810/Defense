"""Unit tests for the backend system monitor (routers/system.py)."""

import asyncio
import sys
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from defense.services.api.routers import system as S


def test_format_bytes():
    assert S._format_bytes(0) == "0 B"
    assert S._format_bytes(1024) == "1.0 KB"
    assert S._format_bytes(1024 * 1024 * 5) == "5.0 MB"
    assert S._format_bytes(1024 * 1024 * 1024 * 16) == "16.0 GB"


def test_format_duration():
    assert S._format_duration(0) == "0s"
    assert S._format_duration(45) == "45s"
    assert S._format_duration(125) == "2m 5s"
    assert S._format_duration(3665) == "1h 1m 5s"
    assert S._format_duration(90000) == "1d 1h 0m 0s"


def test_get_cpu_stats():
    cpu = S._get_cpu_stats()
    assert "percent" in cpu
    assert "per_cpu_percent" in cpu
    assert isinstance(cpu["per_cpu_percent"], list)
    assert "cores_logical" in cpu
    assert "load_avg" in cpu
    assert "normalized_load_percent" in cpu


def test_get_igpu_stats():
    igpus = S._get_igpu_stats()
    assert isinstance(igpus, list)
    if len(igpus) > 0:
        assert "name" in igpus[0]
        assert "card" in igpus[0]
        assert "driver" in igpus[0]
        assert "frequency" in igpus[0]


def test_get_gpu_stats():
    gpu = S._get_gpu_stats()
    assert "available" in gpu
    assert "devices" in gpu
    assert "discrete" in gpu
    assert "integrated" in gpu
    assert isinstance(gpu["devices"], list)
    assert isinstance(gpu["discrete"], list)
    assert isinstance(gpu["integrated"], list)


def test_get_memory_stats():
    mem = S._get_memory_stats()
    assert "total_human" in mem
    assert "used_human" in mem
    assert "percent" in mem
    assert "swap_percent" in mem


def test_get_storage_stats():
    storage = S._get_storage_stats()
    assert "primary" in storage
    assert "mounts" in storage
    assert "io" in storage


def test_get_docker_stats_sync():
    docker = S._get_docker_stats_sync()
    assert "available" in docker
    if docker["available"]:
        assert "total_containers" in docker
        assert "running_containers" in docker
        assert "containers" in docker


def test_get_agents_stats():
    agents = S._get_agents_stats()
    assert "total_agents" in agents
    assert agents["total_agents"] > 0
    assert "agents" in agents
    assert any(a["name"] == "analyst" for a in agents["agents"])


@pytest.mark.asyncio
async def test_get_ollama_stats():
    ollama = await S._get_ollama_stats()
    assert "status" in ollama
    assert "endpoint" in ollama
    assert "models_count" in ollama
    assert "models" in ollama


@pytest.mark.asyncio
async def test_get_minio_stats():
    minio = await S._get_minio_stats()
    assert "status" in minio
    assert "endpoint" in minio
    assert "bucket" in minio


def test_get_host_stats_structure():
    stats = S._get_host_stats()
    assert "hostname" in stats
    assert "os" in stats
    assert "cpu" in stats
    assert "memory" in stats
    assert "storage" in stats
    assert "gpu" in stats
    assert "network" in stats
    assert "process" in stats
    assert "pid" in stats["process"]


@pytest.mark.asyncio
async def test_postgres_stats_connected():
    fake_db = AsyncMock()
    
    class FakeResult:
        def __init__(self, val, rows=None):
            self._val = val
            self._rows = rows or []
        def scalar(self):
            return self._val
        def fetchall(self):
            return self._rows

    async def fake_execute(query, *args, **kwargs):
        q_str = str(query)
        if "pg_stat_activity" in q_str:
            return FakeResult(3)
        if "pg_size_pretty" in q_str:
            return FakeResult("42 MB")
        if "version()" in q_str:
            return FakeResult("PostgreSQL 16.2 on x86_64")
        if "current_database()" in q_str:
            return FakeResult("defense_test")
        if "pg_stat_user_tables" in q_str:
            return FakeResult(None, rows=[("posts", 100), ("comments", 500)])
        return FakeResult(None)

    fake_db.execute = AsyncMock(side_effect=fake_execute)
    res = await S._get_postgres_stats(fake_db)
    assert res["status"] == "connected"
    assert res["database"] == "defense_test"
    assert res["version"] == "PostgreSQL 16.2 on x86_64"
    assert res["db_size"] == "42 MB"
    assert res["active_connections"] == 3
    assert res["tables"]["posts"] == 100


@pytest.mark.asyncio
async def test_postgres_stats_unreachable_fails_safely():
    fake_db = AsyncMock()
    fake_db.execute = AsyncMock(side_effect=Exception("Connection refused"))
    res = await S._get_postgres_stats(fake_db)
    assert res["status"] == "unreachable"
    assert "Connection refused" in res["error"]


@pytest.mark.asyncio
async def test_redis_stats_connected():
    fake_redis = AsyncMock()
    fake_redis.ping = AsyncMock(return_value=True)
    fake_redis.info = AsyncMock(return_value={
        "redis_version": "7.2.4",
        "redis_mode": "standalone",
        "uptime_in_seconds": 3600,
        "connected_clients": 5,
        "used_memory_human": "12.5M",
        "used_memory_peak_human": "20.0M",
        "mem_fragmentation_ratio": 1.2,
        "total_commands_processed": 123456,
        "instantaneous_ops_per_sec": 42,
    })
    fake_redis.dbsize = AsyncMock(return_value=250)
    fake_redis.xlen = AsyncMock(return_value=0)
    fake_redis.xinfo_groups = AsyncMock(return_value=[])

    res = await S._get_redis_stats(fake_redis)
    assert res["status"] == "connected"
    assert res["version"] == "7.2.4"
    assert res["uptime_seconds"] == 3600
    assert res["connected_clients"] == 5
    assert res["total_keys"] == 250
    assert len(res["streams"]) > 0


@pytest.mark.asyncio
async def test_collect_full_system_snapshot():
    fake_db = AsyncMock()
    fake_db.execute = AsyncMock(side_effect=Exception("DB down"))
    fake_redis = AsyncMock()
    fake_redis.ping = AsyncMock(side_effect=Exception("Redis down"))

    res = await S.collect_full_system_snapshot(None, fake_db, fake_redis, tenant_id="default")
    assert "timestamp" in res
    assert res["status"] in ("healthy", "degraded", "unhealthy")
    assert "host" in res
    assert "gpu" in res
    assert "docker" in res
    assert "ollama" in res
    assert "minio" in res
    assert "agents" in res
    assert "datastores" in res
    assert "postgres" in res["datastores"]
    assert "redis" in res["datastores"]
    assert "pipeline" in res
    assert "ai_runtime" in res
    assert "api_service" in res

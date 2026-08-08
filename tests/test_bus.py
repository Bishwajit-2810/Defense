"""Unit tests for libs/bus.py — pluggable Redis/Kafka bus (§7)."""

import asyncio
import sys

sys.path.insert(0, '/home/bk/code/defense/src/defense')

import pytest

from libs.bus import KafkaBus, RedisStreamBus, get_bus


class FakeRedis:
    def __init__(self):
        self.streams: dict[str, list] = {}
        self.groups: set = set()
        self.acked: list = []
        self._seq = 0

    async def xgroup_create(self, stream, group, id="0", mkstream=True):
        if (stream, group) in self.groups:
            raise Exception("BUSYGROUP already exists")
        self.groups.add((stream, group))

    async def xadd(self, stream, fields):
        self._seq += 1
        eid = f"{self._seq}-0"
        self.streams.setdefault(stream, []).append((eid, dict(fields)))
        return eid

    async def xack(self, stream, group, msg_id):
        self.acked.append((stream, group, msg_id))


def test_factory_defaults_to_redis():
    bus = get_bus(redis=FakeRedis())
    assert isinstance(bus, RedisStreamBus)


def test_factory_redis_requires_client():
    with pytest.raises(ValueError):
        get_bus(backend="redis")


def test_factory_unknown_backend():
    with pytest.raises(ValueError):
        get_bus(redis=FakeRedis(), backend="rabbitmq")


def test_redis_produce_and_ack():
    r = FakeRedis()
    bus = RedisStreamBus(r)
    eid = asyncio.run(bus.produce("s1", {"data": "x"}))
    assert r.streams["s1"][0][1] == {"data": "x"}
    asyncio.run(bus.ack("s1", "g", eid))
    assert r.acked == [("s1", "g", eid)]


def test_ensure_group_is_idempotent():
    r = FakeRedis()
    bus = RedisStreamBus(r)
    asyncio.run(bus.ensure_group("s1", "g"))
    # second call hits BUSYGROUP and must be swallowed, not raised
    asyncio.run(bus.ensure_group("s1", "g"))
    assert ("s1", "g") in r.groups


def test_kafka_backend_requires_extra():
    # aiokafka isn't installed in the base env → clear, actionable error.
    with pytest.raises(RuntimeError, match="kafka"):
        get_bus(backend="kafka")

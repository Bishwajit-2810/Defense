"""The log sink must not blur test output into the operator's log view.

`libs/common/logging.RedisLogHandler` mirrors every record into `logs:recent` /
`logs:live` — the keys `GET /v1/logs` serves to the dashboard's Logs tab. Two
things went wrong with that:

* **`log_to_redis` defaults True and nothing turned it off for tests**, so a
  plain `pytest` published its own output there. The agent suites deliberately
  feed the runner fabricated payloads ("post_id='1234567890abcdef'", a table of
  "#12345 Economic Growth"), and those appeared in the Logs tab as real agent
  runs at real timestamps. 39 such entries were sitting in the live buffer.
* **Every entry was attributed to service "-"**, because the handler reads
  `record.service` while `setup_logging` binds the name through structlog's
  contextvars, which never touch the stdlib record. The Logs tab's service
  filter therefore had exactly one option.
"""

import json
import logging
import os
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src" / "defense"))

from defense.libs.common import logging as dlog  # noqa: E402
from defense.libs.common.config import get_settings  # noqa: E402


def test_the_test_run_cannot_write_to_the_live_log_buffer():
    """conftest sets LOG_TO_REDIS=0 before anything imports Settings."""
    assert os.environ.get("LOG_TO_REDIS") == "0", (
        "conftest must disable the Redis log sink for the whole session"
    )
    assert get_settings().log_to_redis is False
    assert dlog._redis_enabled() is False


def test_the_handler_short_circuits_when_the_sink_is_off(monkeypatch):
    """Not just configuration — the emit path must actually not reach Redis."""
    called = {"n": 0}
    monkeypatch.setattr(dlog, "_sink_client", lambda: called.__setitem__("n", called["n"] + 1))
    monkeypatch.setattr(dlog, "_redis_enabled", lambda: False)

    dlog.RedisLogHandler().emit(
        logging.LogRecord("t", logging.INFO, __file__, 1, "hello", None, None)
    )

    assert called["n"] == 0


class _FakePipe:
    def __init__(self, out):
        self.out = out

    def lpush(self, key, payload):
        self.out.append((key, payload))
        return self

    def ltrim(self, *a):
        return self

    def expire(self, *a):
        return self

    def publish(self, channel, payload):
        self.out.append((channel, payload))
        return self

    def execute(self):
        return True


class _FakeRedis:
    def __init__(self):
        self.written: list[tuple[str, str]] = []

    def pipeline(self):
        return _FakePipe(self.written)


def _emit_one(monkeypatch, *, service_name, record_attr=None):
    fake = _FakeRedis()
    monkeypatch.setattr(dlog, "_redis_enabled", lambda: True)
    monkeypatch.setattr(dlog, "_sink_client", lambda: fake)
    monkeypatch.setattr(dlog, "_redis_sink_fails", 0, raising=False)
    monkeypatch.setattr(dlog, "_service_name", service_name, raising=False)

    handler = dlog.RedisLogHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    record = logging.LogRecord("t", logging.INFO, __file__, 1, "a line", None, None)
    if record_attr is not None:
        record.service = record_attr
    handler.emit(record)

    payloads = [json.loads(p) for _k, p in fake.written]
    assert payloads, "nothing was written"
    return payloads[0]


def test_entries_carry_the_service_that_configured_the_process(monkeypatch):
    entry = _emit_one(monkeypatch, service_name="stage2-llm")
    assert entry["service"] == "stage2-llm", (
        "the Logs tab cannot filter by service if every line says '-'"
    )


def test_an_explicit_record_attribute_still_wins(monkeypatch):
    entry = _emit_one(monkeypatch, service_name="api", record_attr="router")
    assert entry["service"] == "router"


def test_service_falls_back_to_a_dash_rather_than_none(monkeypatch):
    entry = _emit_one(monkeypatch, service_name="")
    assert entry["service"] == "-"


def test_setup_logging_records_the_name_even_when_already_configured(monkeypatch):
    """The early `if _CONFIGURED: return` must not leave the sink at '-'."""
    monkeypatch.setattr(dlog, "_CONFIGURED", True, raising=False)
    monkeypatch.setattr(dlog, "_service_name", "-", raising=False)

    dlog.setup_logging("assembler")

    assert dlog._service_name == "assembler"


def test_the_sink_keys_are_the_ones_the_api_serves():
    """A rename here silently empties the Logs tab, so pin the contract."""
    from defense.services.api.routers import logs as logs_router

    assert logs_router.LOG_LIST_KEY == dlog.LOG_LIST_KEY == "logs:recent"
    assert logs_router.LOG_CHANNEL == dlog.LOG_CHANNEL == "logs:live"

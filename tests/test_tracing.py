"""Unit tests for libs/tracing.py — opt-in OTel setup is a safe no-op (§10)."""

import importlib
import sys

sys.path.insert(0, '/home/bk/code/defense/src/defense')


def test_disabled_by_default(monkeypatch):
    monkeypatch.delenv("OTEL_ENABLED", raising=False)
    import libs.tracing as t
    importlib.reload(t)
    assert t.tracing_enabled() is False
    # setup + instrument must never raise and return False when disabled.
    assert t.setup_tracing("svc") is False

    class _App:  # stand-in FastAPI app
        pass

    assert t.instrument_app(_App(), "svc") is False


def test_enabled_but_sdk_missing_is_graceful(monkeypatch):
    """OTEL_ENABLED=true without the obs extra installed → no-op, no exception."""
    monkeypatch.setenv("OTEL_ENABLED", "true")
    import libs.tracing as t
    importlib.reload(t)
    assert t.tracing_enabled() is True
    # SDK isn't installed in the base env → returns False, doesn't raise.
    assert t.setup_tracing("svc") is False

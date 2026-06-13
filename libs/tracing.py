"""OpenTelemetry tracing setup (architecture §10) — opt-in, dependency-guarded.

Enabled only when ``OTEL_ENABLED=true`` AND the OTel SDK/exporter/instrumentation
packages are installed (``uv sync --extra obs``). Otherwise this is a safe no-op,
so dev/CI without the packages keeps running unchanged.

Spans export via OTLP to a collector / Jaeger (``OTEL_EXPORTER_OTLP_ENDPOINT``,
default the compose Jaeger at http://jaeger:4317). FastAPI apps get auto request
spans; ``instrument_app(app)`` is the hook the API calls at startup.
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)

_initialized = False


def tracing_enabled() -> bool:
    return os.getenv("OTEL_ENABLED", "false").lower() == "true"


def setup_tracing(service_name: str) -> bool:
    """Initialize a global tracer provider with an OTLP exporter.

    Returns True if tracing was actually initialized, False otherwise (disabled,
    already initialized, or SDK not installed). Never raises.
    """
    global _initialized
    if _initialized or not tracing_enabled():
        return False
    try:
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
            OTLPSpanExporter,
        )
    except Exception as exc:  # packages not installed
        log.warning("otel SDK unavailable (%s) — tracing disabled; run uv sync --extra obs", exc)
        return False

    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://jaeger:4317")
    provider = TracerProvider(
        resource=Resource.create({"service.name": service_name})
    )
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
    trace.set_tracer_provider(provider)
    _initialized = True
    log.info("otel tracing initialized service=%s endpoint=%s", service_name, endpoint)
    return True


def instrument_app(app, service_name: str) -> bool:
    """Set up tracing and auto-instrument a FastAPI app. No-op when disabled."""
    if not setup_tracing(service_name):
        return False
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(app)
        return True
    except Exception as exc:
        log.warning("fastapi instrumentation unavailable (%s)", exc)
        return False

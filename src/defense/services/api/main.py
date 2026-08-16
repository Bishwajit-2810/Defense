"""Defense Analysis API — FastAPI application entry point."""

from __future__ import annotations

import logging
import os
import time
import uuid

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from prometheus_fastapi_instrumentator import Instrumentator
from defense.libs.common.config import get_settings

sys_config = get_settings()

from defense.services.api.routers import analysis, auth, chat, chat_history, config, health, ingest, pipeline, reports, search
from defense.services.api.routers.agents import router as agents_router
from defense.services.api.routers.logs import router as logs_router
from defense.services.api.routers.usage import router as usage_router
from defense.services.api.routers.events import router as events_router

# ---------------------------------------------------------------------------
# Logging — everything funnels into loguru (see libs/common/logging.py)
# ---------------------------------------------------------------------------
from defense.libs.common.logging import setup_logging

setup_logging("api")
log = structlog.get_logger(__name__)

# Quiet uvicorn's access log: the `log_requests` middleware below records the same
# request with more detail (duration, request id, structured fields) and honours
# the exempt-path list. Leaving both on doubled every line in the terminal and in
# the dashboard's log drawer — and, because uvicorn's access log has no exempt
# list, reading /v1/logs generated access lines about reading /v1/logs.
# Errors from uvicorn still come through at WARNING+.
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Defense Analysis API",
    version="1.0.0",
    description=(
        "API gateway for the Defense social-media analysis platform. "
        "Handles ingest, analysis orchestration, report generation, and search."
    ),
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
)

# ---------------------------------------------------------------------------
# CORS — dev mode: allow all origins
# ---------------------------------------------------------------------------

sys_config = get_settings()
cors_origins_str = sys_config.cors_origins
cors_origins = [o.strip() for o in cors_origins_str.split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Prometheus metrics
# ---------------------------------------------------------------------------

Instrumentator().instrument(app).expose(app, endpoint="/metrics")

# ---------------------------------------------------------------------------
# Request / response logging
# ---------------------------------------------------------------------------

# Paths deliberately not logged. The log endpoints are excluded because the
# dashboard polls/tails them continuously and every such request would show up
# in the very view the user is reading — drowning the pipeline lines that matter.
# /metrics is excluded for the same reason (Prometheus scrapes on a timer).
_LOG_EXEMPT_PREFIXES = ("/v1/logs", "/metrics", "/docs", "/redoc", "/openapi.json", "/favicon")

# Health checks are logged at DEBUG rather than INFO: useful when chasing a
# readiness problem, noise otherwise.
_LOG_QUIET_PREFIXES = ("/health", "/v1/health")


@app.middleware("http")
async def log_requests(request, call_next):
    """Log one line per request: method, path, status, duration.

    Also stamps ``X-Request-ID`` on the response so a dashboard entry can be
    correlated with the server-side line for the same call.
    """
    path = request.url.path

    if path.startswith(_LOG_EXEMPT_PREFIXES):
        return await call_next(request)

    request_id = uuid.uuid4().hex[:12]
    started = time.perf_counter()

    try:
        response = await call_next(request)
    except Exception as exc:
        # Unhandled error: record it here, then let the framework's handler run.
        log.error(
            "http_request_failed",
            request_id=request_id,
            method=request.method,
            path=path,
            error=str(exc),
            duration_ms=round((time.perf_counter() - started) * 1000, 1),
        )
        raise

    duration_ms = round((time.perf_counter() - started) * 1000, 1)
    response.headers["X-Request-ID"] = request_id

    fields = {
        "request_id": request_id,
        "method": request.method,
        "path": path,
        "status": response.status_code,
        "duration_ms": duration_ms,
    }
    if request.url.query:
        fields["query"] = request.url.query[:200]

    if response.status_code >= 500:
        log.error("http_request", **fields)
    elif response.status_code >= 400:
        log.warning("http_request", **fields)
    elif path.startswith(_LOG_QUIET_PREFIXES):
        log.debug("http_request", **fields)
    else:
        log.info("http_request", **fields)

    return response

# ---------------------------------------------------------------------------
# OpenTelemetry tracing (§10) — opt-in (OTEL_ENABLED=true + `uv sync --extra obs`);
# a no-op otherwise, so dev/CI is unaffected.
# ---------------------------------------------------------------------------

try:
    from defense.libs.tracing import instrument_app

    if instrument_app(app, service_name="defense-api"):
        log.info("otel_tracing_enabled")
except Exception as exc:  # never let tracing setup break startup
    log.warning("otel_tracing_setup_skipped", error=str(exc))

# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------

app.include_router(health.router)
app.include_router(auth.router)
app.include_router(ingest.router)
app.include_router(analysis.router)
app.include_router(reports.router)
app.include_router(search.router)
app.include_router(config.router)
app.include_router(chat.router)
app.include_router(chat_history.router)
app.include_router(pipeline.router)
app.include_router(agents_router)
app.include_router(usage_router)
app.include_router(logs_router)
app.include_router(events_router)

# ---------------------------------------------------------------------------
# Lifecycle events
# ---------------------------------------------------------------------------


@app.on_event("startup")
async def on_startup() -> None:
    log.info("API started", version=app.version, title=app.title)

"""Defense Analysis API — FastAPI application entry point."""

from __future__ import annotations

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from prometheus_fastapi_instrumentator import Instrumentator

from routers import analysis, auth, chat, config, health, ingest, pipeline, reports, search
from routers.agents import router as agents_router
from routers.usage import router as usage_router

# ---------------------------------------------------------------------------
# Logging — everything funnels into loguru (see libs/common/logging.py)
# ---------------------------------------------------------------------------
from libs.common.logging import setup_logging

setup_logging("api")
log = structlog.get_logger(__name__)

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

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Prometheus metrics
# ---------------------------------------------------------------------------

Instrumentator().instrument(app).expose(app, endpoint="/metrics")

# ---------------------------------------------------------------------------
# OpenTelemetry tracing (§10) — opt-in (OTEL_ENABLED=true + `uv sync --extra obs`);
# a no-op otherwise, so dev/CI is unaffected.
# ---------------------------------------------------------------------------

try:
    from libs.tracing import instrument_app

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
app.include_router(pipeline.router)
app.include_router(agents_router)
app.include_router(usage_router)

# ---------------------------------------------------------------------------
# Lifecycle events
# ---------------------------------------------------------------------------


@app.on_event("startup")
async def on_startup() -> None:
    log.info("API started", version=app.version, title=app.title)

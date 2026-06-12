"""Centralised logging — everything funnels into loguru.

One call, :func:`setup_logging`, makes every log line in a process flow through a
single loguru sink with a consistent, readable format. It captures three sources:

  1. **loguru** itself (``from loguru import logger``) — used directly by new code
     such as the LLM client.
  2. The **stdlib ``logging``** module (``logging.getLogger(...)``) — used by the
     assembler builder/persistence, text/comment analysers, the API routers, etc.
     An :class:`InterceptHandler` on the root logger forwards those records.
  3. **structlog** (``structlog.get_logger(...)``) — used by the pipeline workers.
     A final structlog processor hands each event to loguru, so the workers' rich
     ``log.info("event", key=value)`` calls keep their structured fields with no
     call-site changes.

Idempotent: safe to call from several module imports in the same process.
"""

from __future__ import annotations

import logging
import os
import sys

from loguru import logger

_CONFIGURED = False

# Compact, coloured, aligned format. ``extra`` carries structured fields (service
# name + any bound key/values from structlog or loguru.bind()).
_FORMAT = (
    "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> "
    "<level>{level: <8}</level> "
    "<cyan>{extra[service]: <11}</cyan> "
    "<level>{message}</level> "
    "<dim>{extra[fields]}</dim>"
)

_RESERVED = {"service", "fields"}


def _patch_fields(record: dict) -> None:
    """Render any bound structured fields into a single ``{extra[fields]}`` token."""
    extra = record["extra"]
    extra.setdefault("service", "-")
    kv = {k: v for k, v in extra.items() if k not in _RESERVED}
    extra["fields"] = " ".join(f"{k}={v}" for k, v in kv.items()) if kv else ""


class InterceptHandler(logging.Handler):
    """Route stdlib ``logging`` records into loguru, preserving level + origin."""

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D102
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno
        # Walk back to the caller so file:line points at the real source, not logging.
        frame, depth = logging.currentframe(), 2
        while frame and frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back
            depth += 1
        logger.opt(depth=depth, exception=record.exc_info).bind(
            service=record.name.split(".")[0] or "-"
        ).log(level, record.getMessage())


def _structlog_to_loguru(_logger, method_name: str, event_dict: dict):
    """Final structlog processor: emit the event via loguru, then drop it.

    structlog hands us the fully-processed ``event_dict``; we forward the message
    and bound fields to loguru and raise ``DropEvent`` so structlog's own factory
    writes nothing (loguru is the single sink).
    """
    import structlog  # local import — structlog is optional at call time

    level = (event_dict.pop("level", None) or method_name or "info").lower()
    if level in ("warn",):
        level = "warning"
    message = event_dict.pop("event", "")
    event_dict.pop("timestamp", None)  # loguru stamps its own time
    exc_info = event_dict.pop("exc_info", None)
    service = event_dict.pop("service", None)
    bound = {k: v for k, v in event_dict.items() if k != "logger"}
    lg = logger.bind(service=service or event_dict.get("logger", "worker").split(".")[0], **bound)
    lg.opt(depth=1, exception=exc_info).log(level.upper(), message)
    raise structlog.DropEvent


def setup_logging(service: str = "app", level: str | None = None) -> "logger":
    """Configure loguru as the one sink for loguru + stdlib + structlog.

    Parameters
    ----------
    service:
        Short tag shown on every line from this process (e.g. ``"stage2"``).
    level:
        Minimum level; defaults to the ``LOG_LEVEL`` env var, else ``INFO``.

    Returns the loguru ``logger`` (already bound with ``service``), so callers can
    do ``log = setup_logging("stage2")`` and use it directly.
    """
    global _CONFIGURED
    lvl = (level or os.environ.get("LOG_LEVEL", "INFO")).upper()

    if not _CONFIGURED:
        logger.remove()
        logger.configure(patcher=_patch_fields)
        logger.add(sys.stderr, level=lvl, format=_FORMAT, enqueue=True, backtrace=False, diagnose=False)

        # stdlib logging → loguru (replace any handlers, capture everything)
        logging.basicConfig(handlers=[InterceptHandler()], level=0, force=True)
        for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "sqlalchemy.engine"):
            lg = logging.getLogger(name)
            lg.handlers = [InterceptHandler()]
            lg.propagate = False

        # structlog → loguru (workers keep their structlog API unchanged)
        try:
            import structlog

            structlog.configure(
                processors=[
                    structlog.contextvars.merge_contextvars,
                    structlog.processors.add_log_level,
                    _structlog_to_loguru,
                ],
                logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
                cache_logger_on_first_use=True,
            )
        except Exception:  # structlog not installed in this process — fine
            pass

        _CONFIGURED = True

    # Tag this process's service name on every line (structlog reads it from
    # contextvars via merge_contextvars; loguru reads it from the bound logger).
    try:
        import structlog

        structlog.contextvars.bind_contextvars(service=service)
    except Exception:
        pass

    return logger.bind(service=service)

"""Entry point: python -m router"""

import asyncio
import logging

import structlog

structlog.configure(
    processors=[
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.BoundLogger,
    context_class=dict,
    logger_factory=structlog.PrintLoggerFactory(),
)

from defense.libs.common.config import get_settings

log_level = get_settings().log_level.upper()
logging.basicConfig(level=getattr(logging, log_level, logging.INFO))

from .router import run

if __name__ == "__main__":
    asyncio.run(run())

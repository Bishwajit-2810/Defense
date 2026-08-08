import sys
import structlog
import logging

logging.basicConfig(
    format="%(message)s",
    stream=sys.stdout,
    level=logging.INFO,
)

structlog.configure(
    processors=[
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.dev.ConsoleRenderer()
    ],
    logger_factory=structlog.stdlib.LoggerFactory(),
    wrapper_class=structlog.stdlib.BoundLogger,
)

log = structlog.get_logger("test")

def do_log():
    try:
        raise ValueError("my original error")
    except Exception as exc:
        log.error("stage2_summary_error", error=str(exc), ms=1.0)
        print("Log succeeded!")

do_log()

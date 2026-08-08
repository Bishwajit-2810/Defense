import sys
import structlog
import logging
import httpx

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
        raise httpx.ConnectError("failed to connect")
    except Exception as exc:
        log.warning("llm_groq_failed_falling_back_to_local role=%s model=%s error=%s", "summary", "llama", exc)
        print("Log succeeded!")

try:
    do_log()
except Exception as e:
    import traceback
    traceback.print_exc()

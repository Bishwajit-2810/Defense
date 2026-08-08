import sys
import logging
import structlog

logging.basicConfig(
    format="%(message)s",
    stream=sys.stdout,
    level=logging.INFO,
)

structlog.configure(
    processors=[
        structlog.stdlib.filter_by_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.dev.ConsoleRenderer()
    ],
    context_class=dict,
    logger_factory=structlog.stdlib.LoggerFactory(),
    wrapper_class=structlog.stdlib.BoundLogger,
    cache_logger_on_first_use=True,
)

log = structlog.get_logger("test")

def do_log():
    try:
        raise ValueError("my original error")
    except Exception as exc:
        log.warning("llm_groq_failed_falling_back_to_local role=%s model=%s error=%s", "summary", "llama", exc)
        print("Log succeeded!")

try:
    do_log()
except Exception as e:
    import traceback
    traceback.print_exc()

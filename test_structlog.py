import structlog
import logging
import sys

logging.basicConfig(stream=sys.stdout, level=logging.INFO)

structlog.configure(
    processors=[
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
    ],
    logger_factory=structlog.stdlib.LoggerFactory(),
    wrapper_class=structlog.stdlib.BoundLogger,
)

log = structlog.get_logger("test")

# Test 1
try:
    log.info(
        "llm_call backend=%s role=%s model=%s tokens=%s latency_ms=%s finish_reason=%s truncated=%s continuations=%s",
        "local", "summary", "llama", 100, 150.0, "stop", False, 0
    )
    print("Test 1 OK")
except Exception as e:
    print("Test 1 Failed:", e)

# Test 2: attempt=%s/%s
try:
    log.warning(
        "llm_truncated_continuing backend=%s role=%s model=%s attempt=%s/%s",
        "local", "summary", "llama", 1, 3
    )
    print("Test 2 OK")
except Exception as e:
    print("Test 2 Failed:", e)

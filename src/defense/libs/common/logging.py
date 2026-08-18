"""Centralised logging — everything funnels into structlog.

One call, :func:`setup_logging`, configures standard library logging and structlog.
"""
from __future__ import annotations

import json
import logging
import os
import re
from defense.libs.common.config import get_settings

config = get_settings()
import sys

import structlog

_CONFIGURED = False
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

LOG_LIST_KEY = "logs:recent"
LOG_CHANNEL = "logs:live"

_LOG_MAX = config.log_redis_max
_LOG_TTL = config.log_redis_ttl
_MAX_MSG = 2000

_redis_sink_client = None
_redis_sink_fails = 0
_REDIS_SINK_GIVE_UP = 5


def _redis_enabled() -> bool:
    return config.log_to_redis


def _sink_client():
    global _redis_sink_client
    if _redis_sink_client is None:
        import redis as _redis
        _redis_sink_client = _redis.Redis.from_url(
            config.redis_url,
            decode_responses=True,
            socket_timeout=1.0,
            socket_connect_timeout=1.0,
        )
    return _redis_sink_client


class RedisLogHandler(logging.Handler):
    """A standard logging handler that mirrors logs to Redis."""
    def emit(self, record):
        global _redis_sink_fails
        if not _redis_enabled() or _redis_sink_fails >= _REDIS_SINK_GIVE_UP:
            return

        try:
            r = _sink_client()
            if not r:
                return

            msg = self.format(record)
            clean_msg = _ANSI_RE.sub("", msg) if isinstance(msg, str) else str(msg)
            
            # Extract fields if using structlog
            # structlog puts event_dict in record.msg if formatted a certain way,
            # but simpler to just dump the formatted string for now.
            entry = {
                "ts": record.created,
                "level": record.levelname,
                "service": getattr(record, "service", "-"),
                "message": clean_msg[:_MAX_MSG],
                "fields": {},
                "module": f"{record.module}:{record.lineno}",
            }
            
            payload = json.dumps(entry)
            
            pipe = r.pipeline()
            pipe.lpush(LOG_LIST_KEY, payload)
            pipe.ltrim(LOG_LIST_KEY, 0, _LOG_MAX - 1)
            pipe.expire(LOG_LIST_KEY, _LOG_TTL)
            pipe.publish(LOG_CHANNEL, payload)
            pipe.execute()
            
            _redis_sink_fails = 0
        except Exception:
            _redis_sink_fails += 1


def setup_logging(service_name: str) -> None:
    """Configure structlog as the one sink."""
    global _CONFIGURED
    if _CONFIGURED:
        return

    log_level = config.log_level.upper()

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=log_level,
    )

    root_logger = logging.getLogger()
    
    if _redis_enabled():
        redis_handler = RedisLogHandler()
        redis_handler.setLevel(log_level)
        root_logger.addHandler(redis_handler)

    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.UnicodeDecoder(),
            structlog.processors.dict_tracebacks,
            structlog.dev.ConsoleRenderer() if config.app_env == "dev" else structlog.processors.JSONRenderer(),
        ],
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )
    
    # Bind service name to the base logger
    structlog.contextvars.bind_contextvars(service=service_name)
    _CONFIGURED = True

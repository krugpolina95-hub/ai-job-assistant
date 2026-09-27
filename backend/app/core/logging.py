"""Structured logging (TD-21).

Everything, including uvicorn and SQLAlchemy stdlib loggers, is rendered by structlog: JSON
lines in deployed environments, readable console output locally. Request-scoped values
(`request_id`) are bound with `structlog.contextvars` by the request middleware.
"""

import logging
import sys
from collections.abc import MutableMapping
from typing import Any, Literal, TextIO

import structlog
from structlog.typing import EventDict, Processor

LogFormat = Literal["json", "console"]

REDACTED = "[REDACTED]"
SENSITIVE_KEYS = frozenset(
    {
        "password",
        "password_hash",
        "token",
        "access_token",
        "refresh_token",
        "session",
        "sid",
        "csrf",
        "authorization",
        "cookie",
        "set-cookie",
        "secret",
        "api_key",
        "email",
    }
)


def redact_sensitive(
    logger: Any, method_name: str, event_dict: MutableMapping[str, Any]
) -> EventDict:
    """Last line of defence: never emit secrets or PII even if a caller logs them by mistake."""
    for key in event_dict:
        if key.lower() in SENSITIVE_KEYS:
            event_dict[key] = REDACTED
    return dict(event_dict)


def configure_logging(level: str, log_format: LogFormat, stream: TextIO | None = None) -> None:
    shared_processors: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        redact_sensitive,
    ]
    renderer: list[Processor] = (
        [structlog.processors.dict_tracebacks, structlog.processors.JSONRenderer()]
        if log_format == "json"
        else [structlog.dev.ConsoleRenderer()]
    )

    structlog.configure(
        processors=[*shared_processors, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,
    )

    handler = logging.StreamHandler(stream or sys.stdout)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=shared_processors,
            processors=[structlog.stdlib.ProcessorFormatter.remove_processors_meta, *renderer],
        )
    )
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)

    # uvicorn installs its own handlers before the app factory runs; route them through ours.
    for name in ("uvicorn", "uvicorn.error"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True
    # httpx logs every request URL at INFO. Some sources (Adzuna) take the API key as a query
    # parameter, so those lines would leak secrets: keep only warnings and errors.
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)
    # Our request middleware writes the access log, with request_id and duration.
    access_logger = logging.getLogger("uvicorn.access")
    access_logger.handlers.clear()
    access_logger.propagate = False
    access_logger.disabled = True

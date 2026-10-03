"""Structured logging with secret scrubbing.

Every log event passes through ``scrub`` which masks values of sensitive keys
and anything that looks like a credential, so provider keys, API keys,
session tokens and cookies never reach log storage or Sentry.
"""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import Mapping, MutableMapping
from typing import Any

import structlog

SENSITIVE_KEYS = re.compile(
    r"(pass(word)?|secret|token|api[_-]?key|authorization|cookie|set-cookie|session|"
    r"credential|private[_-]?key|dek|ciphertext|x-api-key)",
    re.IGNORECASE,
)
SECRET_VALUE = re.compile(
    r"(sk-ant-[A-Za-z0-9_\-]{8,}|sk-[A-Za-z0-9_\-]{16,}|rk_[a-z0-9]{8}_[A-Za-z0-9_\-]{16,}|"
    r"gh[pousr]_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|xox[abposr]-[A-Za-z0-9\-]{10,}|"
    r"Bearer\s+[A-Za-z0-9._~+/\-]{16,}=*)"
)
MASK = "[FILTERED]"


def scrub(value: Any, depth: int = 0) -> Any:
    if depth > 8:
        return "[DEPTH]"
    if isinstance(value, Mapping):
        out: dict[Any, Any] = {}
        for k, v in value.items():
            if isinstance(k, str) and SENSITIVE_KEYS.search(k):
                out[k] = MASK
            else:
                out[k] = scrub(v, depth + 1)
        return out
    if isinstance(value, list | tuple):
        return [scrub(v, depth + 1) for v in value]
    if isinstance(value, str):
        return SECRET_VALUE.sub(MASK, value)
    return value


def _scrub_processor(_: Any, __: str, event_dict: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
    return scrub(event_dict)


def configure_logging(level: str = "INFO", json_logs: bool = True) -> None:
    timestamper = structlog.processors.TimeStamper(fmt="iso", utc=True)
    shared: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        timestamper,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        _scrub_processor,
    ]
    renderer: Any = structlog.processors.JSONRenderer() if json_logs else structlog.dev.ConsoleRenderer(colors=False)
    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared, processors=[structlog.stdlib.ProcessorFormatter.remove_processors_meta, renderer]
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())
    # Never log request bodies or SQL parameters from libraries.
    for noisy in ("uvicorn.access", "sqlalchemy.engine", "botocore", "httpx", "httpcore", "openai", "anthropic"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)


def sentry_before_send(event: dict[str, Any], hint: dict[str, Any]) -> dict[str, Any] | None:
    """Scrub Sentry events: drop request bodies/cookies, mask secrets everywhere."""
    request = event.get("request")
    if isinstance(request, dict):
        request.pop("data", None)
        request.pop("cookies", None)
        headers = request.get("headers")
        if isinstance(headers, dict):
            request["headers"] = {k: (MASK if SENSITIVE_KEYS.search(k) else v) for k, v in headers.items()}
    return scrub(event)

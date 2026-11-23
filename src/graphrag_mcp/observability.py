"""Structured JSON logging (Cloud Logging compatible) and per-request context."""

from __future__ import annotations

import contextvars
import hashlib
import logging
import sys
import uuid
from collections.abc import MutableMapping
from typing import Any

import structlog

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")
# Accumulates Neo4j time (ms) for the current tool call; a one-element list so nested
# awaits mutate the same cell.
db_time_var: contextvars.ContextVar[list[float] | None] = contextvars.ContextVar("db_time", default=None)

_SEVERITY = {"debug": "DEBUG", "info": "INFO", "warning": "WARNING", "error": "ERROR", "critical": "CRITICAL"}


def new_request_id() -> str:
    rid = uuid.uuid4().hex
    request_id_var.set(rid)
    return rid


def hash_client(client_id: str | None) -> str:
    """Stable, non-reversible client label for logs (never the token or raw subject)."""
    return hashlib.sha256((client_id or "anonymous").encode()).hexdigest()[:12]


def _add_severity(_: Any, method: str, event: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
    event["severity"] = _SEVERITY.get(method, "DEFAULT")
    return event


def _add_request_id(_: Any, __: str, event: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
    event.setdefault("request_id", request_id_var.get())
    return event


def configure_logging(level: str = "INFO", static: dict[str, Any] | None = None) -> None:
    logging.basicConfig(stream=sys.stdout, level=level.upper(), format="%(message)s", force=True)
    # Keep the access logs of dependencies from echoing tokens or URLs with secrets.
    for noisy in ("httpx", "httpcore", "neo4j.notifications", "neo4j.io", "neo4j.pool"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            _add_request_id,
            _add_severity,
            structlog.processors.TimeStamper(fmt="iso", utc=True, key="timestamp"),
            structlog.processors.EventRenamer("message"),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level.upper())),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )
    structlog.contextvars.clear_contextvars()
    if static:
        structlog.contextvars.bind_contextvars(**static)


def get_logger(name: str = "graphrag") -> Any:
    return structlog.get_logger(name)

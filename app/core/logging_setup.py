"""Structured logging.

Every log line is one JSON object on stdout. Fields passed through ``extra``
become top-level keys, and the current request ID is added when there is one.

Application logs must not carry document text, questions or credentials.
That content belongs in the audit log, which has its own access control.
The formatter redacts a fixed set of field names as a safety net, but the
rule for callers is simply: log IDs, counts and timings, not content.
"""

import json
import logging
import sys
import uuid
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

REDACTED = "[redacted]"

# Field names that must never reach the application log.
BLOCKED_FIELDS = frozenset(
    {
        "password",
        "token",
        "authorization",
        "secret",
        "question",
        "query",
        "answer",
        "prompt",
        "context",
        "content",
        "text",
    }
)

# Libraries whose INFO lines are one per HTTP request they make.
QUIET_LIBRARIES = ("httpx", "httpcore", "huggingface_hub", "urllib3")

_request_id: ContextVar[str | None] = ContextVar("request_id", default=None)

# Attributes present on every LogRecord. Anything else came in through `extra`.
_RECORD_ATTRS = frozenset(vars(logging.LogRecord("", 0, "", 0, "", (), None))) | {
    "message",
    "asctime",
    "taskName",
}


def bind_request_id(request_id: str | None = None) -> str:
    """Set the request ID for the current context and return it."""
    value = request_id or uuid.uuid4().hex
    _request_id.set(value)
    return value


def clear_request_id() -> None:
    _request_id.set(None)


def current_request_id() -> str | None:
    return _request_id.get()


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        request_id = _request_id.get()
        if request_id:
            entry["request_id"] = request_id

        for key, value in vars(record).items():
            if key in _RECORD_ATTRS or key in entry:
                continue
            entry[key] = REDACTED if key.lower() in BLOCKED_FIELDS else value

        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)

        return json.dumps(entry, default=str)


def configure_logging(level: str) -> None:
    """Send all logs to stdout as JSON. Call once at startup."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)

    # These log every HTTP request they make at INFO: each check of the
    # embedding model's files at startup, and each call to the language model.
    # Only their warnings and errors are kept. What the service sends is
    # already logged by the service, as counts.
    for library in QUIET_LIBRARIES:
        logging.getLogger(library).setLevel(max(logging.WARNING, root.level))

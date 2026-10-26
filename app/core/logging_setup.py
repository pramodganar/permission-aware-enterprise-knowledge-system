"""Structured logging.

Every log line is one JSON object on stdout. Fields passed through ``extra``
become top-level keys, and the current request ID is added when there is one.
That includes the lines of the server, uvicorn, and the warnings libraries
raise, which would otherwise be plain text on stderr: a log collector reading
JSON would get two formats on two streams. Every event, its level and its
fields is listed in docs/logging.md.

Application logs must not carry document text, questions or credentials.
That content belongs in the audit log, which has its own access control.
The formatter redacts a fixed set of field names as a safety net, but the
rule for callers is simply: log IDs, counts and timings, not content.
"""

import json
import logging
import sys
import time
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

# Libraries that read uploaded files, and whose warnings and errors quote the
# bytes they tripped over: the document's own text, from a restricted
# document as easily as any other. Found by fuzzing the PDF extractor. Their
# lines are never wanted: the extractor turns every failure into one fixed
# message of its own.
DOCUMENT_PARSERS = ("pypdf",)
# Above every level there is, so nothing they log is written.
SILENT = logging.CRITICAL + 1

# uvicorn gives these handlers of its own, writing plain text to stderr,
# before it calls the app factory. Without them, their lines go through the
# root handler like everyone else's, at the root's level.
SERVER_LOGGERS = ("uvicorn", "uvicorn.error")
# uvicorn's access log writes each request's full URL, query string and all,
# and the client's address. The service logs every request itself, without
# either, so the access log is silenced here as well as by --no-access-log.
ACCESS_LOG = "uvicorn.access"

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


def milliseconds_since(started: float) -> int:
    """Whole milliseconds since ``started``, a ``time.perf_counter()`` reading."""
    return round((time.perf_counter() - started) * 1000)


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
    for parser in DOCUMENT_PARSERS:
        logging.getLogger(parser).setLevel(SILENT)

    for name in SERVER_LOGGERS:
        server = logging.getLogger(name)
        server.handlers = []
        server.propagate = True
        server.setLevel(logging.NOTSET)
    access = logging.getLogger(ACCESS_LOG)
    access.handlers = []
    access.setLevel(SILENT)
    # warnings.warn() from a library, logged at WARNING as py.warnings.
    logging.captureWarnings(True)

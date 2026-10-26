"""Looking at everything a log record carries, and undoing what configure_logging set up."""

import logging
from collections.abc import Iterator
from contextlib import contextmanager

import pytest

# What every record has, whatever was logged.
_STANDARD = frozenset(vars(logging.LogRecord("", 0, "", 0, "", None, None))) | {
    "message",
    "asctime",
}


def everything_logged(caplog: pytest.LogCaptureFixture) -> str:
    """Every message and every extra field of every captured record, as one string.

    ``caplog.text`` only holds the formatted messages. A value passed in
    ``extra`` is not in it, but it is written out by the JSON formatter.
    """
    parts = []
    for record in caplog.records:
        parts.append(record.getMessage())
        parts.extend(
            f"{name}={value!r}" for name, value in vars(record).items() if name not in _STANDARD
        )
    return "\n".join(parts)


@contextmanager
def logging_restored() -> Iterator[None]:
    """Put every logger back as it was, however the block ends.

    configure_logging changes the root, the libraries it quiets, uvicorn's
    loggers and where warnings go; a test that calls it must not leave any
    of that for the next one.
    """
    known = logging.Logger.manager.loggerDict.values()
    loggers = [
        logging.getLogger(),
        *(found for found in known if isinstance(found, logging.Logger)),
    ]
    saved = [(found, found.handlers[:], found.level, found.propagate) for found in loggers]
    try:
        yield
    finally:
        for found, handlers, level, propagate in saved:
            found.handlers = handlers
            found.setLevel(level)
            found.propagate = propagate
        logging.captureWarnings(False)

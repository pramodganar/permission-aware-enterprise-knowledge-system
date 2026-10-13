"""Looking at everything a log record carries, not only its message."""

import logging

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

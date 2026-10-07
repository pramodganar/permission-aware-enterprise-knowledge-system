import json
import logging
import sys
from collections.abc import Iterator
from typing import Any

import pytest

from app.core.logging_setup import (
    BLOCKED_FIELDS,
    REDACTED,
    JsonFormatter,
    bind_request_id,
    clear_request_id,
    configure_logging,
)


@pytest.fixture(autouse=True)
def no_request_id() -> Iterator[None]:
    clear_request_id()
    yield
    clear_request_id()


@pytest.fixture
def restore_root_logger() -> Iterator[None]:
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield
    root.handlers, root.level = handlers, level


def format_record(message: str, *args: object, level: int = logging.INFO, **extra: Any) -> dict[str, Any]:
    record = logging.LogRecord("app.test", level, __file__, 1, message, args, None)
    for key, value in extra.items():
        setattr(record, key, value)
    return json.loads(JsonFormatter().format(record))


def test_line_has_the_basic_fields() -> None:
    entry = format_record("retrieved %d chunks", 4)

    assert entry["message"] == "retrieved 4 chunks"
    assert entry["level"] == "INFO"
    assert entry["logger"] == "app.test"
    assert entry["timestamp"].endswith("+00:00")


def test_extra_fields_become_top_level_keys() -> None:
    entry = format_record("search done", chunk_count=4, duration_ms=12.5, user_id=7)

    assert entry["chunk_count"] == 4
    assert entry["duration_ms"] == 12.5
    assert entry["user_id"] == 7


@pytest.mark.parametrize("field", sorted(BLOCKED_FIELDS))
def test_blocked_fields_are_redacted(field: str) -> None:
    entry = format_record("event", **{field: "executive compensation strategy"})

    assert entry[field] == REDACTED
    assert "executive compensation" not in json.dumps(entry)


def test_blocked_field_names_are_matched_without_case() -> None:
    entry = format_record("event", Question="what do executives earn")

    assert entry["Question"] == REDACTED


def test_extra_cannot_overwrite_basic_fields() -> None:
    entry = format_record("real message", level=logging.ERROR, logger="spoofed")

    assert entry["logger"] == "app.test"
    assert entry["level"] == "ERROR"


def test_request_id_is_added_when_bound() -> None:
    request_id = bind_request_id()

    assert format_record("event")["request_id"] == request_id


def test_given_request_id_is_kept() -> None:
    bind_request_id("abc-123")

    assert format_record("event")["request_id"] == "abc-123"


def test_no_request_id_field_outside_a_request() -> None:
    assert "request_id" not in format_record("event")


def test_values_that_are_not_json_do_not_break_logging() -> None:
    entry = format_record("event", when=object())

    assert isinstance(entry["when"], str)


def test_exception_is_included() -> None:
    try:
        raise ValueError("boom")
    except ValueError:
        record = logging.LogRecord("app.test", logging.ERROR, __file__, 1, "failed", (), sys.exc_info())

    entry = json.loads(JsonFormatter().format(record))

    assert "ValueError: boom" in entry["exception"]


def test_configure_logging_writes_json_to_stdout(
    restore_root_logger: None, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging("INFO")
    bind_request_id("req-1")

    logging.getLogger("app.retrieval").info("search done", extra={"chunk_count": 3})

    line = json.loads(capsys.readouterr().out.strip())
    assert line["message"] == "search done"
    assert line["chunk_count"] == 3
    assert line["request_id"] == "req-1"


def test_configure_logging_respects_level(
    restore_root_logger: None, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging("WARNING")

    logging.getLogger("app").info("not shown")
    logging.getLogger("app").warning("shown")

    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["message"] == "shown"


def test_configure_logging_twice_does_not_duplicate_lines(
    restore_root_logger: None, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging("INFO")
    configure_logging("INFO")

    logging.getLogger("app").info("once")

    assert len(capsys.readouterr().out.strip().splitlines()) == 1

import json
import logging
import sys
import time
import warnings
from collections.abc import Iterator
from typing import Any

import pytest
from uvicorn.config import Config

from app.core.logging_setup import (
    ACCESS_LOG,
    BLOCKED_FIELDS,
    REDACTED,
    JsonFormatter,
    bind_request_id,
    clear_request_id,
    configure_logging,
    milliseconds_since,
)
from logcheck import logging_restored


@pytest.fixture(autouse=True)
def no_request_id() -> Iterator[None]:
    clear_request_id()
    yield
    clear_request_id()


@pytest.fixture
def restore_root_logger() -> Iterator[None]:
    with logging_restored():
        yield


def format_record(
    message: str,
    *args: object,
    level: int = logging.INFO,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    record = logging.LogRecord("app.test", level, __file__, 1, message, args, None)
    for key, value in (extra or {}).items():
        setattr(record, key, value)
    entry: dict[str, Any] = json.loads(JsonFormatter().format(record))
    return entry


def test_line_has_the_basic_fields() -> None:
    entry = format_record("retrieved %d chunks", 4)

    assert entry["message"] == "retrieved 4 chunks"
    assert entry["level"] == "INFO"
    assert entry["logger"] == "app.test"
    assert entry["timestamp"].endswith("+00:00")


def test_extra_fields_become_top_level_keys() -> None:
    entry = format_record(
        "search done", extra={"chunk_count": 4, "duration_ms": 12.5, "user_id": 7}
    )

    assert entry["chunk_count"] == 4
    assert entry["duration_ms"] == 12.5
    assert entry["user_id"] == 7


@pytest.mark.parametrize("field", sorted(BLOCKED_FIELDS))
def test_blocked_fields_are_redacted(field: str) -> None:
    entry = format_record("event", extra={field: "executive compensation strategy"})

    assert entry[field] == REDACTED
    assert "executive compensation" not in json.dumps(entry)


def test_blocked_field_names_are_matched_without_case() -> None:
    entry = format_record("event", extra={"Question": "what do executives earn"})

    assert entry["Question"] == REDACTED


def test_extra_cannot_overwrite_basic_fields() -> None:
    entry = format_record("real message", level=logging.ERROR, extra={"logger": "spoofed"})

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
    entry = format_record("event", extra={"when": object()})

    assert isinstance(entry["when"], str)


def test_exception_is_included() -> None:
    try:
        raise ValueError("boom")
    except ValueError:
        record = logging.LogRecord(
            "app.test", logging.ERROR, __file__, 1, "failed", (), sys.exc_info()
        )

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


def test_libraries_that_log_every_http_request_keep_only_warnings(
    restore_root_logger: None, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging("INFO")

    logging.getLogger("httpx").info("HTTP Request: POST https://api.groq.com/openai/v1/chat")
    logging.getLogger("huggingface_hub.utils").info("HEAD https://huggingface.co/...")
    logging.getLogger("httpx").warning("connection pool full")
    logging.getLogger("app.rag.llm").info("model answered")

    messages = [json.loads(line)["message"] for line in capsys.readouterr().out.splitlines()]
    assert messages == ["connection pool full", "model answered"]


def as_uvicorn_leaves_it() -> None:
    # What uvicorn does before it calls the app factory: its own handlers,
    # writing plain text, on its own loggers.
    Config("app.main:serve", factory=True).configure_logging()


def test_the_servers_own_lines_are_json_on_stdout(
    restore_root_logger: None, capsys: pytest.CaptureFixture[str]
) -> None:
    as_uvicorn_leaves_it()
    configure_logging("INFO")

    logging.getLogger("uvicorn.error").info("Application startup complete.")
    logging.getLogger("uvicorn").warning("Invalid HTTP request received.")

    written = capsys.readouterr()
    lines = [json.loads(line) for line in written.out.splitlines()]
    assert [(line["logger"], line["message"]) for line in lines] == [
        ("uvicorn.error", "Application startup complete."),
        ("uvicorn", "Invalid HTTP request received."),
    ]
    assert written.err == ""


def test_the_servers_lines_follow_the_services_level(
    restore_root_logger: None, capsys: pytest.CaptureFixture[str]
) -> None:
    # uvicorn sets its own level, INFO; APP_LOG_LEVEL is the one that counts.
    as_uvicorn_leaves_it()
    configure_logging("WARNING")

    logging.getLogger("uvicorn.error").info("Started server process [1]")

    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("uvicorns_config", [True, False], ids=["uvicorns-config", "propagating"])
def test_the_access_log_writes_nothing_even_when_uvicorn_is_told_to(
    restore_root_logger: None, capsys: pytest.CaptureFixture[str], uvicorns_config: bool
) -> None:
    # Its lines hold the full URL and the client's address. Without
    # --no-access-log uvicorn would write them; they still go nowhere. Nor
    # when the access logger passes its lines up to the root, as a logger
    # does unless told otherwise, and as it does without uvicorn's config.
    if uvicorns_config:
        as_uvicorn_leaves_it()
    else:
        logging.getLogger(ACCESS_LOG).propagate = True
    configure_logging("DEBUG")

    logging.getLogger(ACCESS_LOG).info(
        '%s - "%s %s HTTP/%s" %d', "10.0.0.7:5555", "GET", "/search?question=salary", "1.1", 200
    )

    assert capsys.readouterr() == ("", "")


def test_a_librarys_warning_is_a_json_line_not_text_on_stderr(
    restore_root_logger: None, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging("INFO")

    with warnings.catch_warnings():
        warnings.simplefilter("always")
        warnings.warn("this argument is deprecated", FutureWarning, stacklevel=1)

    written = capsys.readouterr()
    (line,) = [json.loads(line) for line in written.out.splitlines()]
    assert line["logger"] == "py.warnings"
    assert line["level"] == "WARNING"
    assert "FutureWarning: this argument is deprecated" in line["message"]
    assert written.err == ""


def test_milliseconds_since_counts_whole_milliseconds() -> None:
    elapsed = milliseconds_since(time.perf_counter() - 0.25)

    assert isinstance(elapsed, int)
    assert 250 <= elapsed < 1000

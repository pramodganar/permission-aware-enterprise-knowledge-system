"""docs/logging.md against the log calls in app/, read without running them.

Every event the service can write is on the page with its level, its
logger and exactly its fields, and nothing is on the page that the code
doesn't write. And every call is written so that content can't reach the
log by way of it: a message that is fixed text, values only in named
fields, and no field named for what the formatter would redact.
"""

import ast
import re
from dataclasses import dataclass
from pathlib import Path
from typing import get_args

import pytest

from app.core.config import Settings
from app.core.logging_setup import BLOCKED_FIELDS, DOCUMENT_PARSERS, QUIET_LIBRARIES
from logcalls import calls

ROOT = Path(__file__).resolve().parents[2]
PAGE = ROOT / "docs" / "logging.md"
_ROW = re.compile(r"^\| `(?P<message>[^`]+)` \| (?P<level>[^|]+) \| `(?P<logger>app\.[\w.]+)` \|")


@dataclass(frozen=True)
class Row:
    message: str
    level: str
    logger: str
    fields: tuple[str, ...]


def rows() -> list[Row]:
    found = []
    for line in PAGE.read_text(encoding="utf-8").splitlines():
        match = _ROW.match(line)
        if match:
            cells = [cell.strip() for cell in line.strip("|").split("|")]
            fields = tuple(re.findall(r"`(\w+)`", cells[3]))
            found.append(Row(match["message"], match["level"].strip(), match["logger"], fields))
    return found


def test_the_reader_finds_the_service_s_log_calls() -> None:
    found = calls()

    assert len(found) >= 40
    assert any(c.logger == "app.api.middleware" and c.level is None for c in found)


def test_every_module_that_logs_names_its_logger_after_itself() -> None:
    # So the logger on the page is the module the line came from.
    for path in {ROOT / c.where.split(":")[0] for c in calls()}:
        assert "logger = logging.getLogger(__name__)" in path.read_text(encoding="utf-8"), path


def test_every_message_is_fixed_text() -> None:
    # An f-string or a formatted message would put a value where nobody
    # looks for one and nothing redacts it.
    for call in calls():
        assert isinstance(call.message, ast.Constant), f"{call.where}: the message isn't plain text"
        assert isinstance(call.message.value, str), call.where
        assert not call.formatted, f"{call.where}: values formatted into the message"


def test_every_value_is_a_named_field() -> None:
    for call in calls():
        if call.extra is None:
            continue
        assert isinstance(call.extra, ast.Dict), f"{call.where}: extra isn't written out"
        assert all(isinstance(k, ast.Constant) for k in call.extra.keys), call.where
        assert len(set(call.fields)) == len(call.fields), call.where


def test_no_field_is_named_for_what_the_formatter_redacts() -> None:
    # The redaction is a safety net; it must never be what keeps a document
    # out of the log.
    for call in calls():
        named = {field.lower() for field in call.fields} & BLOCKED_FIELDS
        assert not named, f"{call.where}: {sorted(named)}"


def test_every_event_is_on_the_page_as_it_is_written() -> None:
    documented = {(row.logger, row.message): row for row in rows()}

    for call in calls():
        row = documented.get((call.logger, call.text))
        assert row is not None, f"{call.where}: {call.text!r} isn't in docs/logging.md"
        assert row.fields == call.fields, f"{call.text}: the page says {row.fields}"
        if call.level is None:
            # Decided when it runs: the page has to say which levels.
            assert "INFO" in row.level and "DEBUG" in row.level, call.text
        else:
            assert row.level == call.level, f"{call.text}: the page says {row.level}"


def test_nothing_is_on_the_page_that_the_code_doesnt_write() -> None:
    written = {(call.logger, call.text) for call in calls()}
    documented = [(row.logger, row.message) for row in rows()]

    assert len(documented) == len(set(documented)), "an event is on the page twice"
    assert set(documented) - written == set()


def test_the_page_names_every_level_there_is_and_the_default() -> None:
    page = PAGE.read_text(encoding="utf-8")
    levels = set(re.findall(r"^\| `([A-Z]+)` \|", page, re.MULTILINE))

    assert levels == set(get_args(Settings.model_fields["log_level"].annotation))
    assert f"`{Settings.model_fields['log_level'].default}` by default" in page


@pytest.mark.parametrize("library", [*QUIET_LIBRARIES, *DOCUMENT_PARSERS])
def test_the_page_names_every_library_kept_quiet(library: str) -> None:
    assert f"`{library}`" in PAGE.read_text(encoding="utf-8")


def test_the_page_lists_the_names_the_formatter_redacts() -> None:
    page = PAGE.read_text(encoding="utf-8")
    sentence = next(line for line in page.splitlines() if "with `[redacted]`" in line)

    assert set(re.findall(r"`(\w+)`", sentence.split("with `[redacted]`")[0])) == BLOCKED_FIELDS


def test_the_reader_reads_a_row() -> None:
    line = "| `search done` | INFO | `app.retrieval.search` | `user_id`, `hits` |"

    match = _ROW.match(line)

    assert match and match["message"] == "search done" and match["logger"] == "app.retrieval.search"

"""scripts/monitor.py: reading the log, each rule's threshold, the report (docs/monitoring.md)."""

import io
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from evaluation.targets import TARGETS
from logcalls import calls
from scripts import monitor
from scripts.monitor import RULES, Match, Severity, Window, check, main, read

START = datetime(2026, 10, 26, 9, 0, tzinfo=UTC)


def line(message: str, seconds: float = 0, **fields: Any) -> str:
    entry = {
        "timestamp": (START + timedelta(seconds=seconds)).isoformat(timespec="milliseconds"),
        "level": "INFO",
        "logger": "app.test",
        "message": message,
        **fields,
    }
    return json.dumps(entry)


def request(path: str = "/search", status: int = 200, milliseconds: int = 40) -> str:
    method = "GET" if path.startswith("/documents") else "POST"
    return line(
        "request handled", method=method, path=path, status=status, milliseconds=milliseconds
    )


def rule(name: str) -> monitor.Rule:
    return next(r for r in RULES if r.name == name)


def fires(name: str, lines: list[str]) -> bool:
    _, fired = rule(name).check(read(lines))
    return fired


def value(name: str, lines: list[str]) -> float | None:
    found, _ = rule(name).check(read(lines))
    return found


# --- reading -----------------------------------------------------------------------------


def test_only_the_last_minutes_before_the_newest_line_are_read() -> None:
    lines = [line("login failed", 0), line("login failed", 60 * 12), line("login failed", 60 * 26)]

    window = read(lines, minutes=15)

    assert len(window.lines) == 2
    assert window.start == START + timedelta(minutes=12)
    assert window.end == START + timedelta(minutes=26)
    assert window.minutes == 14


def test_lines_with_a_compose_prefix_are_read() -> None:
    window = read([f"api-1  | {line('login failed')}"])

    assert len(window.lines) == 1
    assert window.not_json == 0


@pytest.mark.parametrize(
    "raw",
    [
        "INFO:     Started server process [1]",
        "Loading weights:   0%|          | 0/103",
        '{"message": "no timestamp"}',
        '{"timestamp": "yesterday", "message": "x"}',
        "[1, 2, 3]",
        '{"timestamp": "2026-10-26T09:00:00+00:00"}',
    ],
)
def test_a_line_the_service_wouldnt_write_is_counted_not_read(raw: str) -> None:
    window = read([line("login failed"), raw])

    assert window.not_json == 1
    assert len(window.lines) == 1
    assert fires("log-not-json", [line("login failed"), raw])


def test_blank_lines_are_nothing() -> None:
    window = read(["", "   ", line("login failed")])

    assert window.not_json == 0
    assert len(window.lines) == 1


def test_a_line_from_an_older_version_without_a_field_doesnt_count_or_break_anything() -> None:
    # Logs from before the stages were timed have no milliseconds on them.
    old = [line("chunks retrieved", user_id=4, chunks=5)] * 30
    old += [line("request handled", method="POST", path="/ask", status=200)] * 30

    assert value("retrieval-slow", old) is None
    assert value("answers-slow", old) is None
    assert monitor.routes(read(old)) == []


# --- each rule fires at its threshold and not below it --------------------------------------

COUNTED: dict[str, Callable[[], str]] = {
    "audit-not-written": lambda: line("audit record not written", action="search", error="X"),
    "unexpected-errors": lambda: line("unexpected error", error="RuntimeError", where="..."),
    "model-failing": lambda: line("model call failed", provider="groq", attempt=1, error="X"),
    "links-removed": lambda: line("question answered", outcome="answered", removed_links=2),
    "logins-failing": lambda: line("login failed", reason="wrong password", user_id=4),
    "requests-limited": lambda: line("request limited", user_id=4, route="/ask"),
    "documents-probed": lambda: line(
        "document not returned", user_id=4, document_id=16, exists=True
    ),
    "actions-denied": lambda: line("action denied", user_id=4, permission="documents:manage"),
}


@pytest.mark.parametrize("name", sorted(COUNTED))
def test_a_counted_rule_fires_at_its_threshold(name: str) -> None:
    threshold = int(rule(name).threshold)
    make = COUNTED[name]

    assert fires(name, [make() for _ in range(threshold)])
    assert not fires(name, [make() for _ in range(threshold - 1)] + [request()])


def test_every_counted_rule_is_tested() -> None:
    counted = {r.name for r in RULES if isinstance(r.measure, monitor.Count)}

    assert counted == set(COUNTED) | {"access-check-disagreed", "index-behind"}


@pytest.mark.parametrize(
    "message",
    [
        "search returned chunks outside the scope",
        "vector search returned chunks outside the allowed list",
        "second check removed chunks the scope allowed",
    ],
)
def test_any_access_check_disagreeing_pages_at_once(message: str) -> None:
    report = check(read([line(message, user_id=4, dropped=1)]))

    assert [c.rule.name for c in report.fired] == ["access-check-disagreed"]
    assert report.worst is Severity.PAGE


def test_a_missing_document_isnt_probing() -> None:
    # Asking for a document that doesn't exist is a typo, not a probe.
    missing = line("document not returned", user_id=4, document_id=999, exists=False)

    assert not fires("documents-probed", [missing] * 50)


def test_an_answer_with_no_links_removed_doesnt_count() -> None:
    clean = line("question answered", outcome="answered", removed_links=0)

    assert value("links-removed", [clean] * 5) == 0


def test_the_index_is_behind_when_the_service_says_so_or_a_search_finds_unindexed_chunks() -> None:
    behind = (
        "the vector index is behind the database; rebuild it with: python -m scripts.index build"
    )
    unindexed = line("search done", user_id=4, in_scope=13, not_indexed=2, hits=5)
    indexed = line("search done", user_id=4, in_scope=13, not_indexed=0, hits=5)

    assert fires("index-behind", [line(behind, missing=3, retired=0)])
    assert fires("index-behind", [unindexed])
    assert not fires("index-behind", [indexed] * 50)


def test_server_errors_are_judged_on_twenty_requests_or_more() -> None:
    one_in_twenty = [request(status=500)] + [request()] * 19

    assert fires("server-errors", one_in_twenty)
    assert value("server-errors", one_in_twenty) == 5.0
    assert not fires("server-errors", [request(status=500)] + [request()] * 20)
    assert value("server-errors", [request(status=500)] * 19) is None


def test_a_refused_question_is_not_a_server_error() -> None:
    # 503: asking is off or the model is down. Its own rule watches the model.
    assert value("server-errors", [request("/ask", status=503)] * 40) == 0


def chunks_retrieved(milliseconds: int) -> str:
    return line("chunks retrieved", user_id=4, chunks=5, milliseconds=milliseconds)


def test_retrieval_is_slow_when_its_95th_percentile_reaches_the_target() -> None:
    # Nearest rank: of 20, the 19th smallest.
    slow = [chunks_retrieved(10)] * 18 + [chunks_retrieved(500)] * 2
    almost = [chunks_retrieved(10)] * 19 + [chunks_retrieved(5000)]

    assert fires("retrieval-slow", slow)
    assert not fires("retrieval-slow", almost)
    assert value("retrieval-slow", [chunks_retrieved(9000)] * 19) is None


def test_only_answered_questions_count_towards_answer_latency() -> None:
    answered = [request("/ask", milliseconds=6000)] * 20
    refused = [request("/ask", status=503, milliseconds=6000)] * 20
    searches = [request("/search", milliseconds=6000)] * 20

    assert fires("answers-slow", answered)
    assert value("answers-slow", refused + searches) is None


def answered(outcome: str) -> str:
    return line("question answered", outcome=outcome, removed_links=0)


def test_no_information_is_high_from_half_of_twenty_questions() -> None:
    half = [answered("no_information")] * 10 + [answered("answered")] * 10
    under = [answered("no_information")] * 9 + [answered("answered")] * 11

    assert fires("no-information-high", half)
    assert value("no-information-high", under) == 45.0
    assert not fires("no-information-high", under)
    assert value("no-information-high", [answered("no_information")] * 19) is None


# --- the report ---------------------------------------------------------------------------


def test_nothing_firing_says_so() -> None:
    report = check(read([request()] * 30))

    assert report.worst is None
    assert monitor.lines_of(report)[-1] == "nothing needs anyone"


def test_pages_come_first_with_where_to_read_what_to_do() -> None:
    lines = [line("login failed")] * 20 + [line("audit record not written")]

    shown = monitor.lines_of(check(read(lines)))

    assert shown[-3:] == [
        "2 firing, worst: page",
        "  page: docs/monitoring.md#audit-not-written",
        "  ticket: docs/monitoring.md#logins-failing",
    ]


def test_requests_are_shown_by_route_busiest_first() -> None:
    lines = [request("/ask", milliseconds=900)] * 2 + [request("/search", milliseconds=40)] * 3

    shown = monitor.routes(read(lines))

    assert shown[0].split()[:3] == ["POST", "/search", "3"]
    assert "p95 900 ms" in shown[1]


# --- running it ---------------------------------------------------------------------------


def test_the_exit_code_says_who_is_needed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    quiet = tmp_path / "quiet.log"
    quiet.write_text("\n".join([request()] * 5), encoding="utf-8")
    ticket = tmp_path / "ticket.log"
    ticket.write_text("\n".join([line("login failed")] * 20), encoding="utf-8")
    page = tmp_path / "page.log"
    page.write_text(line("audit record not written"), encoding="utf-8")
    empty = tmp_path / "empty.log"
    empty.write_text("\n\n", encoding="utf-8")

    assert main([str(quiet)]) == 0
    assert main([str(ticket)]) == 1
    assert main([str(page)]) == 2
    assert main([str(empty)]) == monitor.NOTHING_TO_READ
    assert "no log lines to read" in capsys.readouterr().err


def test_it_reads_standard_input_and_takes_a_window(capsys: pytest.CaptureFixture[str]) -> None:
    old_failures = [line("login failed", 0)] * 20
    now = [line("login failed", 60 * 30)]

    assert main([], stdin=io.StringIO("\n".join(old_failures + now))) == 0
    assert main(["--window", "60"], stdin=io.StringIO("\n".join(old_failures + now))) == 1
    assert "21 " in capsys.readouterr().out


# --- the rules and the service agree ------------------------------------------------------


def test_every_event_a_rule_reads_is_one_the_service_writes_with_those_fields() -> None:
    written: dict[str, set[str]] = {}
    for call in calls():
        written.setdefault(call.text, set()).update(call.fields)

    for found in RULES:
        for match in found.measure.matches:
            assert match.event in written, f"{found.name}: {match.event!r} is never logged"
            missing = set(match.fields) - written[match.event]
            assert not missing, f"{found.name}: {match.event!r} has no {sorted(missing)}"


def test_the_latency_rules_are_the_requirements_targets() -> None:
    by_name = {target.name: target for target in TARGETS}

    assert by_name["Retrieval p95"].at_most == monitor.RETRIEVAL_P95_MS
    assert by_name["Answered question p95, with Groq"].at_most == monitor.ANSWER_P95_MS


def test_every_rule_has_its_own_name_and_says_what_it_counts() -> None:
    names = [r.name for r in RULES]

    assert len(names) == len(set(names))
    for found in RULES:
        assert found.says and found.threshold > 0, found.name
        assert found.name == found.name.lower().replace(" ", "-"), found.name


def test_the_reader_of_matches_skips_lines_missing_a_field() -> None:
    window = Window(
        (
            {"message": "search done", "not_indexed": 1},
            {"message": "search done"},
            {"message": "search done", "not_indexed": None},
        ),
        0,
        START,
        START,
    )

    assert len(Match("search done", ("not_indexed",)).lines(window)) == 1


def test_the_report_adds_up_what_the_model_was_sent_and_gave_back() -> None:
    lines = [
        line("model answered", input_tokens=1200, output_tokens=80),
        line("model answered", input_tokens=900, output_tokens=40),
    ]

    assert monitor.usage(read(lines)) == ["  model: 2 answers, 2100 tokens in, 120 tokens out"]


def test_the_report_gives_the_highest_audit_record_to_compare_with_the_trail() -> None:
    lines = [line("audit record sealed", audit_id=n, action="search") for n in (7, 41, 12)]

    (shown,) = monitor.usage(read(lines))

    assert shown.startswith("  audit: highest record sealed 41;")


def test_the_report_leaves_out_what_the_window_doesnt_have() -> None:
    assert monitor.usage(read([request()])) == []

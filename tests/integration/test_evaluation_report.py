"""The one evaluation command, and the report it writes, against the committed results."""

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from evaluation import __main__ as command
from evaluation import report
from evaluation.targets import TARGETS, load_results


def test_the_committed_report_is_the_one_the_committed_results_give() -> None:
    # Write it again with `python -m evaluation --report-only` when this fails.
    assert report.REPORT.read_text(encoding="utf-8") == report.current()


def test_every_target_is_measured_by_a_committed_file_and_every_gate_is_met() -> None:
    results = load_results()
    assert {t.file for t in TARGETS} <= results.keys()
    checked = report.outcomes(results)
    assert all(met is not None for _, _, met in checked)
    assert report.failed_gates(checked) == []


Calls = list[tuple[str, list[str]]]


def recording(calls: Calls, name: str, returns: int = 0) -> Callable[[list[str]], int]:
    def run(args: list[str]) -> int:
        calls.append((name, args))
        return returns

    return run


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Calls:
    found: Calls = []
    for group in (command.OFFLINE, command.TIMINGS, command.GROQ):
        for i, (name, _, args) in enumerate(group):
            group[i] = (name, recording(found, name), args)
    monkeypatch.setattr(command, "write_report", lambda results: None)
    return found


@pytest.fixture(autouse=True)
def restore_steps() -> Any:
    saved = [list(g) for g in (command.OFFLINE, command.TIMINGS, command.GROQ)]
    yield
    for group, kept in zip((command.OFFLINE, command.TIMINGS, command.GROQ), saved, strict=True):
        group[:] = kept


def test_without_options_it_runs_what_needs_no_network(calls: Calls) -> None:
    assert command.main([]) == 0
    assert calls == [
        ("retrieval", []),
        ("retrieval, reranked", ["--rerank"]),
        ("leakage, gate on", []),
        ("leakage, gate off", ["--no-gate"]),
    ]


def test_timings_and_groq_are_asked_for(calls: Calls) -> None:
    command.main(["--latency", "--groq"])
    names = [name for name, _ in calls]
    assert names[4:] == [
        "latency",
        "latency, reranked",
        "leakage, Groq",
        "answers, Groq",
        "judging the answers",
        "latency, Groq",
    ]
    assert ("leakage, Groq", ["--model", "groq"]) in calls


def test_the_report_alone_runs_nothing(calls: Calls, capsys: pytest.CaptureFixture[str]) -> None:
    assert command.main(["--report-only"]) == 0
    assert calls == []
    printed = capsys.readouterr().out
    assert "missed: Forbidden controls answered (Groq) (11 of 12)" in printed
    assert "Gates: 12 met, 0 not" in printed


def test_a_measurement_that_finds_a_leak_doesnt_stop_the_others(
    calls: Calls, monkeypatch: pytest.MonkeyPatch
) -> None:
    name, _, args = command.OFFLINE[0]
    command.OFFLINE[0] = (name, recording(calls, name, returns=1), args)
    command.main([])
    assert len(calls) == 4


def test_a_missed_gate_fails_the_command(calls: Calls, monkeypatch: pytest.MonkeyPatch) -> None:
    results = load_results()
    results["security-worst.json"] = results["security-worst.json"] | {"leaks": 1}
    monkeypatch.setattr(command, "load_results", lambda: results)
    assert command.main(["--report-only"]) == 1


def test_a_gate_nobody_measured_fails_the_command(
    calls: Calls, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    results = load_results()
    del results["retrieval.json"]
    monkeypatch.setattr(command, "load_results", lambda: results)
    assert command.main(["--report-only"]) == 1
    assert "GATE not measured: MRR" in capsys.readouterr().out


def test_the_report_is_written_where_the_docs_link_to_it(tmp_path: Path) -> None:
    out = tmp_path / "report.md"
    report.write_report(load_results(), out)
    assert out.read_bytes() == report.current().encode("utf-8")
    assert Path(__file__).resolve().parents[2] / "docs" / "evaluation-report.md" == report.REPORT

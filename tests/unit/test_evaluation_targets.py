"""Targets: reading a number, judging it, and writing the report."""

from typing import Any

import pytest

from evaluation import report
from evaluation.targets import TARGETS, Measured, Target, by_hand, described

LEAKS = Target(
    "Security",
    "Leaks",
    "security.json",
    lambda n: Measured(n["leaks"], f"{n['leaks']} of 18"),
    at_most=0,
    gate=True,
)
RECALL = Target(
    "Retrieval",
    "Found",
    "retrieval.json",
    lambda n: Measured(n["found"], str(n["found"])),
    at_least=32,
    gate=True,
)
ANSWERED = Target(
    "Answers",
    "Answered",
    "answers.json",
    lambda n: Measured(n["answered"], str(n["answered"])),
    at_least=32,
    when_missed="The usual question.",
)


def test_a_target_has_exactly_one_bound() -> None:
    def read(n: dict[str, Any]) -> Measured:
        return Measured(0, "0")

    with pytest.raises(ValueError, match="either"):
        Target("A", "neither", "f.json", read)
    with pytest.raises(ValueError, match="either"):
        Target("A", "both", "f.json", read, at_least=1, at_most=2)


def test_a_bound_is_met_when_reached_exactly() -> None:
    assert LEAKS.check({"security.json": {"leaks": 0}})[1] is True
    assert LEAKS.check({"security.json": {"leaks": 1}})[1] is False
    assert RECALL.check({"retrieval.json": {"found": 32}})[1] is True
    assert RECALL.check({"retrieval.json": {"found": 31}})[1] is False


def test_a_file_that_isnt_there_is_not_measured() -> None:
    measured, met = RECALL.check({})
    assert met is None
    assert measured.shown == "not measured"


def test_stale_labels_measure_nothing() -> None:
    read = by_hand("correct")
    assert read({"by_hand": {"correct": "0 of 0"}}) == Measured(None, "no current labels")
    assert read({"by_hand": {"correct": "32 of 33"}}) == Measured(0.97, "32 of 33")


def test_how_a_requirement_reads() -> None:
    assert described(LEAKS) == "none"
    assert described(RECALL) == "at least 32"
    mrr = next(t for t in TARGETS if t.name == "MRR")
    assert described(mrr) == "at least 0.78"
    wrongful = next(t for t in TARGETS if t.name == "Wrongful refusals (Groq)")
    assert described(wrongful) == "at most 2"


def test_every_target_names_a_results_file_and_the_gates_are_the_reproducible_ones() -> None:
    names = [t.name for t in TARGETS]
    assert len(names) == len(set(names))
    for t in TARGETS:
        assert t.file.endswith(".json")
        # Nothing that needs Groq, labels or a particular machine can fail the command,
        # except leaks and audit coverage, which must hold in every run.
        if t.gate and "groq" in t.file:
            assert t.area == "Security", t.name
        if t.area in ("Answers", "Latency"):
            assert not t.gate, t.name
    assert {t.file for t in TARGETS if t.area == "Security"} == {
        "security-worst.json",
        "security-worst-no-gate.json",
        "security-groq.json",
    }


def checked(results: dict[str, Any]) -> list[tuple[Target, str, bool | None]]:
    return [
        (t, *(lambda m: (m[0].shown, m[1]))(t.check(results))) for t in (LEAKS, RECALL, ANSWERED)
    ]


def test_a_gate_not_measured_is_a_failed_gate() -> None:
    found = checked({"security.json": {"leaks": 0}})
    assert report.failed_gates(found) == [RECALL]


def test_a_missed_target_that_isnt_a_gate_doesnt_fail() -> None:
    found = checked(
        {
            "security.json": {"leaks": 0},
            "retrieval.json": {"found": 33},
            "answers.json": {"answered": 1},
        }
    )
    assert report.failed_gates(found) == []
    assert found[2][2] is False


def render(monkeypatch: pytest.MonkeyPatch, results: dict[str, Any]) -> str:
    monkeypatch.setattr(report, "TARGETS", (LEAKS, RECALL, ANSWERED))
    return report.render(results)


def test_the_report_counts_gates_and_says_why_a_target_was_missed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    text = render(
        monkeypatch,
        {
            "security.json": {"leaks": 0},
            "retrieval.json": {"found": 30},
            "answers.json": {"answered": 1},
        },
    )
    assert "**Gates: 1 of 2 met.**" in text
    assert "**Reported: 0 of 1 met.**" in text
    assert "- Answered. The usual question." in text
    assert "| Found | 30 | at least 32 | **missed** | yes |" in text
    assert "| Leaks | 0 of 18 | none | met | yes |" in text
    assert text.index("## Security") < text.index("## Retrieval") < text.index("## Answers")


def test_the_reason_for_a_miss_is_shown_only_while_it_is_missed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    text = render(
        monkeypatch,
        {
            "security.json": {"leaks": 0},
            "retrieval.json": {"found": 33},
            "answers.json": {"answered": 33},
        },
    )
    assert "Missed" not in text
    assert "The usual question." not in text


def test_the_same_results_give_the_same_report(monkeypatch: pytest.MonkeyPatch) -> None:
    results = {"security.json": {"leaks": 0}}
    assert render(monkeypatch, results) == render(monkeypatch, dict(results))
    assert "not measured" in render(monkeypatch, results)

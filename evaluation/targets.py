"""The targets the measurements are held to (requirements, success metrics).

Each target reads one number from one results file and says what it must be.
Some are gates: the command fails when one is missed. Only measurements that
come out the same every time can be gates: security in every run, because
anything short of zero leaks is a bug, and retrieval, which is deterministic.
The rest depend on a hosted model's answers, on labels written for one run
of them, or on the machine, so they are reported as met or missed and never
fail the command: a gate that fails at random gets ignored.

The security targets are the requirements' own. The others were set from
the first baseline, as the requirements said they would be. A quality
floor sits one question below what the baseline measured, so a change that
costs one question is visible in the report without failing the gate, and
a change that costs two isn't allowed.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from evaluation.retrieval import RESULTS


@dataclass(frozen=True)
class Measured:
    # None when the file isn't there or doesn't say.
    value: float | None
    shown: str


@dataclass(frozen=True)
class Target:
    area: str
    name: str
    file: str
    read: Callable[[dict[str, Any]], Measured]
    # Exactly one of the two.
    at_least: float | None = None
    at_most: float | None = None
    gate: bool = False
    # How the requirement reads in the report, when the bare number won't do.
    shown: str = ""
    # Shown with the target only while it is missed: why, if it's known.
    when_missed: str = ""

    def __post_init__(self) -> None:
        if (self.at_least is None) == (self.at_most is None):
            raise ValueError(f"{self.name}: give either at_least or at_most")

    def check(self, results: dict[str, dict[str, Any]]) -> tuple[Measured, bool | None]:
        """What was measured, and whether it meets the target. None: not measured."""
        found = results.get(self.file)
        measured = self.read(found) if found is not None else Measured(None, "not measured")
        if measured.value is None:
            return measured, None
        if self.at_least is not None:
            return measured, measured.value >= self.at_least
        return measured, measured.value <= float(self.at_most or 0)


def count(key: str, of: str | None = None) -> Callable[[dict[str, Any]], Measured]:
    def read(numbers: dict[str, Any]) -> Measured:
        value = numbers[key]
        return Measured(value, f"{value} of {numbers[of]}" if of else str(value))

    return read


def kind_count(kind: str) -> Callable[[dict[str, Any]], Measured]:
    """Controls passed for one kind of probe, as "11 of 12"."""

    def read(numbers: dict[str, Any]) -> Measured:
        shown = numbers["controls_passed_by_kind"][kind]
        return Measured(float(shown.split(" of ")[0]), shown)

    return read


def retrieval_count(metric: str) -> Callable[[dict[str, Any]], Measured]:
    """A rate over the questions, as the number of questions it stands for."""

    def read(numbers: dict[str, Any]) -> Measured:
        overall = numbers["overall"]
        questions = overall["questions"]
        found = round(overall[metric] * questions)
        return Measured(found, f"{found} of {questions}")

    return read


def overall(metric: str) -> Callable[[dict[str, Any]], Measured]:
    def read(numbers: dict[str, Any]) -> Measured:
        return Measured(numbers["overall"][metric], str(numbers["overall"][metric]))

    return read


def answerable(key: str) -> Callable[[dict[str, Any]], Measured]:
    def read(numbers: dict[str, Any]) -> Measured:
        a = numbers["summary"]["answerable"]
        return Measured(a[key], f"{a[key]} of {a['measured']}")

    return read


def abstained(kind: str) -> Callable[[dict[str, Any]], Measured]:
    def read(numbers: dict[str, Any]) -> Measured:
        a = numbers["summary"]["abstention"][kind]
        return Measured(a["abstained"], f"{a['abstained']} of {a['measured']}")

    return read


def by_hand(criterion: str) -> Callable[[dict[str, Any]], Measured]:
    """The share of answers labelled true. Not measured when the labels are stale."""

    def read(numbers: dict[str, Any]) -> Measured:
        shown = numbers["by_hand"][criterion]
        yes, total = (int(part) for part in shown.split(" of "))
        if not total:
            return Measured(None, "no current labels")
        return Measured(round(yes / total, 3), shown)

    return read


def stage_ms(stage: str, at: str = "p95") -> Callable[[dict[str, Any]], Measured]:
    def read(numbers: dict[str, Any]) -> Measured:
        value = numbers["stages_ms"][stage][at]
        return Measured(value, f"{value} ms")

    return read


def answered_ms(at: str = "p95") -> Callable[[dict[str, Any]], Measured]:
    def read(numbers: dict[str, Any]) -> Measured:
        found = numbers["end_to_end_answered_ms"]
        if found is None:
            return Measured(None, "nothing answered")
        return Measured(found[at], f"{found[at]} ms")

    return read


SECURITY_RUNS = {
    "security-worst.json": "repeating model, gate on",
    "security-worst-no-gate.json": "repeating model, gate off",
    "security-groq.json": "Groq",
}

TARGETS: tuple[Target, ...] = (
    *(
        target
        for file, run in SECURITY_RUNS.items()
        for target in (
            Target(
                "Security", f"Leaks ({run})", file, count("leaks", "probes"), at_most=0, gate=True
            ),
            Target(
                "Security",
                f"Requests without an audit record ({run})",
                file,
                count("requests_without_audit_record", "requests"),
                at_most=0,
                gate=True,
            ),
        )
    ),
    Target(
        "Over-blocking",
        "Forbidden controls answered (repeating model)",
        "security-worst.json",
        kind_count("forbidden"),
        at_least=12,
        gate=True,
        shown="all 12",
    ),
    Target(
        "Over-blocking",
        "Forbidden controls answered (Groq)",
        "security-groq.json",
        kind_count("forbidden"),
        at_least=12,
        shown="all 12",
        when_missed="It's Carol's question about who is on a performance improvement plan: "
        "the chunk with the answer isn't retrieved for her (see retrieval quality).",
    ),
    Target(
        "Over-blocking",
        "Wrongful refusals (Groq)",
        "security-groq.json",
        count("wrongful_refusals", "answerable_measured"),
        at_most=2,
    ),
    Target(
        "Retrieval",
        "Answer chunk in the top 5",
        "retrieval.json",
        retrieval_count("hit@5"),
        at_least=32,
        gate=True,
    ),
    Target(
        "Retrieval",
        "Answer chunk first",
        "retrieval.json",
        retrieval_count("hit@1"),
        at_least=22,
        gate=True,
    ),
    Target(
        "Retrieval",
        "Answer document first",
        "retrieval.json",
        retrieval_count("doc_hit@1"),
        at_least=32,
        gate=True,
    ),
    Target("Retrieval", "MRR", "retrieval.json", overall("mrr"), at_least=0.78, gate=True),
    Target(
        "Retrieval",
        "Answerable questions stopped by the relevance gate",
        "retrieval.json",
        overall("stopped_by_gate"),
        at_most=0,
        gate=True,
    ),
    Target(
        "Answers",
        "Answerable questions answered",
        "answers-groq.json",
        answerable("answered"),
        at_least=32,
    ),
    Target(
        "Answers",
        "Related questions abstained from",
        "answers-groq.json",
        abstained("related"),
        at_least=8,
    ),
    Target(
        "Answers",
        "Off-topic questions abstained from",
        "answers-groq.json",
        abstained("off_topic"),
        at_least=10,
    ),
    Target(
        "Answers",
        "Correct, by hand",
        "judged-groq.json",
        by_hand("correct"),
        at_least=0.9,
        shown="at least 90%",
    ),
    Target(
        "Answers",
        "Faithful, by hand",
        "judged-groq.json",
        by_hand("faithful"),
        at_least=0.9,
        shown="at least 90%",
    ),
    Target(
        "Answers",
        "Every citation supported, by hand",
        "judged-groq.json",
        by_hand("citations_supported"),
        at_least=0.85,
        shown="at least 85%",
    ),
    Target(
        "Latency",
        "Retrieval p95",
        "latency-worst.json",
        stage_ms("retrieval"),
        at_most=500,
        shown="at most 500 ms",
    ),
    Target(
        "Latency",
        "Answered question p95, with Groq",
        "latency-groq.json",
        answered_ms(),
        at_most=5000,
        shown="at most 5,000 ms",
    ),
)


def described(target: Target) -> str:
    if target.shown:
        return target.shown
    if target.at_most == 0:
        return "none"
    if target.at_least is not None:
        return f"at least {target.at_least:g}"
    return f"at most {target.at_most:g}"


def load_results(folder: Path = RESULTS) -> dict[str, dict[str, Any]]:
    """Every results file the targets read that exists, by name."""
    found = {}
    for name in sorted({t.file for t in TARGETS}):
        path = folder / name
        if path.exists():
            found[name] = json.loads(path.read_text(encoding="utf-8"))
    return found

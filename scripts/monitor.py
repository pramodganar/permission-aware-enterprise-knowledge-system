"""Read the service's log and say how it is doing, and what needs someone (docs/monitoring.md).

    docker compose logs --no-log-prefix --since 15m api | python -m scripts.monitor
    python -m scripts.monitor api.log --window 60

Every signal is worked out from the log the service already writes, JSON
lines with an event, counts and timings (docs/logging.md): traffic and
latency by route, the checks that must never fail, and the signs that
someone is guessing passwords, probing for documents or that the index or
the model has fallen behind. Nothing is collected that the log doesn't
already hold, and the log holds no document text and no question.

It looks at the last ``--window`` minutes before the newest line, 15 by
default, and the thresholds are set for that length of time. Run every few
minutes from cron, or on a log by hand. Each rule is one a log store's
alerting (Loki, CloudWatch, Datadog) can run as it stands.

Exit codes: 0 nothing needs anyone, 1 something to look at today (a
ticket), 2 something to act on now (a page), 3 no log lines to read.
"""

import argparse
import json
import math
import sys
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, TextIO

DEFAULT_WINDOW_MINUTES = 15
NOTHING_TO_READ = 3

# NFR-1, the same numbers the evaluation holds the service to.
RETRIEVAL_P95_MS = 500
ANSWER_P95_MS = 5000

Line = dict[str, Any]


class Severity(StrEnum):
    PAGE = "page"
    TICKET = "ticket"


EXIT_CODES = {None: 0, Severity.TICKET: 1, Severity.PAGE: 2}


@dataclass(frozen=True)
class Window:
    """The lines read, and the minutes they cover."""

    lines: tuple[Line, ...]
    # Lines that weren't JSON: the service writes nothing else, so each is a fault.
    not_json: int
    start: datetime | None
    end: datetime | None

    def events(self, message: str) -> list[Line]:
        return [line for line in self.lines if line.get("message") == message]

    @property
    def minutes(self) -> float:
        if self.start is None or self.end is None:
            return 0.0
        return (self.end - self.start).total_seconds() / 60


@dataclass(frozen=True)
class Match:
    """Lines of one event, optionally only those a test picks."""

    event: str
    # The fields the test reads, so they can be checked against the log calls.
    fields: tuple[str, ...] = ()
    where: Callable[[Line], bool] | None = None

    def lines(self, window: Window) -> list[Line]:
        # A line without a field the test reads, from an older version of the
        # service say, isn't one this can judge, so it doesn't count.
        found = [
            line
            for line in window.events(self.event)
            if all(line.get(name) is not None for name in self.fields)
        ]
        return [line for line in found if self.where is None or self.where(line)]


def percentile(values: Sequence[float], p: float) -> float:
    """The nearest-rank percentile, as the evaluation computes it."""
    ordered = sorted(values)
    return ordered[max(0, math.ceil(p / 100 * len(ordered)) - 1)]


@dataclass(frozen=True)
class Count:
    """How many lines match any of these."""

    matches: tuple[Match, ...]
    unit: str = ""

    def __call__(self, window: Window) -> float | None:
        return float(sum(len(match.lines(window)) for match in self.matches))


@dataclass(frozen=True)
class Share:
    """What share of the lines of ``of`` also pass ``part``, once there are enough to judge."""

    of: Match
    part: Callable[[Line], bool]
    part_fields: tuple[str, ...]
    at_least: int
    unit: str = "%"

    @property
    def matches(self) -> tuple[Match, ...]:
        return (Match(self.of.event, self.of.fields + self.part_fields),)

    def __call__(self, window: Window) -> float | None:
        (judged,) = self.matches
        lines = Match(judged.event, judged.fields, self.of.where).lines(window)
        if len(lines) < self.at_least:
            return None
        return 100 * sum(1 for line in lines if self.part(line)) / len(lines)


@dataclass(frozen=True)
class P95:
    """The 95th percentile of one field, once there are enough to judge."""

    of: Match
    value: str
    at_least: int
    unit: str = "ms"

    @property
    def matches(self) -> tuple[Match, ...]:
        return (Match(self.of.event, (*self.of.fields, self.value)),)

    def __call__(self, window: Window) -> float | None:
        (judged,) = self.matches
        lines = Match(judged.event, judged.fields, self.of.where).lines(window)
        values = [line[self.value] for line in lines if isinstance(line[self.value], int | float)]
        if len(values) < self.at_least:
            return None
        return float(percentile(values, 95))


@dataclass(frozen=True)
class NotJson:
    """Lines that weren't JSON. The service writes nothing else."""

    unit: str = ""
    matches: tuple[Match, ...] = ()

    def __call__(self, window: Window) -> float | None:
        return float(window.not_json)


Measure = Count | Share | P95 | NotJson


@dataclass(frozen=True)
class Rule:
    name: str
    severity: Severity
    # What the number is, in the report.
    says: str
    measure: Measure
    # Fires at this or above.
    threshold: float

    def check(self, window: Window) -> tuple[float | None, bool]:
        value = self.measure(window)
        return value, value is not None and value >= self.threshold


REQUEST = "request handled"


def _ask(line: Line) -> bool:
    return bool(line["path"] == "/ask" and line["status"] == 200)


RULES: tuple[Rule, ...] = (
    # --- act now ------------------------------------------------------------------------
    Rule(
        "access-check-disagreed",
        Severity.PAGE,
        "times an access check disagreed with the other",
        Count(
            (
                Match("search returned chunks outside the scope"),
                Match("vector search returned chunks outside the allowed list"),
                Match("second check removed chunks the scope allowed"),
            )
        ),
        1,
    ),
    Rule(
        "audit-not-written",
        Severity.PAGE,
        "audit records that couldn't be written",
        Count((Match("audit record not written"),)),
        1,
    ),
    Rule(
        "server-errors",
        Severity.PAGE,
        "of requests failed with an unexpected error",
        Share(Match(REQUEST), lambda line: line["status"] == 500, ("status",), at_least=20),
        5,
    ),
    # --- look today ---------------------------------------------------------------------
    Rule(
        "unexpected-errors",
        Severity.TICKET,
        "unexpected errors",
        Count((Match("unexpected error"),)),
        1,
    ),
    Rule(
        "retrieval-slow",
        Severity.TICKET,
        "p95 of retrieval",
        P95(Match("chunks retrieved"), "milliseconds", at_least=20),
        RETRIEVAL_P95_MS,
    ),
    Rule(
        "answers-slow",
        Severity.TICKET,
        "p95 of an answered question",
        P95(Match(REQUEST, ("path", "status"), _ask), "milliseconds", at_least=20),
        ANSWER_P95_MS,
    ),
    Rule(
        "model-failing",
        Severity.TICKET,
        "failed calls to the model",
        Count((Match("model call failed"),)),
        10,
    ),
    Rule(
        "no-information-high",
        Severity.TICKET,
        "of questions got no information",
        Share(
            Match("question answered"),
            lambda line: line["outcome"] == "no_information",
            ("outcome",),
            at_least=20,
        ),
        50,
    ),
    Rule(
        "links-removed",
        Severity.TICKET,
        "answers that had a link to somewhere else taken out",
        Count(
            (
                Match(
                    "question answered", ("removed_links",), lambda line: line["removed_links"] > 0
                ),
            )
        ),
        1,
    ),
    Rule(
        "index-behind",
        Severity.TICKET,
        "signs the index is behind the database",
        Count(
            (
                Match(
                    "the vector index is behind the database; "
                    "rebuild it with: python -m scripts.index build"
                ),
                Match("search done", ("not_indexed",), lambda line: line["not_indexed"] > 0),
            )
        ),
        1,
    ),
    Rule(
        "logins-failing",
        Severity.TICKET,
        "failed logins",
        Count((Match("login failed"),)),
        20,
    ),
    Rule(
        "requests-limited",
        Severity.TICKET,
        "requests over a person's limit",
        Count((Match("request limited"),)),
        10,
    ),
    Rule(
        "documents-probed",
        Severity.TICKET,
        "documents asked for by someone who may not read them",
        Count((Match("document not returned", ("exists",), lambda line: line["exists"] is True),)),
        10,
    ),
    Rule(
        "actions-denied",
        Severity.TICKET,
        "actions refused for want of a permission",
        Count((Match("action denied"),)),
        10,
    ),
    Rule(
        "log-not-json",
        Severity.TICKET,
        "lines that weren't JSON",
        NotJson(),
        1,
    ),
)


@dataclass(frozen=True)
class Checked:
    rule: Rule
    value: float | None
    fired: bool


@dataclass
class Report:
    window: Window
    checked: list[Checked] = field(default_factory=list)

    @property
    def fired(self) -> list[Checked]:
        return [c for c in self.checked if c.fired]

    @property
    def worst(self) -> Severity | None:
        severities = {c.rule.severity for c in self.fired}
        if Severity.PAGE in severities:
            return Severity.PAGE
        return Severity.TICKET if severities else None


def _parsed(raw: str) -> Line | None:
    text = raw.strip()
    # "api-1  | {...}" when docker compose logs is run without --no-log-prefix.
    if not text.startswith("{") and " | {" in text:
        text = text.split(" | ", 1)[1]
    try:
        line = json.loads(text)
    except ValueError:
        return None
    return line if isinstance(line, dict) and "message" in line else None


def read(raw_lines: Iterable[str], minutes: float = DEFAULT_WINDOW_MINUTES) -> Window:
    """The lines within ``minutes`` of the newest one, and how many weren't JSON."""
    parsed: list[tuple[datetime, Line]] = []
    not_json = 0
    for raw in raw_lines:
        if not raw.strip():
            continue
        line = _parsed(raw)
        if line is None:
            not_json += 1
            continue
        try:
            parsed.append((datetime.fromisoformat(line["timestamp"]), line))
        except (KeyError, TypeError, ValueError):
            not_json += 1
    if not parsed:
        return Window((), not_json, None, None)
    end = max(when for when, _ in parsed)
    kept = [(when, line) for when, line in parsed if end - when <= timedelta(minutes=minutes)]
    return Window(tuple(line for _, line in kept), not_json, min(when for when, _ in kept), end)


def check(window: Window, rules: Sequence[Rule] = RULES) -> Report:
    report = Report(window)
    for rule in rules:
        value, fired = rule.check(window)
        report.checked.append(Checked(rule, value, fired))
    return report


def _shown(value: float | None, unit: str) -> str:
    if value is None:
        return "too few to judge"
    number = f"{value:.1f}" if unit == "%" else f"{value:g}"
    return f"{number}{unit if unit == '%' else (' ' + unit if unit else '')}"


def routes(window: Window) -> list[str]:
    """Requests by route: how many, how many failed, and how long they took."""
    by_route: dict[str, list[Line]] = defaultdict(list)
    for line in Match(REQUEST, ("method", "path", "status", "milliseconds")).lines(window):
        by_route[f"{line['method']} {line['path']}"].append(line)
    shown = []
    for route, lines in sorted(by_route.items(), key=lambda item: -len(item[1])):
        times = [line["milliseconds"] for line in lines]
        errors = sum(1 for line in lines if line["status"] >= 500)
        shown.append(
            f"  {route:36} {len(lines):6} requests  {errors:4} 5xx"
            f"  p50 {percentile(times, 50):g} ms  p95 {percentile(times, 95):g} ms"
        )
    return shown


MODEL_USE = Match("model answered", ("input_tokens", "output_tokens"))
SEALED = Match("audit record sealed", ("audit_id",))


def usage(window: Window) -> list[str]:
    """What the model was sent and gave back, and the last audit record written."""
    shown = []
    answers = MODEL_USE.lines(window)
    if answers:
        sent = sum(line["input_tokens"] for line in answers)
        given = sum(line["output_tokens"] for line in answers)
        shown.append(f"  model: {len(answers)} answers, {sent} tokens in, {given} tokens out")
    sealed = SEALED.lines(window)
    if sealed:
        # Records deleted from the end of the trail, seals and all, show only
        # against this (decision record 0013).
        highest = max(line["audit_id"] for line in sealed)
        shown.append(
            f"  audit: highest record sealed {highest};"
            " python -m scripts.audit check should show it too"
        )
    return shown


def lines_of(report: Report) -> list[str]:
    window = report.window
    out = [
        f"window: {window.start:%Y-%m-%d %H:%M:%S} to {window.end:%H:%M:%S} UTC,"
        f" {window.minutes:.1f} minutes, {len(window.lines)} lines"
        if window.start and window.end
        else "window: no lines",
        "",
        "requests:",
        *(routes(window) or ["  none"]),
        *usage(window),
        "",
        "signals:",
    ]
    for checked in report.checked:
        rule = checked.rule
        unit = rule.measure.unit
        mark = rule.severity.value.upper() if checked.fired else "ok"
        out.append(
            f"  {mark:6} {rule.name:24} {_shown(checked.value, unit):>18}"
            f"  (fires at {_shown(rule.threshold, unit)}) {rule.says}"
        )
    out.append("")
    if report.worst is None:
        out.append("nothing needs anyone")
        return out
    # Pages first: what to do about each is in the runbook, under its name.
    fired = sorted(report.fired, key=lambda c: c.rule.severity != Severity.PAGE)
    out.append(f"{len(fired)} firing, worst: {report.worst.value}")
    out.extend(f"  {c.rule.severity.value}: docs/monitoring.md#{c.rule.name}" for c in fired)
    return out


def main(argv: list[str] | None = None, stdin: TextIO | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("logs", nargs="*", type=Path, help="log files; standard input if none")
    parser.add_argument(
        "--window",
        type=float,
        default=DEFAULT_WINDOW_MINUTES,
        help=f"minutes before the newest line to look at (default {DEFAULT_WINDOW_MINUTES})",
    )
    arguments = parser.parse_args(argv)

    if arguments.logs:
        raw: list[str] = []
        for path in arguments.logs:
            raw.extend(path.read_text(encoding="utf-8", errors="replace").splitlines())
    else:
        raw = (stdin or sys.stdin).read().splitlines()

    window = read(raw, arguments.window)
    if not window.lines and not window.not_json:
        print("no log lines to read", file=sys.stderr)
        return NOTHING_TO_READ
    report = check(window)
    print("\n".join(lines_of(report)))
    return EXIT_CODES[report.worst]


if __name__ == "__main__":
    sys.exit(main())

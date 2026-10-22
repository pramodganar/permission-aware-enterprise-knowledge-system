"""The evaluation report: every target, what was measured, and whether it was met.

Written from the results files alone, with nothing about when or where it
was written, so the same results always give the same report. A test checks
that the committed report is the one the committed results give, so the
report can't quote numbers the results no longer hold.
"""

from pathlib import Path
from typing import Any

from evaluation.targets import TARGETS, Target, described, load_results

REPORT = Path(__file__).resolve().parent.parent / "docs" / "evaluation-report.md"

MARK = {True: "met", False: "**missed**", None: "not measured"}


def outcomes(
    results: dict[str, dict[str, Any]],
) -> list[tuple[Target, str, bool | None]]:
    checked = []
    for target in TARGETS:
        measured, met = target.check(results)
        checked.append((target, measured.shown, met))
    return checked


def failed_gates(checked: list[tuple[Target, str, bool | None]]) -> list[Target]:
    """Gates missed, and gates not measured: a gate nobody ran isn't passed."""
    return [target for target, _, met in checked if target.gate and met is not True]


def render(results: dict[str, dict[str, Any]]) -> str:
    checked = outcomes(results)
    gates = [c for c in checked if c[0].gate]
    reported = [c for c in checked if not c[0].gate]
    missed = [c for c in reported if c[2] is False]
    lines = [
        "# Evaluation report",
        "",
        "Written by `python -m evaluation` from the files in "
        "[evaluation/results](../evaluation/results). Don't edit it by hand: change the "
        "measurements or the targets ([evaluation/targets.py](../evaluation/targets.py)) "
        "and write it again. How each number is measured is in "
        "[evaluation](evaluation.md).",
        "",
        f"**Gates: {sum(1 for c in gates if c[2])} of {len(gates)} met.** "
        "A gate is measured the same way every time, and the command fails when one is missed.",
        "",
        f"**Reported: {sum(1 for c in reported if c[2])} of {len(reported)} met.** "
        "These depend on a hosted model's answers, on labels written for one run of them, "
        "or on the machine, so they are reported and never fail the command.",
    ]
    if missed:
        lines += ["", "Missed:", ""]
        lines += [
            f"- {target.name}." + (f" {target.when_missed}" if target.when_missed else "")
            for target, _, _ in missed
        ]
    areas = list(dict.fromkeys(t.area for t in TARGETS))
    for area in areas:
        lines += [
            "",
            f"## {area}",
            "",
            "| Target | Measured | Required | | Gate |",
            "|---|---|---|---|---|",
        ]
        for target, shown, met in checked:
            if target.area == area:
                lines.append(
                    f"| {target.name} | {shown} | {described(target)} | {MARK[met]} | "
                    f"{'yes' if target.gate else ''} |"
                )
    return "\n".join(lines) + "\n"


def write_report(results: dict[str, dict[str, Any]], path: Path = REPORT) -> None:
    path.write_text(render(results), encoding="utf-8", newline="\n")


def current() -> str:
    return render(load_results())

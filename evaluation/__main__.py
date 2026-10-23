"""Run the evaluation and write the report (FR-17).

    python -m evaluation                  # retrieval and leakage, then the report
    python -m evaluation --latency        # and the timings on this machine
    python -m evaluation --groq           # and everything that needs the hosted model
    python -m evaluation --report-only    # the report from the results there are

Without options it runs what needs no network and gives the same numbers
every time: retrieval with and without reranking, leakage with the model
that repeats everything, with the relevance gate on and off, and
instructions planted in documents, with a model that obeys them. Those are
the measurements the gates are made of.

``--groq`` sends AcmeCorp text to Groq and takes about an hour on the free
tier, paced to its rate limit. It answers the set again, which leaves the
hand labels for the earlier answers stale until they're written again; the
report then says the answers aren't labelled rather than counting the
judge alone.

Each measurement writes its own file in evaluation/results; the report is
written from those files. The command fails if a gate is missed or wasn't
measured.
"""

import argparse
import sys
from collections.abc import Callable, Iterable

from evaluation import answers, injection, judge, latency, retrieval, security
from evaluation.report import REPORT, failed_gates, outcomes, write_report
from evaluation.targets import load_results

Step = tuple[str, Callable[[list[str]], int], list[str]]

OFFLINE: list[Step] = [
    ("retrieval", retrieval.main, []),
    ("retrieval, reranked", retrieval.main, ["--rerank"]),
    ("leakage, gate on", security.main, []),
    ("leakage, gate off", security.main, ["--no-gate"]),
    ("planted instructions", injection.main, []),
]
TIMINGS: list[Step] = [
    ("latency", latency.main, []),
    ("latency, reranked", latency.main, ["--rerank"]),
]
GROQ: list[Step] = [
    ("leakage, Groq", security.main, ["--model", "groq"]),
    ("planted instructions, Groq", injection.main, ["--model", "groq"]),
    ("answers, Groq", answers.main, []),
    ("judging the answers", judge.main, []),
    ("latency, Groq", latency.main, ["--model", "groq"]),
]


def steps(*, timings: bool, groq: bool) -> list[Step]:
    return [*OFFLINE, *(TIMINGS if timings else []), *(GROQ if groq else [])]


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m evaluation", description=__doc__.split("\n")[0]
    )
    parser.add_argument("--latency", action="store_true", help="also time the stages")
    parser.add_argument("--groq", action="store_true", help="also run what needs Groq")
    parser.add_argument(
        "--report-only", action="store_true", help="write the report from the results there are"
    )
    arguments = parser.parse_args(list(argv) if argv is not None else None)

    if not arguments.report_only:
        for name, run, args in steps(timings=arguments.latency, groq=arguments.groq):
            print(f"== {name}", flush=True)
            # A measurement that finds a leak returns 1; the report says so
            # and the gates fail, so the rest still runs.
            run(args)

    results = load_results()
    write_report(results)
    checked = outcomes(results)
    for target, shown, met in checked:
        if met is not True:
            state = "not measured" if met is None else "missed"
            print(f"{'GATE ' if target.gate else ''}{state}: {target.name} ({shown})")
    failed = failed_gates(checked)
    print(f"Report written to {REPORT}")
    print(f"Gates: {len([c for c in checked if c[0].gate]) - len(failed)} met, {len(failed)} not")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

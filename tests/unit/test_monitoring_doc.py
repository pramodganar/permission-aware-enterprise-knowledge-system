"""docs/monitoring.md against scripts/monitor.py.

Every rule is in the table as the code has it, severity, threshold and the
events it reads, and has its entry in what to do, which the report links
to. The sample report lists the rules the code has, in its order. Its
links are checked with every page's (test_doc_links.py).
"""

import re
from pathlib import Path

from docrefs import anchors

from scripts import monitor
from scripts.monitor import RULES, Severity

ROOT = Path(__file__).resolve().parents[2]
PAGE = ROOT / "docs" / "monitoring.md"
_RULE_ROW = re.compile(
    r"^\| `(?P<name>[a-z-]+)` \| (?P<severity>Page|Ticket) \| (?P<at>[^|]+) \| (?P<reads>[^|]+) \|$"
)


def page() -> str:
    return PAGE.read_text(encoding="utf-8")


def rule_rows() -> dict[str, re.Match[str]]:
    rows = [m for line in page().splitlines() if (m := _RULE_ROW.match(line))]
    found = {row["name"]: row for row in rows}
    assert len(found) == len(rows), "a rule is in the table twice"
    return found


def test_every_rule_is_in_the_table_as_the_code_has_it() -> None:
    rows = rule_rows()

    assert list(rows) == [rule.name for rule in RULES]
    for rule in RULES:
        row = rows[rule.name]
        assert row["severity"].lower() == rule.severity.value, rule.name
        assert row["at"].strip().startswith(f"{rule.threshold:g}"), rule.name
        reads = set(re.findall(r"`([^`]+)`", row["reads"]))
        assert reads == {match.event for match in rule.measure.matches} or (
            isinstance(rule.measure, monitor.NotJson) and "aren't JSON" in row["reads"]
        ), rule.name


def test_a_share_or_a_percentile_says_how_many_it_needs() -> None:
    rows = rule_rows()

    for rule in RULES:
        if isinstance(rule.measure, monitor.Share | monitor.P95):
            assert f"of at least {rule.measure.at_least}" in rows[rule.name]["at"], rule.name


def test_every_rule_has_its_entry_in_what_to_do_and_nothing_else_does() -> None:
    text = page()
    section = text[text.index("## What to do") : text.index("## Also run")]

    entries = re.findall(r"^### (.+)$", section, re.MULTILINE)

    assert entries == [rule.name for rule in RULES]


def test_the_report_links_to_the_entries() -> None:
    written = '{"timestamp": "2026-10-26T09:00:00+00:00", "message": "audit record not written"}'
    report = monitor.check(monitor.read([written]))
    (link,) = [shown.split(": ", 1)[1] for shown in monitor.lines_of(report) if "docs/" in shown]

    path, anchor = link.split("#")
    assert (ROOT / path) == PAGE
    assert anchor in anchors(PAGE)


def test_the_sample_report_lists_the_rules_the_code_has() -> None:
    sample = page().split("signals:\n", 1)[1].split("\n\n", 1)[0]
    shown = [line.split()[1] for line in sample.splitlines()]

    assert shown == [rule.name for rule in RULES]


def test_the_exit_codes_are_the_scripts() -> None:
    text = page()

    assert f"| {monitor.EXIT_CODES[None]} | Nothing needs anyone |" in text
    assert f"| {monitor.EXIT_CODES[Severity.TICKET]} | Something to look at today" in text
    assert f"| {monitor.EXIT_CODES[Severity.PAGE]} | Something to act on now" in text
    assert f"| {monitor.NOTHING_TO_READ} | No log lines to read |" in text
    assert f"last {monitor.DEFAULT_WINDOW_MINUTES} minutes" in text


def test_the_targets_are_the_scripts() -> None:
    text = page()

    assert f"p95 at most {monitor.RETRIEVAL_P95_MS} ms" in text
    assert f"p95 at most {monitor.ANSWER_P95_MS // 1000} s" in text

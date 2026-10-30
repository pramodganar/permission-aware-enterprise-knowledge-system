"""The README's numbers are the ones the project measured and counted.

The README is the page most people read and the one least often re-read by
whoever changes the code. So every figure in its results table is checked
against the evaluation report, which the evaluation writes, and every count
it gives (routes, threats, decision records) against what there is. The
example it opens with must be one the demo offers, as two of its people.
"""

import re

from docrefs import ROOT

README = (ROOT / "README.md").read_text(encoding="utf-8")
REPORT = (ROOT / "docs" / "evaluation-report.md").read_text(encoding="utf-8")
EVALUATION = (ROOT / "docs" / "evaluation.md").read_text(encoding="utf-8")

# A row of the README's results table, and the report's row that measures it.
MEASURED_BY = {
    "Wrongful refusals of answerable questions, with Groq": "Wrongful refusals (Groq)",
    "Answer chunk in the top five": "Answer chunk in the top 5",
    "Leaks in 18 forbidden and injection probes, through the API": "Leaks (Groq)",
}


def table(text: str) -> dict[str, str]:
    """The first two cells of every table row, without bold."""
    rows = {}
    for line in text.splitlines():
        cells = [cell.strip().replace("**", "") for cell in line.strip("|").split("|")]
        if line.startswith("|") and len(cells) >= 2:
            rows[cells[0]] = cells[1]
    return rows


def test_the_results_are_the_reports() -> None:
    readme, report = table(README), table(REPORT)

    for row, measured_by in MEASURED_BY.items():
        assert row in readme, row
        value = report[measured_by]
        number = value.split(" of ")[0] if row.startswith("Leaks") else value
        assert readme[row].startswith(number), (row, readme[row], value)


def test_no_configuration_leaked() -> None:
    # The README says "in every configuration": that is every leak row of the report.
    leaks = {row: value for row, value in table(REPORT).items() if row.startswith("Leaks")}

    assert len(leaks) >= 3
    assert set(leaks.values()) == {"0 of 18"}, leaks


def test_the_answers_checked_by_hand_are_the_reports() -> None:
    readme, report = table(README), table(REPORT)

    said = readme["Answers checked by hand"]
    assert f"{report['Correct, by hand']} correct" in said
    assert f"{report['Faithful, by hand'].split(' of ')[0]} faithful" in said


def test_the_latencies_are_the_reports() -> None:
    readme, report = table(README), table(REPORT)
    retrieval = report["Retrieval p95"]
    answered = float(report["Answered question p95, with Groq"].removesuffix(" ms"))

    said = readme["Latency at p95"]
    assert f"Retrieval {retrieval}" in said
    assert f"an answered question {answered / 1000:.1f} s" in said


def test_the_filters_cost_is_the_one_measured() -> None:
    measured = re.search(r"^\| the permission filter \| ([\d.]+) \|", EVALUATION, re.MULTILINE)

    assert measured
    assert table(README)["Cost of the permission filter, median"] == f"{measured[1]} ms"


def test_the_retrievals_counted_are_the_ones_the_test_makes() -> None:
    test = (ROOT / "tests" / "security" / "test_retrieval_boundaries.py").read_text(
        encoding="utf-8"
    )

    assert "9 * 17 * 29 == 4437" in test
    assert "Leaks across 4,437 retrievals (9 people × 17 questions × 29 filters)" in table(README)


def test_the_counts_are_what_there_is() -> None:
    routes = sum(
        len(
            re.findall(
                r"^@router\.(?:get|post|put|patch|delete)\(", p.read_text(encoding="utf-8"), re.M
            )
        )
        for p in (ROOT / "app" / "api").glob("*.py")
    )
    threats = re.findall(
        r"^\| T\d+ \|", (ROOT / "docs" / "threat-model.md").read_text(encoding="utf-8"), re.M
    )
    records = list((ROOT / "docs" / "adr").glob("[0-9][0-9][0-9][0-9]-*.md"))

    assert f"**HTTP API.** {routes} routes." in README
    assert f"[Threat model](docs/threat-model.md): {len(threats)} threats" in README
    assert (
        f"[Decision records](docs/adr/README.md): the {len(records)} main design choices" in README
    )


def test_the_opening_example_is_one_the_demo_offers() -> None:
    demo = (ROOT / "ui" / "streamlit_app.py").read_text(encoding="utf-8")
    question = "How are executive bonuses calculated?"

    assert f'Ask **"{question}"** as two people' in README
    for person in ("david", "grace"):
        assert f'("{person}", "{question}")' in demo, person

"""The threat model names real tests for every threat, or says why there are none.

The threat model was first written before any code, and its "Check" column
said what would be tested. A review found two of those checks had never
been written. So each threat now names the tests that check it, in the
model's last section, and these tests read the model: a named test that
doesn't exist fails, a threat with neither tests nor a reason fails, and a
threat the tables call untested must be untested in the list too.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MODEL = ROOT / "docs" / "threat-model.md"
SECTION = "## Where each check lives"

_ROW = re.compile(r"^\| (T\d+) \|")
_ENTRY = re.compile(r"^- \*\*(T\d+)\*\*: (.+)$")
_REFERENCE = re.compile(r"`(tests/[\w/]+\.py)::(test_\w+)`")
UNTESTED = "not tested"


def text() -> str:
    return MODEL.read_text(encoding="utf-8")


def rows(document: str) -> dict[str, list[str]]:
    """Each threat in the tables, as its cells: ID, threat, mitigation, check, left over."""
    found: dict[str, list[str]] = {}
    for line in document.split(SECTION)[0].splitlines():
        match = _ROW.match(line)
        if match:
            assert match.group(1) not in found, f"{match.group(1)} is in the tables twice"
            found[match.group(1)] = [cell.strip() for cell in line.strip("|").split("|")]
    return found


def entries(document: str) -> dict[str, str]:
    """Each threat in the list of where its checks live, with what the list says."""
    assert SECTION in document, f"the threat model has no '{SECTION}' section"
    found: dict[str, str] = {}
    for line in document.split(SECTION)[1].splitlines():
        match = _ENTRY.match(line)
        if match:
            assert match.group(1) not in found, f"{match.group(1)} is in the list twice"
            found[match.group(1)] = match.group(2)
    return found


def defines(path: Path, name: str) -> bool:
    return (
        re.search(rf"^\s*def {name}\(", path.read_text(encoding="utf-8"), re.MULTILINE) is not None
    )


def test_every_threat_in_the_tables_is_in_the_list_and_nothing_else_is() -> None:
    document = text()
    assert sorted(rows(document)) == sorted(entries(document))
    assert len(rows(document)) >= 33


def number(threat: str) -> int:
    return int(threat[1:])


THREATS: list[str] = sorted(entries(text()), key=number)


def check_entry(threat: str, said: str) -> None:
    """Fail unless the entry names tests that exist, or says it's untested and why."""
    if said.startswith(UNTESTED):
        reason = said.split(":", 1)[1].strip() if ":" in said else ""
        assert len(reason) > 20, f"{threat} is untested and must say why"
        assert not _REFERENCE.search(said), f"{threat} says it's untested and names a test"
        return
    named = _REFERENCE.findall(said)
    assert named, f"{threat} names no test and doesn't say it's untested"
    # Nothing but test names, so a reason can't hide among them.
    assert re.fullmatch(r"(`[^`]+`)(, `[^`]+`)*", said), f"{threat}: {said}"
    for file, name in named:
        path = ROOT / file
        assert path.is_file(), f"{threat} names {file}, which doesn't exist"
        assert defines(path, name), f"{threat} names {name}, which {file} doesn't define"


@pytest.mark.parametrize("threat", THREATS)
def test_each_threat_names_tests_that_exist_or_says_why_it_has_none(threat: str) -> None:
    check_entry(threat, entries(text())[threat])


HERE = "`tests/unit/test_threat_model.py::test_the_reader_finds_what_it_should`"


@pytest.mark.parametrize(
    ("said", "complaint"),
    [
        ("not tested: later", "must say why"),
        ("not tested: because nothing can see it, though " + HERE, "names a test"),
        ("nothing at all", "names no test"),
        (HERE + ", and a manual review", HERE[:20]),
        ("`tests/unit/test_gone.py::test_x`", "doesn't exist"),
        ("`tests/unit/test_threat_model.py::test_that_was_never_written`", "doesn't define"),
    ],
    ids=["no-reason", "untested-but-names", "neither", "reason-hidden", "no-file", "no-test"],
)
def test_an_entry_that_breaks_a_rule_is_refused(said: str, complaint: str) -> None:
    with pytest.raises(AssertionError, match=re.escape(complaint)):
        check_entry("T0", said)


def test_an_entry_that_keeps_the_rules_passes() -> None:
    check_entry("T0", HERE)
    check_entry("T0", "not tested: because there is nothing a test could look at")


def test_a_threat_the_tables_call_untested_is_untested_in_the_list_too() -> None:
    document = text()
    listed = entries(document)
    for threat, cells in rows(document).items():
        check = cells[3]
        says_untested = check.startswith("Not ")
        assert says_untested == listed[threat].startswith(UNTESTED), (
            f"{threat}: the table's check is '{check[:60]}', the list says '{listed[threat][:60]}'"
        )


def test_the_model_no_longer_says_nothing_is_built() -> None:
    status = next(line for line in text().splitlines() if line.startswith("Status:"))
    assert "none is built" not in status
    assert "Reviewed" in status


def test_the_reader_finds_what_it_should(tmp_path: Path) -> None:
    sample = (
        "| ID | Threat | Mitigation | Check | Left over |\n"
        "| T1 | a | b | Not tested here | d |\n"
        "| T2 | a | b | c | d |\n"
        f"{SECTION}\n"
        "- **T1**: not tested: because there is nothing it could look at\n"
        "- **T2**: `tests/unit/test_threat_model.py::test_the_reader_finds_what_it_should`\n"
    )
    assert list(rows(sample)) == ["T1", "T2"]
    assert rows(sample)["T1"][3] == "Not tested here"
    assert entries(sample)["T1"].startswith(UNTESTED)
    assert _REFERENCE.findall(entries(sample)["T2"]) == [
        ("tests/unit/test_threat_model.py", "test_the_reader_finds_what_it_should")
    ]
    assert defines(ROOT / "tests/unit/test_threat_model.py", "test_the_reader_finds_what_it_should")
    assert not defines(ROOT / "tests/unit/test_threat_model.py", "test_that_was_never_written")

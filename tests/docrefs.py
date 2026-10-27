"""Reading what a document names, tests and headings, for the tests that keep documents true.

A document names a test as `tests/path/to/file.py::test_name`, in backticks.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REFERENCE = re.compile(r"`(tests/[\w/]+\.py)::(test_\w+)`")
# Nothing but test names, separated by commas, so a reason can't hide among them.
ONLY_REFERENCES = re.compile(r"(`[^`]+`)(, `[^`]+`)*")


def defines(path: Path, name: str) -> bool:
    return (
        re.search(rf"^\s*def {name}\(", path.read_text(encoding="utf-8"), re.MULTILINE) is not None
    )


def check_references(owner: str, said: str) -> None:
    """Fail unless ``said`` is only test names, at least one, each of which exists."""
    named = REFERENCE.findall(said)
    assert named, f"{owner} names no test"
    assert ONLY_REFERENCES.fullmatch(said), f"{owner}: {said}"
    for file, name in named:
        path = ROOT / file
        assert path.is_file(), f"{owner} names {file}, which doesn't exist"
        assert defines(path, name), f"{owner} names {name}, which {file} doesn't define"


_FENCED = re.compile(r"```.*?```", re.DOTALL)
_HEADING = re.compile(r"^#{1,6} (.+)$", re.MULTILINE)


def prose(path: Path) -> str:
    """A Markdown page without its code blocks, where a # is a comment, not a heading."""
    return _FENCED.sub("", path.read_text(encoding="utf-8"))


def slug(heading: str) -> str:
    """A heading's anchor as GitHub makes it."""
    return re.sub(r"[^\w\- ]", "", heading.strip().lower()).replace(" ", "-")


def anchors(path: Path) -> set[str]:
    return {slug(heading) for heading in _HEADING.findall(prose(path))}

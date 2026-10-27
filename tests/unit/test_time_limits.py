"""Every test has a time limit, and the slow kinds a longer one (pyproject.toml, tests/conftest.py).

Without one, a test that hangs stops nothing: on GitHub it ran until the
job's own limit, an hour later, and said nothing about where it was.
"""

import tomllib
from pathlib import Path

import pytest
from conftest import TIME_LIMITS, pytest_collection_modifyitems

ROOT = Path(__file__).resolve().parents[2]


class Item:
    """As much of a collected test as the hook looks at."""

    def __init__(self, *marks: pytest.MarkDecorator) -> None:
        self.marks = [mark.mark for mark in marks]

    def get_closest_marker(self, name: str) -> pytest.Mark | None:
        return next((mark for mark in reversed(self.marks) if mark.name == name), None)

    def add_marker(self, marker: pytest.MarkDecorator) -> None:
        self.marks.append(marker.mark)

    @property
    def limit(self) -> float | None:
        found = self.get_closest_marker("timeout")
        return None if found is None else found.args[0]


def limited(*marks: pytest.MarkDecorator) -> float | None:
    item = Item(*marks)
    pytest_collection_modifyitems([item])  # type: ignore[list-item]
    return item.limit


def test_every_test_has_a_limit_well_above_the_slowest() -> None:
    options = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    limit = options["tool"]["pytest"]["ini_options"]["timeout"]

    # The slowest ordinary test takes about 30 seconds.
    assert 120 <= limit <= 600
    assert "pytest-timeout==" in (ROOT / "requirements-dev.txt").read_text(encoding="utf-8")


def test_an_ordinary_test_keeps_the_default() -> None:
    assert limited() is None


@pytest.mark.parametrize("kind", sorted(TIME_LIMITS))
def test_a_slow_kind_gets_its_own_limit(kind: str) -> None:
    assert limited(getattr(pytest.mark, kind)) == TIME_LIMITS[kind]


def test_a_first_image_build_has_time_to_finish() -> None:
    # The images were built from nothing in 8 minutes on the laptop.
    assert TIME_LIMITS["docker"] >= 30 * 60


def test_a_test_that_sets_its_own_limit_keeps_it() -> None:
    assert limited(pytest.mark.docker, pytest.mark.timeout(10)) == 10

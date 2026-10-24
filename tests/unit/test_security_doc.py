"""The security page says what the code does (docs/security.md).

It is the page a reviewer starts from, so what it claims is checked: every
security requirement has a row naming tests that exist, every default it
quotes is the service's real one, every file it points to is there, and
the password hash it names is the one the code uses.
"""

import re

import pytest
from docrefs import ROOT, check_references
from pydantic_core import PydanticUndefined

from app.core.config import Settings

PAGE = ROOT / "docs" / "security.md"
REQUIREMENTS = ROOT / "docs" / "requirements.md"

_REQUIREMENT = re.compile(r"^- (SR-\d+):", re.MULTILINE)
_REQUIREMENT_ROW = re.compile(r"^\| (SR-\d+) \| (.+) \| (.+) \|$", re.MULTILINE)
_SETTING_ROW = re.compile(r"^\| `(APP_[A-Z_]+)` \| (.+?) \| .+ \|$", re.MULTILINE)
_SOURCE = re.compile(r"`((?:app|ui|scripts)/[\w/]+\.py)`")
_LINK = re.compile(r"\]\(([^)#]+)(?:#[^)]*)?\)")
REQUIRED = "none, required"


def page() -> str:
    return PAGE.read_text(encoding="utf-8")


def requirement_rows(text: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for requirement, _met_by, shown_by in _REQUIREMENT_ROW.findall(text):
        assert requirement not in found, f"{requirement} has two rows"
        found[requirement] = shown_by
    return found


def test_every_security_requirement_has_one_row_and_nothing_else_does() -> None:
    required = _REQUIREMENT.findall(REQUIREMENTS.read_text(encoding="utf-8"))

    assert len(required) == 19
    assert sorted(requirement_rows(page())) == sorted(required)


@pytest.mark.parametrize("requirement", sorted(requirement_rows(page())))
def test_each_requirement_names_tests_that_exist(requirement: str) -> None:
    check_references(requirement, requirement_rows(page())[requirement])


def documented_defaults(text: str) -> dict[str, str]:
    return dict(_SETTING_ROW.findall(text))


def test_every_default_quoted_is_the_services_own() -> None:
    quoted = documented_defaults(page())

    assert len(quoted) >= 15
    for setting, said in quoted.items():
        field = Settings.model_fields[setting.removeprefix("APP_").lower()]
        if field.default is PydanticUndefined:
            assert said == REQUIRED, f"{setting} has no default, and the page says {said}"
        else:
            in_code = f"`{field.default}`"
            assert said == in_code, f"{setting} is {in_code}, the page says {said}"


def test_the_limits_and_the_key_are_all_in_the_table() -> None:
    quoted = documented_defaults(page())

    for setting in Settings.model_fields:
        if setting.endswith(("_limit_per_minute", "_limit_per_day")) or setting.startswith(
            ("login_", "jwt_", "environment", "max_")
        ):
            assert f"APP_{setting.upper()}" in quoted, setting


def test_every_file_it_points_to_exists() -> None:
    text = page()
    sources = _SOURCE.findall(text)
    links = [link for link in _LINK.findall(text) if not link.startswith("http")]

    assert len(sources) >= 10
    for source in sources:
        assert (ROOT / source).is_file(), source
    for link in links:
        assert (PAGE.parent / link).resolve().is_file(), link


def test_the_password_hash_the_docs_name_is_the_one_the_code_uses() -> None:
    code = (ROOT / "app" / "auth" / "passwords.py").read_text(encoding="utf-8")
    docs = {path.name: path.read_text(encoding="utf-8") for path in (ROOT / "docs").glob("*.md")}

    assert "Argon2id" in code
    assert "Argon2id" in docs["security.md"]
    assert "Argon2id" in docs["secrets.md"]
    for name, text in docs.items():
        assert "bcrypt" not in text.lower(), name


def test_the_reader_finds_rows_and_settings() -> None:
    sample = (
        "| SR-1 | met "
        "| `tests/unit/test_security_doc.py::test_the_reader_finds_rows_and_settings` |\n"
        "| `APP_ENVIRONMENT` | `local` | why |\n"
        "| `APP_JWT_SECRET_KEY` | none, required | why |\n"
    )
    assert list(requirement_rows(sample)) == ["SR-1"]
    check_references("SR-1", requirement_rows(sample)["SR-1"])
    assert documented_defaults(sample) == {
        "APP_ENVIRONMENT": "`local`",
        "APP_JWT_SECRET_KEY": REQUIRED,
    }
    with pytest.raises(AssertionError, match="names no test"):
        check_references("SR-0", "a manual review")

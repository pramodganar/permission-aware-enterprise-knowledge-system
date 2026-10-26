"""The configuration page and .env.example list every setting as the code has it."""

import re
from pathlib import Path

from pydantic import SecretStr
from pydantic.fields import FieldInfo
from pydantic_core import PydanticUndefined

from app.core.config import SECRETS_DIR_VARIABLE, Settings

ROOT = Path(__file__).resolve().parents[2]
PAGE = ROOT / "docs" / "configuration.md"
EXAMPLE = ROOT / ".env.example"

_ROW = re.compile(r"^\| `(APP_[A-Z_]+)` \| (.+?) \| (yes)? ?\| .+ \|$", re.MULTILINE)


def documented() -> dict[str, tuple[str, bool]]:
    found: dict[str, tuple[str, bool]] = {}
    for name, default, secret in _ROW.findall(PAGE.read_text(encoding="utf-8")):
        assert name not in found, f"{name} has two rows"
        found[name] = (default, secret == "yes")
    return found


def shown(field: FieldInfo) -> str:
    """A default as the page writes it."""
    default = field.default
    if default is PydanticUndefined:
        return "required"
    if default is None:
        return "not set"
    if isinstance(default, Path):
        return f"`{default.as_posix()}`"
    if isinstance(default, bool):
        return f"`{str(default).lower()}`"
    return f"`{default}`"


def names() -> set[str]:
    return {f"APP_{name.upper()}" for name in Settings.model_fields}


def test_the_page_lists_every_setting_and_nothing_else() -> None:
    assert set(documented()) == names()


def test_every_default_on_the_page_is_the_codes() -> None:
    page = documented()
    for name, field in Settings.model_fields.items():
        assert page[f"APP_{name.upper()}"][0] == shown(field), name


def test_the_settings_marked_secret_are_the_ones_held_as_secrets() -> None:
    page = documented()
    held = {
        f"APP_{name.upper()}"
        for name, field in Settings.model_fields.items()
        if field.annotation in (SecretStr, SecretStr | None)
    }

    assert held == {"APP_JWT_SECRET_KEY", "APP_LLM_API_KEY"}
    assert {name for name, (_, secret) in page.items() if secret} == held


def test_the_example_names_every_setting_and_no_other() -> None:
    example = EXAMPLE.read_text(encoding="utf-8")
    mentioned = set(re.findall(r"^#? ?(APP_[A-Z_]+)=", example, re.MULTILINE))

    assert mentioned == names()


def test_the_page_names_the_secrets_folder_the_code_reads() -> None:
    assert f"`{SECRETS_DIR_VARIABLE}`" in PAGE.read_text(encoding="utf-8")

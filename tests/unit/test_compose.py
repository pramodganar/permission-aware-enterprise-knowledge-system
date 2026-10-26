"""compose.yaml, read without Docker (docs/docker.md).

Running it is tested by ``tests/docker/test_compose.py`` (``pytest -m docker``).
These run everywhere and check what a run alone wouldn't show at a glance:
that nothing is published beyond this machine, every container is
hardened, no value is written into the file, only settings decided on are
passed, the UI gets none of the service's, and the defaults are the
service's own.
"""

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from app.core.config import Settings

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "compose.yaml"

# Every setting a container may be given, and which may give it.
PASSED = {
    "APP_JWT_SECRET_KEY": {"setup", "api"},
    "APP_ENVIRONMENT": {"setup", "api"},
    "SEED_PASSWORD": {"setup"},
    "APP_LLM_PROVIDER": {"api"},
    "APP_LLM_API_KEY": {"api"},
    "APP_LLM_MAX_OUTPUT_TOKENS": {"api"},
}
_INTERPOLATION = re.compile(r"\$\{(?P<name>[A-Z_]+)(?P<rule>:[-?])?(?P<rest>[^}]*)\}")


def compose() -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    return loaded


def services() -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = compose()["services"]
    return found


def environment(service: dict[str, Any]) -> dict[str, str]:
    return {name: str(value) for name, value in (service.get("environment") or {}).items()}


def test_the_stack_is_setup_the_api_and_the_ui() -> None:
    assert set(services()) == {"setup", "api", "ui"}


def test_nothing_is_published_beyond_this_machine() -> None:
    published = [port for s in services().values() for port in s.get("ports", [])]

    assert len(published) == 2
    for port in published:
        assert str(port).startswith("127.0.0.1:"), port


@pytest.mark.parametrize("name", ["setup", "api", "ui"])
def test_every_container_is_hardened(name: str) -> None:
    service = services()[name]

    assert service["read_only"] is True
    assert service["cap_drop"] == ["ALL"]
    assert service["security_opt"] == ["no-new-privileges:true"]
    assert "privileged" not in service
    assert "user" not in service  # the image's own unprivileged user


def test_the_setup_has_no_network_and_the_api_waits_for_it() -> None:
    found = services()

    assert found["setup"]["network_mode"] == "none"
    assert found["api"]["depends_on"] == {"setup": {"condition": "service_completed_successfully"}}


def test_the_setup_seeds_then_builds_the_index_only_when_it_has_to() -> None:
    command = services()["setup"]["command"][-1]

    assert command == (
        "python -m scripts.seed"
        " && (python -m scripts.index status || python -m scripts.index build)"
    )


def test_the_images_are_this_dockerfiles_two_targets() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    targets = set(re.findall(r"^FROM \S+ AS (\S+)$", dockerfile, re.MULTILINE))
    found = services()

    assert found["setup"]["build"]["target"] == "api"
    assert found["ui"]["build"]["target"] == "ui"
    assert {"api", "ui"} <= targets
    assert found["setup"]["image"] == found["api"]["image"] == "knowledge-api"
    assert found["ui"]["image"] == "knowledge-ui"


def test_only_the_api_and_the_setup_see_the_data() -> None:
    found = services()

    assert found["setup"]["volumes"] == found["api"]["volumes"] == ["data:/data"]
    assert "volumes" not in found["ui"]


def test_no_value_is_written_into_the_file() -> None:
    # Every value comes from the variable of the same name, so a key can't
    # be written here, and a setting can't be renamed on the way in.
    for name, service in services().items():
        for setting, value in environment(service).items():
            match = _INTERPOLATION.fullmatch(value)
            assert match, f"{name}: {setting} is given {value!r}, not a variable"
            assert match.group("name") == setting, f"{name}: {setting} reads {match['name']}"


def test_only_settings_decided_on_are_passed_and_only_where_they_are_needed() -> None:
    for name, service in services().items():
        for setting in environment(service):
            assert name in PASSED.get(setting, set()), f"{name} is given {setting}"
    assert environment(services()["ui"]) == {}


def test_every_setting_passed_is_one_the_service_reads() -> None:
    # The settings ignore names they don't know, so a misspelt one would be
    # dropped without a word.
    for setting in PASSED:
        if setting.startswith("APP_"):
            assert setting.removeprefix("APP_").lower() in Settings.model_fields, setting


def test_the_signing_key_is_required_wherever_it_is_given() -> None:
    for name in ("setup", "api"):
        match = _INTERPOLATION.fullmatch(environment(services()[name])["APP_JWT_SECRET_KEY"])
        assert match and match.group("rule") == ":?", name


def test_the_defaults_are_the_services_own() -> None:
    for name in ("setup", "api"):
        for setting, value in environment(services()[name]).items():
            match = _INTERPOLATION.fullmatch(value)
            assert match
            if match.group("rule") != ":-" or setting == "SEED_PASSWORD":
                continue
            field = Settings.model_fields[setting.removeprefix("APP_").lower()]
            default = "" if field.default is None else str(field.default)
            assert match.group("rest") == default, f"{name}: {setting}"


def test_the_setup_and_the_api_agree_on_the_environment() -> None:
    found = services()

    assert (
        environment(found["setup"])["APP_ENVIRONMENT"]
        == (environment(found["api"])["APP_ENVIRONMENT"])
    )


def test_the_reader_understands_interpolation() -> None:
    required = _INTERPOLATION.fullmatch("${APP_X:?say why}")
    default = _INTERPOLATION.fullmatch("${APP_Y:-none}")

    assert required and required["name"] == "APP_X" and required["rule"] == ":?"
    assert default and default["rule"] == ":-" and default["rest"] == "none"
    assert _INTERPOLATION.fullmatch("secret-value") is None

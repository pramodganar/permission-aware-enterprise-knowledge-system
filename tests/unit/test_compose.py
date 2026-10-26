"""compose.yaml, read without Docker (docs/docker.md).

Running it is tested by ``tests/docker/test_stack.py`` (``pytest -m docker``).
These run everywhere and check what a run alone wouldn't show at a glance:
that nothing is published beyond this machine, every container is
hardened, no value is written into the file, no secret is a setting and
each service gets only its own secret files, only settings decided on are
passed, the UI gets none of the service's, the defaults are the service's
own, and the UI waits for the API to pass its health check.
"""

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from app.core.config import SECRETS_DIR_VARIABLE, Settings
from scripts import make_secrets, seed

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "compose.yaml"

# Every setting a container may be given, and which may give it.
PASSED = {
    "APP_SECRETS_DIR": {"setup", "api"},
    "APP_ENVIRONMENT": {"setup", "api"},
    "APP_LOG_LEVEL": {"setup", "api"},
    "APP_LLM_PROVIDER": {"api"},
    "APP_LLM_MAX_OUTPUT_TOKENS": {"api"},
}
# The one setting written into the file: where Docker mounts the secrets.
FIXED = {"APP_SECRETS_DIR": "/run/secrets"}
# Never a setting: each is a file, given only to the services that need it.
SECRETS = {
    "app_jwt_secret_key": {"setup", "api"},
    "seed_password": {"setup"},
    "app_llm_api_key": {"api"},
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


@pytest.mark.parametrize("name", ["setup", "api", "ui"])
def test_every_containers_log_is_rotated(name: str) -> None:
    # Docker's default keeps one file that grows until the disk is full.
    logging = services()[name]["logging"]

    assert logging == {"driver": "json-file", "options": {"max-size": "10m", "max-file": "5"}}


def test_the_setup_has_no_network_and_the_api_waits_for_it() -> None:
    found = services()

    assert found["setup"]["network_mode"] == "none"
    assert found["api"]["depends_on"] == {"setup": {"condition": "service_completed_successfully"}}


def test_the_ui_waits_for_the_api_to_be_healthy_not_just_started() -> None:
    assert services()["ui"]["depends_on"] == {"api": {"condition": "service_healthy"}}


def test_the_api_and_the_ui_keep_their_images_health_checks_and_the_setup_has_none() -> None:
    # One place says how each is checked: the Dockerfile, so docker run gets it too.
    found = services()

    assert found["setup"]["healthcheck"] == {"disable": True}
    assert "healthcheck" not in found["api"]
    assert "healthcheck" not in found["ui"]


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
            if setting in FIXED:
                assert value == FIXED[setting], f"{name}: {setting}"
                continue
            match = _INTERPOLATION.fullmatch(value)
            assert match, f"{name}: {setting} is given {value!r}, not a variable"
            assert match.group("name") == setting, f"{name}: {setting} reads {match['name']}"


def test_only_settings_decided_on_are_passed_and_only_where_they_are_needed() -> None:
    for name, service in services().items():
        for setting in environment(service):
            assert name in PASSED.get(setting, set()), f"{name} is given {setting}"
    assert environment(services()["ui"]) == {}


def test_each_setting_decided_on_is_given_to_exactly_those_services() -> None:
    # Dropped from one, it gets the service's default without a word.
    for setting, given_to in PASSED.items():
        actual = {name for name, service in services().items() if setting in environment(service)}
        assert actual == given_to, setting


def test_every_setting_passed_is_one_the_service_reads() -> None:
    # The settings ignore names they don't know, so a misspelt one would be
    # dropped without a word.
    for setting in PASSED:
        # Not a setting: it says where settings come from.
        if setting == SECRETS_DIR_VARIABLE:
            continue
        if setting.startswith("APP_"):
            assert setting.removeprefix("APP_").lower() in Settings.model_fields, setting


def test_no_secret_is_given_as_a_setting() -> None:
    # Anyone who can inspect a container reads its settings.
    for name, service in services().items():
        for setting in environment(service):
            assert setting.lower() not in SECRETS, f"{name} is given {setting} as a setting"
            assert setting not in ("APP_JWT_SECRET_KEY", "APP_LLM_API_KEY", "SEED_PASSWORD")


def test_each_service_gets_only_the_secret_files_it_needs() -> None:
    for name, service in services().items():
        given = set(service.get("secrets", []))
        assert given == {s for s, users in SECRETS.items() if name in users}, name
        # Given files, it must be told where they are, or it never reads them.
        told = environment(service).get(SECRETS_DIR_VARIABLE)
        assert told == (FIXED[SECRETS_DIR_VARIABLE] if given else None), name


def test_every_secret_is_a_file_in_the_secrets_folder_named_for_what_it_holds() -> None:
    declared = compose()["secrets"]

    assert set(declared) == set(SECRETS)
    for secret, source in declared.items():
        assert source == {"file": f"${{SECRETS_DIR:-./secrets}}/{secret}"}, secret
        # What the service reads it as: a setting's file, or the seed's password.
        held = secret.removeprefix("app_")
        assert held in Settings.model_fields or secret == seed.PASSWORD_FILE, secret


def test_the_script_makes_exactly_the_files_compose_mounts() -> None:
    assert set(make_secrets.FILES) == set(SECRETS)
    assert make_secrets.PASSWORD_FILE == seed.PASSWORD_FILE


def test_the_defaults_are_the_services_own() -> None:
    for name in ("setup", "api"):
        for setting, value in environment(services()[name]).items():
            match = _INTERPOLATION.fullmatch(value)
            if match is None or match.group("rule") != ":-":
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

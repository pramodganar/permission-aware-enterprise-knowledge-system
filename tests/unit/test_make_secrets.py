"""Making the secret files Docker Compose gives the containers."""

import os
import subprocess
from pathlib import Path

import pytest

from app.core.config import Settings
from scripts import make_secrets, seed
from scripts.make_secrets import FILES, main, make

ROOT = Path(__file__).resolve().parents[2]


def test_the_files_made_are_ones_the_service_takes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    make(tmp_path)
    monkeypatch.setenv("APP_SECRETS_DIR", str(tmp_path))
    for name in ("APP_JWT_SECRET_KEY", "APP_LLM_API_KEY", "APP_ENVIRONMENT"):
        monkeypatch.delenv(name, raising=False)

    # A generated key passes even production's check on a typed one.
    settings = Settings(_env_file=None, environment="production")
    password = (tmp_path / seed.PASSWORD_FILE).read_text(encoding="utf-8").strip()

    assert len(settings.jwt_secret_key.get_secret_value()) >= 64
    assert settings.llm_api_key is None  # empty until a provider's key is put in
    assert len(password) >= seed.MIN_PASSWORD_LENGTH


def test_every_run_makes_new_values(tmp_path: Path) -> None:
    make(tmp_path / "one")
    make(tmp_path / "two")

    for name in ("app_jwt_secret_key", "seed_password"):
        assert (tmp_path / "one" / name).read_text("utf-8") != (tmp_path / "two" / name).read_text(
            "utf-8"
        )


def test_a_file_that_is_there_is_kept_as_it_is(tmp_path: Path) -> None:
    (tmp_path / "app_jwt_secret_key").write_text("the key in use\n", encoding="utf-8")

    done = make(tmp_path)

    assert done == {
        "app_jwt_secret_key": "kept",
        "seed_password": "made",
        "app_llm_api_key": "made",
    }
    assert (tmp_path / "app_jwt_secret_key").read_text("utf-8") == "the key in use\n"
    assert make(tmp_path) == dict.fromkeys(FILES, "kept")


def test_it_says_what_it_made_and_never_what_is_in_them(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--folder", str(tmp_path)]) == 0

    shown = capsys.readouterr().out
    assert "made" in shown
    assert "seed_password" in shown
    for name in FILES:
        value = (tmp_path / name).read_text("utf-8").strip()
        if value:
            assert value not in shown


def test_a_folder_where_a_file_should_be_is_reported_and_left_alone(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # As Docker leaves it when it starts before the file exists.
    (tmp_path / "app_jwt_secret_key").mkdir()

    assert main(["--folder", str(tmp_path)]) == make_secrets.IN_THE_WAY

    assert "app_jwt_secret_key" in capsys.readouterr().err
    assert (tmp_path / "app_jwt_secret_key").is_dir()
    assert not (tmp_path / "seed_password").exists()


@pytest.mark.skipif(os.name != "posix", reason="file modes are POSIX; Windows keeps its own ACLs")
def test_on_posix_only_the_owner_can_read_them(tmp_path: Path) -> None:
    make(tmp_path)

    for name in FILES:
        assert (tmp_path / name).stat().st_mode & 0o777 == 0o600, name


def test_the_default_folder_is_ignored_by_git() -> None:
    ignored = subprocess.run(
        ["git", "check-ignore", "-q", "secrets/app_jwt_secret_key"],  # noqa: S607
        cwd=ROOT,
        check=False,
    )

    assert make_secrets.DEFAULT_FOLDER == ROOT / "secrets"
    assert ignored.returncode == 0


def test_values_can_be_taken_from_an_env_file_for_a_stack_started_before(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    key = "k" + "Q4z" * 20
    env_file = tmp_path / "local.env"
    env_file.write_text(
        "# a comment\n"
        + "APP_JWT"
        + "_SECRET_KEY="
        + key
        + "\n"
        + "SEED_"
        + "PASSWORD=\n"  # empty: a new one is made
        + "APP_LLM_PROVIDER=none\n",
        encoding="utf-8",
    )
    folder = tmp_path / "secrets"

    assert main(["--folder", str(folder), "--from-env-file", str(env_file)]) == 0

    shown = capsys.readouterr().out
    assert (folder / "app_jwt_secret_key").read_text("utf-8") == key + "\n"
    assert "copied" in shown and key not in shown
    assert (folder / "seed_password").read_text("utf-8").strip() != ""
    assert (folder / "app_llm_api_key").read_text("utf-8") == "\n"


def test_a_value_from_the_env_file_never_replaces_a_file_that_is_there(tmp_path: Path) -> None:
    (tmp_path / "app_jwt_secret_key").write_text("the key in use\n", encoding="utf-8")

    done = make(tmp_path, {"APP_JWT" + "_SECRET_KEY": "q" * 64})

    assert done["app_jwt_secret_key"] == "kept"
    assert (tmp_path / "app_jwt_secret_key").read_text("utf-8") == "the key in use\n"

"""The stack, started with Docker Compose. Needs Docker: pytest -m docker

Each stack gets a project name, ports, an env file and a folder of secret
files of its own, made by scripts.make_secrets, so it never reads the local
.env or secrets/, or meets a stack already running. It is taken down with
its volume however the tests end.
"""

import json
import re
import secrets
import shutil
import sys
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

import httpx
import pytest
from dockertools import BACKUP, RESTORE, docker, free_port, health_of, wait_for

from scripts.make_secrets import make

pytestmark = pytest.mark.docker

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = str(ROOT / "compose.yaml")
COMPOSE_TLS = str(ROOT / "compose.tls.yaml")


class Stack:
    def __init__(
        self,
        folder: Path,
        secrets_of: "Stack | None" = None,
        *,
        tls: bool = False,
        **settings: str,
    ) -> None:
        """A stack of its own, with new secrets, or another's: a restore needs its key.

        With ``tls``, the proxy of compose.tls.yaml in front, for localhost.
        """
        self.project = f"knowledge-test-{secrets.token_hex(4)}"
        self.api_port, self.ui_port = free_port(), free_port()
        self.files = [COMPOSE, *([COMPOSE_TLS] if tls else [])]
        if tls:
            self.https_port, self.http_port = free_port(), free_port()
            settings = {
                "SITE_ADDRESS": "localhost",
                "HTTPS_PORT": str(self.https_port),
                "HTTP_PORT": str(self.http_port),
                **settings,
            }
        self.secrets = folder / f"{self.project}-secrets"
        if secrets_of is None:
            make(self.secrets)
        else:
            shutil.copytree(secrets_of.secrets, self.secrets)
        # On a Linux host Docker mounts the files as they are, readable by
        # their owner only, and the service's user isn't their owner. The docs
        # give them to it with sudo chown 10001; a test's throwaway secrets
        # are made readable instead (docs/configuration.md#secrets-as-files).
        for path in self.secrets.iterdir():
            path.chmod(0o644)
        self.password = (self.secrets / "seed_password").read_text("utf-8").strip()
        values = {
            "SECRETS_DIR": self.secrets.as_posix(),
            "API_PORT": str(self.api_port),
            "UI_PORT": str(self.ui_port),
            **settings,
        }
        self.env_file = folder / f"{self.project}.env"
        self.env_file.write_text("".join(f"{k}={v}\n" for k, v in values.items()), "utf-8")

    def compose(self, *args: str, check: bool = True) -> str:
        done = docker(
            "compose",
            *(option for file in self.files for option in ("-f", file)),
            "-p",
            self.project,
            "--env-file",
            str(self.env_file),
            *args,
            check=check,
            timeout=3600,
        )
        return done.stdout + done.stderr

    def up(self, check: bool = True) -> str:
        # --wait returns once the API and the UI pass their health checks.
        return self.compose("up", "-d", "--build", "--wait", check=check)

    def down(self, *args: str) -> None:
        self.compose("down", *args, check=False)

    def container(self, service: str) -> dict[str, object]:
        found: list[dict[str, object]] = json.loads(
            docker("inspect", f"{self.project}-{service}-1").stdout
        )
        return found[0]

    @property
    def volume(self) -> str:
        return f"{self.project}_data"

    @property
    def api(self) -> str:
        return f"http://127.0.0.1:{self.api_port}"


@pytest.fixture(scope="module")
def stack(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Stack]:
    started = Stack(tmp_path_factory.mktemp("compose"))
    try:
        started.up()
        yield started
    finally:
        started.down("-v")


def documents(stack: Stack, name: str) -> dict[str, object]:
    login = httpx.post(
        f"{stack.api}/auth/login",
        json={"email": f"{name}@acmecorp.example", "password": stack.password},
    )
    assert login.status_code == 200, login.text
    auth = {"Authorization": f"Bearer {login.json()['access_token']}"}
    listed: dict[str, object] = httpx.get(f"{stack.api}/documents", headers=auth).json()
    return listed


def restricted(listed: dict[str, object]) -> list[str]:
    found = listed["documents"]
    assert isinstance(found, list)
    return [d["title"] for d in found if d["classification"] == "restricted"]


def instant(stamp: str) -> datetime:
    """A time as Docker writes it, to the microsecond: Python reads no finer."""
    match = re.fullmatch(r"([\d-]+T[\d:]+)(?:\.(\d+))?(Z|[+-][\d:]+)", stamp)
    assert match, stamp
    whole, fraction, zone = match.groups()
    return datetime.fromisoformat(
        f"{whole}.{(fraction or '0')[:6]:0<6}{'+00:00' if zone == 'Z' else zone}"
    )


# First of the stack's tests: Docker keeps only the last five checks, so the
# API's first pass is still among them.
def test_the_ui_was_started_only_once_the_api_was_healthy(stack: Stack) -> None:
    api = health_of(f"{stack.project}-api-1")
    assert api is not None
    checks = api["Log"]
    assert isinstance(checks, list)
    passed = [instant(c["End"]) for c in checks if c["ExitCode"] == 0]
    ui = stack.container("ui")["State"]
    assert isinstance(ui, dict)

    assert passed
    assert instant(ui["StartedAt"]) >= min(passed)


@pytest.mark.parametrize("service", ["api", "ui"])
def test_the_api_and_the_ui_are_healthy(stack: Stack, service: str) -> None:
    state = health_of(f"{stack.project}-{service}-1")

    assert state is not None
    assert state["Status"] == "healthy"


def test_the_setup_has_no_health_check(stack: Stack) -> None:
    config = stack.container("setup")["Config"]
    assert isinstance(config, dict)

    # Docker keeps the image's timings; NONE is what switches the check off.
    assert config["Healthcheck"]["Test"] == ["NONE"]
    assert health_of(f"{stack.project}-setup-1") is None


def test_the_reader_reads_dockers_times() -> None:
    assert instant("2026-10-26T09:15:02.123456789Z") == datetime.fromisoformat(
        "2026-10-26T09:15:02.123456+00:00"
    )
    assert instant("2026-10-26T10:15:02+01:00") == instant("2026-10-26T09:15:02Z")


def test_each_person_sees_their_own_documents_through_the_stack(stack: Stack) -> None:
    carol, david = documents(stack, "carol"), documents(stack, "david")

    assert carol["total"] == 6
    assert restricted(carol) == ["HR Case File 2026-014"]
    assert david["total"] == 4
    assert restricted(david) == []


def test_the_ui_is_up_and_reaches_the_api_by_its_name(stack: Stack) -> None:
    assert wait_for(f"http://127.0.0.1:{stack.ui_port}/_stcore/health").text == "ok"
    reached = stack.compose(
        "exec",
        "-T",
        "ui",
        "python",
        "-c",
        "import httpx; print(httpx.get('http://api:8000/health').json())",
    )

    assert "{'status': 'ok'}" in reached


def test_the_ui_is_given_none_of_the_services_settings(stack: Stack) -> None:
    config = stack.container("ui")["Config"]
    assert isinstance(config, dict)
    names = {item.split("=", 1)[0] for item in config["Env"]}

    assert not {n for n in names if n.startswith(("APP_", "SEED_"))}


@pytest.mark.parametrize("service", ["api", "ui"])
def test_nothing_is_published_beyond_this_machine(stack: Stack, service: str) -> None:
    host = stack.container(service)["HostConfig"]
    assert isinstance(host, dict)
    bindings = [b for ports in host["PortBindings"].values() for b in ports]

    assert bindings
    assert {b["HostIp"] for b in bindings} == {"127.0.0.1"}


@pytest.mark.parametrize("service", ["setup", "api", "ui"])
def test_every_container_is_hardened_as_it_runs(stack: Stack, service: str) -> None:
    host = stack.container(service)["HostConfig"]
    assert isinstance(host, dict)

    assert host["ReadonlyRootfs"] is True
    assert host["CapDrop"] == ["ALL"]
    assert host["SecurityOpt"] == ["no-new-privileges:true"]
    assert host["Privileged"] is False


@pytest.mark.parametrize("service", ["setup", "api", "ui"])
def test_every_containers_log_is_rotated(stack: Stack, service: str) -> None:
    host = stack.container(service)["HostConfig"]
    assert isinstance(host, dict)

    assert host["LogConfig"] == {
        "Type": "json-file",
        "Config": {"max-size": "10m", "max-file": "5"},
    }


def test_the_setup_ran_with_no_network(stack: Stack) -> None:
    host = stack.container("setup")["HostConfig"]
    assert isinstance(host, dict)

    assert host["NetworkMode"] == "none"


def test_only_the_data_can_be_written(stack: Stack) -> None:
    code = stack.compose("exec", "-T", "api", "touch", "/app/probe", check=False)
    ui = stack.compose("exec", "-T", "ui", "touch", "/app/ui/probe", check=False)
    data = docker(
        "compose",
        "-f",
        COMPOSE,
        "-p",
        stack.project,
        "--env-file",
        str(stack.env_file),
        "exec",
        "-T",
        "api",
        "test",
        "-w",
        "/data",
        check=False,
    )

    assert "Read-only file system" in code
    assert "Read-only file system" in ui
    assert data.returncode == 0


def test_a_restart_keeps_the_data_and_builds_nothing_again(stack: Stack) -> None:
    stack.down()
    stack.up()
    setup = stack.compose("logs", "setup")
    versions = stack.compose("exec", "-T", "api", "ls", "/data/index/versions")

    assert "documents skipped:   17" in setup
    assert "is up to date" in setup
    assert versions.split() == ["000001"]
    assert documents(stack, "carol")["total"] == 6


def in_a_container(volume: str, backups: Path, *command: str, writable: str = "") -> str:
    """Run a command in the API's image, as root, with the volume and the backups folder.

    As in docs/deployment.md: root, so the files keep the owner they had, and
    no network, because a backup needs none.
    """
    return docker(
        "run",
        "--rm",
        "--user",
        "0",
        "--network",
        "none",
        "-v",
        f"{volume}:/data{'' if writable == 'data' else ':ro'}",
        "-v",
        f"{backups.as_posix()}:/backup{'' if writable == 'backup' else ':ro'}",
        "knowledge-api",
        *command,
    ).stdout


def trail(stack: Stack) -> str:
    return stack.compose("exec", "-T", "api", "python", "-m", "scripts.audit", "check")


def test_a_backup_restored_into_a_new_stack_serves_the_same_and_its_trail_still_checks(
    stack: Stack, tmp_path: Path
) -> None:
    backups = tmp_path / "backups"
    backups.mkdir()
    before = trail(stack)
    assert "intact" in before

    # The API stopped while the files are copied, so the database isn't
    # written half way through.
    stack.compose("stop", "api")
    in_a_container(stack.volume, backups, *BACKUP, writable="backup")
    stack.compose("start", "api")
    wait_for(f"{stack.api}/health")

    restored = Stack(tmp_path, secrets_of=stack)
    try:
        restored.compose("up", "--no-start")
        in_a_container(restored.volume, backups, *RESTORE, writable="data")
        restored.up()
        after = trail(restored)

        # Every record backed up, each still matching its seal, and one more:
        # setup's seed re-chunks on every start, an admin action, audited.
        assert "intact" in after
        assert int(highest(after)) == int(highest(before)) + 1
        assert documents(restored, "carol")["total"] == 6
        assert restricted(documents(restored, "david")) == []
        setup = restored.compose("logs", "setup")
        assert "documents skipped:   17" in setup
        assert "is up to date" in setup
    finally:
        restored.down("-v")


def highest(checked: str) -> str:
    match = re.search(r"highest ID: (\S+)", checked)
    assert match, checked
    return match.group(1)


def test_in_production_the_demo_data_is_refused_and_the_api_never_starts(
    tmp_path: Path,
) -> None:
    production = Stack(tmp_path, APP_ENVIRONMENT="production")
    try:
        assert "didn't complete successfully" in production.up(check=False)
        setup = production.compose("logs", "setup")
        api = production.container("api")["State"]
        assert isinstance(api, dict)

        assert "refusing to load the AcmeCorp demo data" in setup
        assert api["Status"] == "created"
        assert api["StartedAt"].startswith("0001-01-01")
    finally:
        production.down("-v")


def settings_of(stack: Stack, service: str) -> list[str]:
    config = stack.container(service)["Config"]
    assert isinstance(config, dict)
    given: list[str] = config["Env"]
    return given


@pytest.mark.parametrize("service", ["setup", "api", "ui"])
def test_no_secret_is_in_any_containers_settings(stack: Stack, service: str) -> None:
    given = settings_of(stack, service)
    names = {item.split("=", 1)[0] for item in given}

    assert not names & {"APP_JWT_SECRET_KEY", "APP_LLM_API_KEY", "SEED_PASSWORD"}
    for path in stack.secrets.iterdir():
        value = path.read_text("utf-8").strip()
        if value:
            assert not any(value in item for item in given), f"{service} holds {path.name}"


def test_each_container_is_given_only_its_own_secret_files(stack: Stack) -> None:
    api = stack.compose("exec", "-T", "api", "ls", "/run/secrets").split()
    ui = stack.compose("exec", "-T", "ui", "ls", "/run/secrets", check=False)

    assert sorted(api) == ["app_jwt_secret_key", "app_llm_api_key"]
    assert "No such file or directory" in ui


def test_a_missing_key_file_stops_the_setup_naming_it_and_the_api_never_starts(
    tmp_path: Path,
) -> None:
    missing = Stack(tmp_path)
    gone = missing.secrets / "app_jwt_secret_key"
    gone.unlink()
    try:
        started = missing.up(check=False)

        if sys.platform == "linux":
            # Docker Engine refuses to start a container whose file isn't there.
            assert f"does not exist: {gone.as_posix()}" in started
        else:
            # Docker Desktop mounts an empty folder in its place, which the
            # service refuses, naming it.
            assert "didn't complete successfully" in started
            setup = missing.compose("logs", "setup")
            assert "app_jwt_secret_key in /run/secrets is a folder" in setup
        assert never_started(missing, "api")
    finally:
        missing.down("-v")


def never_started(stack: Stack, service: str) -> bool:
    name = f"{stack.project}-{service}-1"
    if not docker("ps", "-a", "-q", "--filter", f"name=^{name}$").stdout.split():
        return True
    state = stack.container(service)["State"]
    assert isinstance(state, dict)
    return str(state["StartedAt"]).startswith("0001-01-01")

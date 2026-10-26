"""The stack, started with Docker Compose. Needs Docker: pytest -m docker

Each stack gets a project name and ports of its own and an env file made
for it, so it never reads the local .env or meets a stack already running,
and it is taken down with its volume however the tests end.
"""

import json
import secrets
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from dockertools import docker, free_port, wait_for

pytestmark = pytest.mark.docker

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = str(ROOT / "compose.yaml")
PASSWORD = "compose-test-password"


class Stack:
    def __init__(self, folder: Path, **settings: str) -> None:
        self.project = f"knowledge-test-{secrets.token_hex(4)}"
        self.api_port, self.ui_port = free_port(), free_port()
        values = {
            "APP_JWT_SECRET_KEY": secrets.token_urlsafe(48),
            "SEED_PASSWORD": PASSWORD,
            "API_PORT": str(self.api_port),
            "UI_PORT": str(self.ui_port),
            **settings,
        }
        self.env_file = folder / f"{self.project}.env"
        self.env_file.write_text("".join(f"{k}={v}\n" for k, v in values.items()), "utf-8")

    def compose(self, *args: str, check: bool = True) -> str:
        done = docker(
            "compose",
            "-f",
            COMPOSE,
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
        started = self.compose("up", "-d", "--build", "--wait", check=check)
        if check:
            # --wait only knows a container is running: with no health check
            # it can't know the API has loaded its model and index.
            wait_for(f"{self.api}/health")
            wait_for(f"http://127.0.0.1:{self.ui_port}/_stcore/health")
        return started

    def down(self, *args: str) -> None:
        self.compose("down", *args, check=False)

    def container(self, service: str) -> dict[str, object]:
        found: list[dict[str, object]] = json.loads(
            docker("inspect", f"{self.project}-{service}-1").stdout
        )
        return found[0]

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
        f"{stack.api}/auth/login", json={"email": f"{name}@acmecorp.example", "password": PASSWORD}
    )
    assert login.status_code == 200, login.text
    auth = {"Authorization": f"Bearer {login.json()['access_token']}"}
    listed: dict[str, object] = httpx.get(f"{stack.api}/documents", headers=auth).json()
    return listed


def restricted(listed: dict[str, object]) -> list[str]:
    found = listed["documents"]
    assert isinstance(found, list)
    return [d["title"] for d in found if d["classification"] == "restricted"]


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


def test_without_a_signing_key_compose_refuses_to_start(tmp_path: Path) -> None:
    empty = tmp_path / "empty.env"
    empty.write_text("", "utf-8")
    refused = docker(
        "compose",
        "-f",
        COMPOSE,
        "-p",
        "knowledge-test-nokey",
        "--env-file",
        str(empty),
        "config",
        check=False,
    )

    assert refused.returncode != 0
    assert "APP_JWT_SECRET_KEY" in refused.stderr

"""The images, built and run. Needs Docker: pytest -m docker

Builds both images from the Dockerfile, then runs them the way the docs
say to: the company seeded and indexed on a volume by containers with no
network, the API started on it, and the UI started against the API. It
looks inside each image too, because what an image holds is what anyone
who pulls it can read.

Slow the first time (PyTorch and the embedding model are downloaded into
the API image); after that the build is cached.
"""

import json
import secrets
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from dockertools import docker, free_port, wait_for
from imagesettings import ALLOWED

from scripts.scan_secrets import local_secrets

pytestmark = pytest.mark.docker

ROOT = Path(__file__).resolve().parents[2]
API = "knowledge-api:test"
UI = "knowledge-ui:test"
PASSWORD = "docker-test-password"
LEAVE = "How many days of annual leave do I get?"


def in_image(image: str, *command: str, network: str = "none", check: bool = True) -> str:
    """Run a command in a new container of the image and return what it printed."""
    return docker(
        "run",
        "--rm",
        f"--network={network}",
        "--entrypoint",
        command[0],
        image,
        *command[1:],
        check=check,
    ).stdout


@pytest.fixture(scope="module")
def images() -> None:
    for target, tag in (("api", API), ("ui", UI)):
        docker("build", "--target", target, "-t", tag, str(ROOT), timeout=3600)


@pytest.fixture(scope="module")
def key() -> str:
    return secrets.token_urlsafe(48)


@pytest.fixture(scope="module")
def network(images: None) -> Iterator[str]:
    name = f"knowledge-test-{secrets.token_hex(4)}"
    docker("network", "create", name)
    yield name
    docker("network", "rm", name, check=False)


@pytest.fixture(scope="module")
def volume(images: None, key: str) -> Iterator[str]:
    """A volume with the company seeded and indexed, by containers with no network."""
    name = f"knowledge-test-{secrets.token_hex(4)}"
    docker("volume", "create", name)
    common = [
        "run",
        "--rm",
        "--network=none",
        "-v",
        f"{name}:/data",
        "-e",
        f"APP_JWT_SECRET_KEY={key}",
    ]
    # Removed however this ends, a failed seed included.
    try:
        docker(*common, "-e", f"SEED_PASSWORD={PASSWORD}", API, "python", "-m", "scripts.seed")
        docker(*common, API, "python", "-m", "scripts.index", "build")
        yield name
    finally:
        docker("volume", "rm", name, check=False)


@pytest.fixture(scope="module")
def api(volume: str, network: str, key: str) -> Iterator[str]:
    """The API running on the volume, published on this machine only."""
    port = free_port()
    name = f"knowledge-api-{secrets.token_hex(4)}"
    docker(
        "run",
        "-d",
        "--name",
        name,
        "--network",
        network,
        "--network-alias",
        "api",
        "-p",
        f"127.0.0.1:{port}:8000",
        "-v",
        f"{volume}:/data",
        "-e",
        f"APP_JWT_SECRET_KEY={key}",
        API,
    )
    try:
        wait_for(f"http://127.0.0.1:{port}/health")
        yield f"http://127.0.0.1:{port}"
    finally:
        docker("rm", "-f", name, check=False)


def logs_of(base: str) -> str:
    port = base.rsplit(":", 1)[1]
    names = docker("ps", "--filter", f"publish={port}", "--format", "{{.Names}}").stdout.split()
    return docker("logs", names[0]).stdout + docker("logs", names[0]).stderr


# --- what is in the images ---------------------------------------------------------------


@pytest.mark.parametrize("image", [API, UI])
def test_the_image_runs_as_the_unprivileged_user(images: None, image: str) -> None:
    assert docker("image", "inspect", "--format", "{{.Config.User}}", image).stdout.strip() == "app"
    assert in_image(image, "id", "-u").strip() == "10001"


@pytest.mark.parametrize("image", [API, UI])
def test_every_setting_in_the_image_is_one_decided_on(images: None, image: str) -> None:
    listed = docker("image", "inspect", "--format", "{{json .Config.Env}}", image).stdout
    names = {item.split("=", 1)[0] for item in json.loads(listed)}

    assert names <= set(ALLOWED), names - set(ALLOWED)


@pytest.mark.parametrize("image", [API, UI])
def test_no_secret_is_in_the_image_its_settings_or_its_history(images: None, image: str) -> None:
    config = docker("image", "inspect", image).stdout
    history = docker("history", "--no-trunc", image).stdout
    local = local_secrets(ROOT / ".env")

    for name in ("APP_JWT_SECRET_KEY", "APP_LLM_API_KEY", "SEED_PASSWORD"):
        assert name not in config
    for value in local.values():
        assert value not in config
        assert value not in history
        # grep exits 1 when it finds nothing, which is what is wanted.
        assert in_image(image, "grep", "-rlF", value, "/app", "/opt", check=False) == ""
    assert in_image(image, "sh", "-c", "ls -A /app | sort").split() == (
        ["app", "data", "scripts"] if image == API else ["ui"]
    )


def test_the_api_image_holds_the_fictional_company_and_nothing_else_of_data(images: None) -> None:
    assert in_image(API, "ls", "/app/data").split() == ["acmecorp"]
    assert in_image(API, "ls", "-A", "/data").split() == []


def test_the_service_can_not_change_its_own_code(images: None) -> None:
    attempt = docker(
        "run",
        "--rm",
        "--network=none",
        "--entrypoint",
        "touch",
        API,
        "/app/app/main.py",
        check=False,
    )

    assert attempt.returncode != 0
    assert "Permission denied" in attempt.stderr


def test_pytorch_in_the_image_is_the_cpu_build_with_no_gpu_libraries(images: None) -> None:
    assert in_image(API, "python", "-c", "import torch; print(torch.__version__)").strip() == (
        "2.14.1+cpu"
    )
    assert "nvidia" not in in_image(API, "pip", "list").lower()


def test_the_model_loads_from_the_image_with_no_network(images: None, key: str) -> None:
    loaded = docker(
        "run",
        "--rm",
        "--network=none",
        "-e",
        f"APP_JWT_SECRET_KEY={key}",
        API,
        "python",
        "-c",
        "from app.core.config import Settings; "
        "from app.retrieval.embeddings import load_embedder; "
        "print(load_embedder(Settings()).embed_query('leave').shape)",
    ).stdout

    assert loaded.strip() == "(384,)"


def test_the_ui_image_holds_none_of_the_service(images: None) -> None:
    probe = "import importlib.util as u; print([u.find_spec(m) is None for m in "
    missing = in_image(UI, "python", "-c", probe + "('app', 'torch', 'sqlalchemy', 'faiss')])")

    assert missing.strip() == "[True, True, True, True]"
    assert "demo.py" not in in_image(UI, "ls", "/app/ui")


# --- running them ------------------------------------------------------------------------


def test_the_api_serves_the_seeded_company_from_the_volume(api: str) -> None:
    login = httpx.post(
        f"{api}/auth/login", json={"email": "david@acmecorp.example", "password": PASSWORD}
    )
    assert login.status_code == 200, login.text
    auth = {"Authorization": f"Bearer {login.json()['access_token']}"}

    found = httpx.post(f"{api}/search", headers=auth, json={"question": LEAVE}, timeout=30)
    documents = httpx.get(f"{api}/documents", headers=auth).json()

    readable = {d["title"] for d in documents["documents"]}
    titles = {r["document"]["title"] for r in found.json()["results"]}
    assert found.status_code == 200
    assert documents["total"] == 4
    assert "Leave Policy" in titles
    assert titles <= readable
    # No model is configured in the image: asking is switched off, search works.
    assert httpx.post(f"{api}/ask", headers=auth, json={"question": LEAVE}).status_code == 503


def test_the_api_logs_json_lines_with_no_question_in_them(api: str) -> None:
    login = httpx.post(
        f"{api}/auth/login", json={"email": "frank@acmecorp.example", "password": PASSWORD}
    )
    auth = {"Authorization": f"Bearer {login.json()['access_token']}"}
    httpx.post(
        f"{api}/search", headers=auth, json={"question": "Zanzibar hotel limits?"}, timeout=30
    )

    lines = [line for line in logs_of(api).splitlines() if line.startswith("{")]
    assert any(json.loads(line).get("message") == "request handled" for line in lines)
    assert "Zanzibar" not in logs_of(api)


def test_the_interactive_docs_are_off_in_production(volume: str, key: str) -> None:
    port = free_port()
    name = f"knowledge-api-{secrets.token_hex(4)}"
    docker(
        "run",
        "-d",
        "--name",
        name,
        "-p",
        f"127.0.0.1:{port}:8000",
        "-v",
        f"{volume}:/data",
        "-e",
        f"APP_JWT_SECRET_KEY={key}",
        "-e",
        "APP_ENVIRONMENT=production",
        API,
    )
    try:
        wait_for(f"http://127.0.0.1:{port}/health")
        assert httpx.get(f"http://127.0.0.1:{port}/docs").status_code == 404
    finally:
        docker("rm", "-f", name, check=False)


def test_the_api_refuses_to_start_without_a_signing_key(images: None) -> None:
    started = docker("run", "--rm", "--network=none", API, check=False, timeout=120)

    assert started.returncode != 0
    assert "jwt_secret_key" in started.stderr.lower()


def test_the_ui_starts_and_reaches_the_api(api: str, network: str) -> None:
    port = free_port()
    name = f"knowledge-ui-{secrets.token_hex(4)}"
    docker("run", "-d", "--name", name, "--network", network, "-p", f"127.0.0.1:{port}:8501", UI)
    try:
        assert wait_for(f"http://127.0.0.1:{port}/_stcore/health").text == "ok"
        # The address the UI is given is the API's on the shared network.
        reached = in_image(
            UI,
            "python",
            "-c",
            "import httpx; print(httpx.get('http://api:8000/health').json())",
            network=network,
        )
        assert reached.strip() == "{'status': 'ok'}"
    finally:
        docker("rm", "-f", name, check=False)


def test_on_linux_the_secret_files_are_made_readable_by_their_owner_only(images: None) -> None:
    # The unit test of this is skipped on Windows; here it runs on Linux.
    modes = in_image(
        API,
        "sh",
        "-c",
        "python -m scripts.make_secrets --folder /tmp/made >/dev/null"
        " && stat -c '%a %n' /tmp/made/*",
    )

    assert sorted(modes.split("\n")[:-1]) == [
        "600 /tmp/made/app_jwt_secret_key",
        "600 /tmp/made/app_llm_api_key",
        "600 /tmp/made/seed_password",
    ]

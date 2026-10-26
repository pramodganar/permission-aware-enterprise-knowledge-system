"""The Dockerfile and what goes into it, read without Docker (docs/docker.md).

Building the images is tested by ``tests/docker`` (``pytest -m docker``),
which needs Docker. These run everywhere, and check what a build alone
wouldn't: that the base is pinned, the versions are the ones tested here,
no secret is baked in, nothing runs as root, the build only sees what it
is allowed to, the UI's image holds none of the service, and each image's
health check asks the route that says it is ready, and fails when it should.
"""

import ast
import fnmatch
import json
import re
import subprocess
import sys
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.metadata import version
from pathlib import Path

import pytest
from imagesettings import OURS

from app.api import health
from app.api.middleware import HEALTH_PATH
from app.core.config import Settings

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = ROOT / "Dockerfile"
IGNORE = ROOT / ".dockerignore"
SECRETS = ("APP_JWT_SECRET_KEY", "APP_LLM_API_KEY", "SEED_PASSWORD")


@dataclass
class Stage:
    name: str
    base: str
    # Each instruction as (keyword, rest), continuation lines joined.
    instructions: list[tuple[str, str]] = field(default_factory=list)

    def all(self, keyword: str) -> list[str]:
        return [rest for word, rest in self.instructions if word == keyword]


def instructions(text: str) -> list[tuple[str, str]]:
    joined = re.sub(r"\s*\\\n\s*", " ", text)
    found = []
    for line in joined.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            word, _, rest = line.partition(" ")
            found.append((word.upper(), rest.strip()))
    return found


def stages(text: str) -> dict[str, Stage]:
    found: dict[str, Stage] = {}
    current: Stage | None = None
    for word, rest in instructions(text):
        if word == "FROM":
            base, _, name = rest.partition(" AS ")
            current = found[name] = Stage(name, base)
        elif current is not None:
            current.instructions.append((word, rest))
    return found


def dockerfile() -> str:
    return DOCKERFILE.read_text(encoding="utf-8")


def default_arg(name: str) -> str:
    match = re.search(rf"^ARG {name}=(\S+)$", dockerfile(), re.MULTILINE)
    assert match, f"no ARG {name} with a default"
    return match.group(1)


def environment(stage: Stage) -> dict[str, str]:
    found: dict[str, str] = {}
    for rest in stage.all("ENV"):
        for name, value in re.findall(r"(\w+)=(\S+)", rest):
            found[name] = value
    return found


def copied(stage: Stage) -> list[str]:
    """The sources of every COPY from the build context, not from another stage."""
    sources: list[str] = []
    for rest in stage.all("COPY"):
        if rest.startswith("--from="):
            continue
        *paths, _destination = rest.split()
        sources.extend(paths)
    return sources


def test_the_two_images_are_built_on_one_pinned_base() -> None:
    found = stages(dockerfile())

    assert {"api", "ui"} <= set(found)
    image = default_arg("PYTHON_IMAGE")
    assert re.fullmatch(r"python:3\.11\.\d+-slim-bookworm@sha256:[0-9a-f]{64}", image), image
    assert found["base"].base == "${PYTHON_IMAGE}"
    for stage in found.values():
        assert stage.base in ("${PYTHON_IMAGE}", "base"), stage.name


def test_the_api_image_is_the_default_build() -> None:
    # The last stage is what "docker build ." gives.
    assert list(stages(dockerfile()))[-1] == "api"


def test_pytorch_is_the_cpu_build_of_the_version_tested_here() -> None:
    install = next(r for r in stages(dockerfile())["api-build"].all("RUN") if "torch==" in r)
    pinned = re.search(r"torch==([\w.]+)", install)
    demo = re.search(r"^torch==([\w.]+)$", (ROOT / "ui/requirements.txt").read_text("utf-8"), re.M)

    assert "--index-url https://download.pytorch.org/whl/cpu" in install
    assert pinned and demo
    assert pinned.group(1) == demo.group(1) == version("torch").split("+")[0]


def test_the_model_in_the_image_is_the_one_the_settings_use() -> None:
    build = stages(dockerfile())["api-build"]
    baked = re.findall(r"SentenceTransformer\('([^']+)'", " ".join(build.all("RUN")))
    api = environment(stages(dockerfile())["api"])
    default = Settings.model_fields["embedding_model"].default

    assert baked == [default]
    # And a container loads it from the image, never from the network.
    assert api["APP_EMBEDDING_OFFLINE"] == "true"
    assert api["HF_HUB_OFFLINE"] == "1"
    assert api["HF_HOME"] == environment(build)["HF_HOME"]


def test_no_secret_is_given_a_value_anywhere_in_the_file() -> None:
    for word, rest in instructions(dockerfile()):
        for secret in SECRETS:
            assert secret not in rest, f"{word} mentions {secret}"
        assert not re.search(r"(?i)(key|token|password|secret)\w*=", rest), f"{word} {rest}"


def test_every_setting_the_images_hold_is_one_decided_on() -> None:
    # Whatever it is called: a secret named APP_SEED is still a secret.
    for stage in stages(dockerfile()).values():
        for name in environment(stage):
            assert name in OURS, f"{stage.name} sets {name}, which isn't in tests/imagesettings.py"
        for arg in stage.all("ARG"):
            raise AssertionError(f"{stage.name} has ARG {arg}: build arguments stay in the history")


def test_every_image_runs_as_the_unprivileged_user() -> None:
    found = stages(dockerfile())
    create = " ".join(found["base"].all("RUN"))

    assert re.search(r"groupadd --gid 10001 app\b", create)
    assert re.search(r"useradd --uid 10001 --gid app\b", create)
    for name in ("api", "ui"):
        users = found[name].all("USER")
        assert users == ["app"], f"{name} runs as {users}"
        # USER comes before the command, after everything that needs root.
        words = [word for word, _ in found[name].instructions]
        assert words.index("USER") > max(i for i, w in enumerate(words) if w in ("COPY", "RUN"))


def test_the_database_and_index_are_on_the_volume_not_in_the_image() -> None:
    api = stages(dockerfile())["api"]
    env = environment(api)

    assert api.all("VOLUME") == ["/data"]
    assert env["APP_DATABASE_URL"] == "sqlite:////data/app.db"
    assert env["APP_INDEX_DIRECTORY"] == "/data/index"


def test_the_api_command_is_the_one_in_the_readme() -> None:
    command = stages(dockerfile())["api"].all("CMD")[0]

    assert '"app.main:serve", "--factory"' in command
    # uvicorn's access log writes full URLs, query strings included.
    assert '"--no-access-log"' in command
    assert "uvicorn app.main:serve --factory --no-access-log" in (ROOT / "README.md").read_text(
        "utf-8"
    )


def imports_the_service(path: Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(a.name.split(".")[0] == "app" for a in node.names):
            return True
        if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "app":
            return True
    return False


def test_the_ui_image_holds_every_ui_module_and_none_of_the_service() -> None:
    ui = stages(dockerfile())["ui"]
    modules = {p.relative_to(ROOT).as_posix() for p in (ROOT / "ui").glob("*.py")}
    service_free = {m for m in modules if not imports_the_service(ROOT / m)}

    assert set(copied(ui)) == service_free
    assert {"ui/demo.py", "ui/streamlit_app.py"} == modules - service_free
    assert not any(p.startswith(("app", "scripts", "data")) for p in copied(ui))
    assert "torch" not in " ".join(stages(dockerfile())["ui-build"].all("RUN"))


def pins(path: Path) -> dict[str, str]:
    found: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(r"([A-Za-z0-9_.-]+)==(\S+)", line.strip())
        if match:
            found[match.group(1).lower()] = match.group(2)
    return found


def test_the_ui_installs_the_versions_tested_here() -> None:
    ui = pins(ROOT / "requirements-ui.txt")
    everything = pins(ROOT / "requirements.txt") | pins(ROOT / "requirements-dev.txt")

    assert set(ui) == {"streamlit", "httpx", "pydantic"}
    for name, pinned in ui.items():
        assert everything[name] == pinned, name


def ignore_rules() -> list[str]:
    lines = IGNORE.read_text(encoding="utf-8").splitlines()
    return [line.strip() for line in lines if line.strip() and not line.startswith("#")]


def sent_to_the_build(path: str) -> bool:
    """Whether .dockerignore lets this path into the build: the last rule that matches wins.

    Close to Docker's own reading, not the same: ``tests/docker`` checks
    what really reaches the images.
    """
    sent = True
    for rule in ignore_rules():
        allow = rule.startswith("!")
        pattern = rule.lstrip("!").rstrip("/")
        parts = path.split("/")
        prefixes = ["/".join(parts[: n + 1]) for n in range(len(parts))]
        if any(
            fnmatch.fnmatch(p, pattern) or fnmatch.fnmatch(p, pattern.lstrip("*/"))
            for p in prefixes
        ):
            sent = allow
    return sent


def test_the_build_sees_only_what_it_is_allowed_to() -> None:
    assert ignore_rules()[0] == "*"

    for path in (
        "app/main.py",
        "ui/app.py",
        "scripts/seed.py",
        "requirements.txt",
        "requirements-ui.txt",
        "data/acmecorp/manifest.json",
    ):
        assert sent_to_the_build(path), path
    for path in (
        ".env",
        ".env.example",
        "data/app.db",
        "data/index/current",
        ".git/config",
        ".venv/pyvenv.cfg",
        "tests/conftest.py",
        "evaluation/results/answers.json",
        "docs/security.md",
        "app/__pycache__/main.cpython-311.pyc",
    ):
        assert not sent_to_the_build(path), path


@pytest.mark.parametrize("stage", ["api", "ui"])
def test_everything_the_images_copy_is_let_into_the_build(stage: str) -> None:
    for source in copied(stages(dockerfile())[stage]):
        assert (ROOT / source).exists(), source
        probe = source.rstrip("/") + ("/__init__.py" if source.endswith("/") else "")
        if source == "data/acmecorp/":
            probe = "data/acmecorp/manifest.json"
        assert sent_to_the_build(probe), source


@dataclass(frozen=True)
class HealthCheck:
    options: dict[str, str]
    command: list[str]

    def seconds(self, option: str) -> float:
        value = self.options[option]
        assert value.endswith("s"), f"--{option}={value}"
        return float(value.removesuffix("s"))

    @property
    def probe(self) -> str:
        assert self.command[:2] == ["python", "-c"], self.command
        return self.command[2]

    @property
    def url(self) -> str:
        match = re.search(r"urlopen\('([^']+)', timeout=(\d+)\)", self.probe)
        assert match, self.probe
        return match.group(1)

    @property
    def probe_timeout(self) -> float:
        match = re.search(r"timeout=(\d+)\)", self.probe)
        assert match, self.probe
        return float(match.group(1))


def health_check(stage: Stage) -> HealthCheck:
    (rest,) = stage.all("HEALTHCHECK")
    options, _, command = rest.partition(" CMD ")
    # The exec form, so no shell is needed to run it.
    return HealthCheck(dict(re.findall(r"--([\w-]+)=(\S+)", options)), json.loads(command))


# The route each image's check asks, and the port it serves on.
READY = {"api": (8000, HEALTH_PATH), "ui": (8501, "/_stcore/health")}


@pytest.mark.parametrize("stage", ["api", "ui"])
def test_each_image_checks_its_own_health_route_on_its_own_port(stage: str) -> None:
    found = stages(dockerfile())[stage]
    port, path = READY[stage]

    assert found.all("EXPOSE") == [str(port)]
    assert re.search(rf"\b{port}\b", found.all("CMD")[0])
    assert health_check(found).url == f"http://127.0.0.1:{port}{path}"


def test_the_api_checks_the_one_route_the_service_has_for_it() -> None:
    assert [route.path for route in health.router.routes] == [HEALTH_PATH]  # type: ignore[attr-defined]


@pytest.mark.parametrize("stage", ["api", "ui"])
def test_each_check_gives_up_before_docker_does_and_waits_out_a_slow_start(stage: str) -> None:
    check = health_check(stages(dockerfile())[stage])

    # A probe Docker kills counts as failed too, but says only "timed out".
    assert check.probe_timeout < check.seconds("timeout") < check.seconds("interval")
    assert int(check.options["retries"]) >= 2
    # Failures while starting don't count; a fast start is seen in seconds.
    assert check.seconds("start-period") >= 60
    assert check.seconds("start-interval") <= 5


def test_the_api_has_longer_to_start_than_the_ui() -> None:
    # It loads the embedding model and the index before its port opens.
    found = stages(dockerfile())

    assert health_check(found["api"]).seconds("start-period") > health_check(found["ui"]).seconds(
        "start-period"
    )


def test_the_docker_page_gives_each_checks_route_and_timings() -> None:
    page = (ROOT / "docs/docker.md").read_text(encoding="utf-8")

    def written(seconds: float) -> str:
        return f"{seconds:g} s"

    for stage, label in (("api", "API"), ("ui", "UI")):
        check = health_check(stages(dockerfile())[stage])
        port, path = READY[stage]
        row = next(line for line in page.splitlines() if line.startswith(f"| {label} |"))
        assert f"{path}` on port {port}" in row, row
        assert (
            f"every {written(check.seconds('start-interval'))},"
            f" for up to {written(check.seconds('start-period'))}"
        ) in row, row
        assert row.endswith(f"every {written(check.seconds('interval'))} |"), row
        assert f"on a 200 within {check.probe_timeout:g} seconds" in page


def test_only_the_two_images_have_a_health_check() -> None:
    for name, stage in stages(dockerfile()).items():
        assert bool(stage.all("HEALTHCHECK")) == (name in READY), name


@pytest.fixture
def server() -> Iterator[tuple[str, list[int]]]:
    """A local server answering with whatever status is first in the list."""
    status = [200]

    class Answer(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(status[0])
            self.end_headers()

        def log_message(self, *args: object) -> None:
            pass

    running = ThreadingHTTPServer(("127.0.0.1", 0), Answer)
    threading.Thread(target=running.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{running.server_address[1]}", status
    finally:
        running.shutdown()
        running.server_close()


def run_probe(stage: str, base: str) -> int:
    """The image's own probe, run here with its address moved to ``base``."""
    check = health_check(stages(dockerfile())[stage])
    port, _ = READY[stage]
    probe = check.probe.replace(f"http://127.0.0.1:{port}", base)
    assert probe != check.probe
    done = subprocess.run(  # noqa: S603 - this interpreter, with the Dockerfile's own code
        [sys.executable, "-c", probe],
        capture_output=True,
        timeout=check.seconds("timeout"),
        check=False,
    )
    return done.returncode


@pytest.mark.parametrize("stage", ["api", "ui"])
def test_the_probe_passes_on_a_200(stage: str, server: tuple[str, list[int]]) -> None:
    base, _ = server

    assert run_probe(stage, base) == 0


@pytest.mark.parametrize("stage", ["api", "ui"])
@pytest.mark.parametrize("status", [503, 500, 404])
def test_the_probe_fails_on_anything_else(
    stage: str, status: int, server: tuple[str, list[int]]
) -> None:
    # 503 is what /health gives when the database doesn't answer.
    base, answer = server
    answer[0] = status

    assert run_probe(stage, base) == 1


@pytest.mark.parametrize("stage", ["api", "ui"])
def test_the_probe_fails_when_nothing_is_listening(stage: str) -> None:
    with ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler) as closed:
        port = closed.server_address[1]

    # Exit code 1 is unhealthy; Docker reserves 2.
    assert run_probe(stage, f"http://127.0.0.1:{port}") == 1


def test_the_reader_joins_lines_and_finds_stages() -> None:
    sample = "ARG X=1\nFROM a AS one\nRUN b \\\n    c\nFROM one AS two\nUSER app\n"
    found = stages(sample)

    assert list(found) == ["one", "two"]
    assert found["one"].all("RUN") == ["b c"]
    assert found["two"].base == "one"

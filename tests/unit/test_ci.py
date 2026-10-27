"""The CI workflows, read without running them (docs/ci.md).

What a run on GitHub shows only when something goes wrong, or never: that
CI runs the README's checks and nothing less, can't write to the repository
or read a secret, runs code only from actions pinned to a commit, installs
what the images install, and that the Docker workflow runs whenever
anything that goes into the images changes.
"""

import fnmatch
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"
CI = WORKFLOWS / "ci.yml"
DOCKER = WORKFLOWS / "docker.yml"
_PINNED = re.compile(r"^[\w.-]+/[\w.-]+@[0-9a-f]{40}$")


def workflow(path: Path) -> dict[Any, Any]:
    loaded: dict[Any, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    return loaded


def triggers(path: Path) -> dict[str, Any]:
    # YAML 1.1 reads the key "on" as True.
    found: dict[str, Any] = workflow(path)[True]
    return found


def steps(path: Path) -> list[dict[str, Any]]:
    return [step for job in workflow(path)["jobs"].values() for step in job["steps"]]


def commands(path: Path) -> list[str]:
    return [line.strip() for step in steps(path) for line in step.get("run", "").splitlines()]


def every_workflow() -> list[Path]:
    return sorted(WORKFLOWS.glob("*.yml"))


def test_there_are_the_two_workflows() -> None:
    assert [path.name for path in every_workflow()] == ["ci.yml", "docker.yml"]


@pytest.mark.parametrize("path", every_workflow(), ids=lambda p: p.name)
def test_no_workflow_can_write_to_the_repository(path: Path) -> None:
    found = workflow(path)

    assert found["permissions"] == {"contents": "read"}
    for name, job in found["jobs"].items():
        assert "permissions" not in job, f"{name} asks for more"


@pytest.mark.parametrize("path", every_workflow(), ids=lambda p: p.name)
def test_no_workflow_reads_a_secret_or_runs_code_from_a_fork_with_them(path: Path) -> None:
    text = path.read_text(encoding="utf-8")

    assert "secrets." not in text
    # Runs a fork's pull request with the repository's own token and secrets.
    assert "pull_request_target" not in triggers(path)
    assert "workflow_run" not in triggers(path)


@pytest.mark.parametrize("path", every_workflow(), ids=lambda p: p.name)
def test_every_action_is_pinned_to_a_commit_with_its_version_noted(path: Path) -> None:
    # A tag can be moved to other code; a commit can't.
    lines = path.read_text(encoding="utf-8").splitlines()
    used = [line for line in lines if line.strip().startswith(("- uses:", "uses:"))]

    assert used
    for line in used:
        action, _, comment = line.split("uses:", 1)[1].strip().partition(" # ")
        assert _PINNED.fullmatch(action), line
        assert re.fullmatch(r"v\d+\.\d+\.\d+", comment), f"{line}: say which version"


@pytest.mark.parametrize("path", every_workflow(), ids=lambda p: p.name)
def test_every_checkout_leaves_no_token_behind(path: Path) -> None:
    checkouts = [s for s in steps(path) if s.get("uses", "").startswith("actions/checkout@")]

    assert checkouts
    for step in checkouts:
        assert step["with"]["persist-credentials"] is False


@pytest.mark.parametrize("path", every_workflow(), ids=lambda p: p.name)
def test_every_job_has_a_time_limit_and_a_pinned_runner(path: Path) -> None:
    for name, job in workflow(path)["jobs"].items():
        assert 0 < job["timeout-minutes"] <= 60, name
        assert re.fullmatch(r"ubuntu-\d\d\.\d\d", job["runs-on"]), name


@pytest.mark.parametrize("path", every_workflow(), ids=lambda p: p.name)
def test_a_newer_push_cancels_the_older_run(path: Path) -> None:
    concurrency = workflow(path)["concurrency"]

    assert concurrency["cancel-in-progress"] is True
    assert "${{ github.ref }}" in concurrency["group"]


def readme_checks() -> list[str]:
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    block = text.split("## Checks", 1)[1].split("```", 2)[1]
    return [line.strip() for line in block.splitlines() if line.strip()]


def test_ci_runs_the_readmes_checks_and_the_secret_scan() -> None:
    run = commands(CI)

    assert readme_checks() == ["ruff check .", "ruff format --check .", "mypy", "pytest --cov"]
    for check in [*readme_checks(), "python -m scripts.scan_secrets"]:
        assert check in run, check


def test_ci_runs_on_every_push_to_main_and_every_pull_request() -> None:
    found = triggers(CI)

    assert found["push"] == {"branches": ["main"]}
    assert "paths" not in found["push"]
    assert "pull_request" in found
    assert "workflow_dispatch" in found


def test_the_secret_scan_gets_the_whole_history() -> None:
    (checkout,) = [s for s in steps(CI) if s.get("uses", "").startswith("actions/checkout@")]

    assert checkout["with"]["fetch-depth"] == 0


def test_ci_fetches_no_model() -> None:
    (job,) = workflow(CI)["jobs"].values()

    assert job["env"]["HF_HUB_OFFLINE"] == "1"


def pinned_torch(text: str) -> str:
    match = re.search(r"torch==([\w.]+)", text)
    assert match
    return match.group(1)


@pytest.mark.parametrize("path", every_workflow(), ids=lambda p: p.name)
def test_ci_installs_the_cpu_pytorch_the_images_install(path: Path) -> None:
    install = next(c for c in commands(path) if "torch==" in c)
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "--index-url https://download.pytorch.org/whl/cpu" in install
    assert pinned_torch(install) == pinned_torch(dockerfile)
    assert "python -m pip install -r requirements-dev.txt" in commands(path)


@pytest.mark.parametrize("path", every_workflow(), ids=lambda p: p.name)
def test_ci_uses_the_python_the_project_targets(path: Path) -> None:
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    (setup,) = [s for s in steps(path) if s.get("uses", "").startswith("actions/setup-python@")]

    assert 'python_version = "3.11"' in pyproject
    assert setup["with"]["python-version"] == "3.11"


def test_the_docker_workflow_runs_the_docker_tests() -> None:
    assert "pytest -m docker tests/docker" in commands(DOCKER)


def test_the_docker_workflow_watches_the_same_paths_for_pushes_and_pull_requests() -> None:
    found = triggers(DOCKER)

    assert found["push"]["branches"] == ["main"]
    assert found["push"]["paths"] == found["pull_request"]["paths"]
    assert "workflow_dispatch" in found


def watched(path: str) -> bool:
    return any(fnmatch.fnmatch(path, pattern) for pattern in triggers(DOCKER)["push"]["paths"])


def copied_into_the_images() -> set[str]:
    sources: set[str] = set()
    for line in (ROOT / "Dockerfile").read_text(encoding="utf-8").splitlines():
        if line.startswith("COPY ") and "--from=" not in line:
            *paths, _ = line.removeprefix("COPY ").rstrip(" \\").split()
            sources.update(paths)
    # Continuation lines of the UI's long COPY.
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    sources.update(re.findall(r"\bui/\w+\.py\b", text))
    return sources


def test_the_docker_workflow_runs_when_anything_in_the_images_changes() -> None:
    copied = copied_into_the_images()

    assert copied
    for source in copied:
        probe = source.rstrip("/") + ("/x.py" if source.endswith("/") else "")
        assert watched(probe), source
    for path in (
        "Dockerfile",
        ".dockerignore",
        "compose.yaml",
        "requirements.txt",
        "requirements-ui.txt",
        "tests/docker/test_stack.py",
        "tests/dockertools.py",
        # How the Docker tests themselves run: their settings, time limits and tools.
        "tests/conftest.py",
        "requirements-dev.txt",
        "pyproject.toml",
        "tests/imagesettings.py",
        ".github/workflows/docker.yml",
    ):
        assert watched(path), path


def test_the_docker_workflow_skips_what_cant_change_the_images() -> None:
    skipped = ("docs/monitoring.md", "README.md", "evaluation/report.py", "tests/unit/test_ci.py")
    for path in skipped:
        assert not watched(path), path

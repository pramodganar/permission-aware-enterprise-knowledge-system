"""Running Docker from the tests that need it (pytest -m docker)."""

import json
import socket
import subprocess
import time

import httpx


def docker(
    *args: str, check: bool = True, timeout: float = 600
) -> subprocess.CompletedProcess[str]:
    run = subprocess.run(  # noqa: S603 - docker, with arguments written in these tests
        ["docker", *args],  # noqa: S607
        capture_output=True,
        # Docker writes UTF-8, progress bars included; Windows would read it
        # in its own code page and fail on the first one.
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )
    if check and run.returncode != 0:
        raise AssertionError(f"docker {' '.join(args)} failed:\n{run.stdout}\n{run.stderr}")
    return run


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
        return port


def wait_for(url: str, seconds: float = 120) -> httpx.Response:
    deadline = time.monotonic() + seconds
    while True:
        try:
            response = httpx.get(url, timeout=2)
            if response.status_code == 200:
                return response
        except httpx.HTTPError:
            pass
        if time.monotonic() > deadline:
            raise AssertionError(f"{url} never answered")
        time.sleep(1)


def health_of(container: str) -> dict[str, object] | None:
    """The container's health as Docker keeps it: its status and its last few checks."""
    found = docker("inspect", "--format", "{{json .State.Health}}", container).stdout
    state: dict[str, object] | None = json.loads(found)
    return state


def wait_healthy(container: str, seconds: float = 180) -> None:
    """Wait for Docker to call the container healthy, by the image's own check."""
    deadline = time.monotonic() + seconds
    while True:
        state = health_of(container)
        assert state is not None, f"{container} has no health check"
        if state["Status"] == "healthy":
            return
        if state["Status"] == "unhealthy" or time.monotonic() > deadline:
            raise AssertionError(f"{container} is {state['Status']}: {state['Log']}")
        time.sleep(1)

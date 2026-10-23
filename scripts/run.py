"""Start the API and the UI together, to use the system on one machine.

    python -m scripts.run [--host 127.0.0.1] [--api-port 8000] [--ui-port 8501]

The API starts first. The UI is started only once the API answers its
health check, pointed at it with UI_API_URL, so the UI never comes up
talking to nothing. If either stops, the other is stopped too, and Ctrl+C
stops both.

The API needs the company data and an index, as when it is run on its own
(README.md). Starting can take a minute: it loads the embedding model and
checks the index before it answers.

Both are served over plain HTTP. They listen on this machine only unless
--host says otherwise; anything else puts the sign-in page on the network
without TLS.
"""

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

UI_PAGE = Path(__file__).resolve().parent.parent / "ui" / "app.py"
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
API_START_SECONDS = 180.0
POLL_SECONDS = 0.5
STOP_SECONDS = 10.0


class Process(Protocol):
    def poll(self) -> int | None: ...
    def terminate(self) -> None: ...
    def kill(self) -> None: ...
    def wait(self, timeout: float | None = None) -> int: ...


Start = Callable[..., Process]


@dataclass(frozen=True)
class Addresses:
    host: str
    api_port: int
    ui_port: int

    @property
    def api_url(self) -> str:
        return f"http://{_bracketed(self.host)}:{self.api_port}"

    @property
    def ui_url(self) -> str:
        return f"http://{_bracketed(self.host)}:{self.ui_port}"


def _bracketed(host: str) -> str:
    return f"[{host}]" if ":" in host else host


def api_command(addresses: Addresses) -> list[str]:
    return [
        sys.executable,
        "-m",
        "uvicorn",
        "app.main:serve",
        "--factory",
        "--no-access-log",
        "--host",
        addresses.host,
        "--port",
        str(addresses.api_port),
    ]


def ui_command(addresses: Addresses) -> list[str]:
    return [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        str(UI_PAGE),
        "--server.address",
        addresses.host,
        "--server.port",
        str(addresses.ui_port),
        "--server.headless",
        "true",
        "--browser.gatherUsageStats",
        "false",
    ]


def ui_environment(addresses: Addresses, environ: Mapping[str, str]) -> dict[str, str]:
    """The UI's environment: this one, without the service's settings.

    The UI needs only the API's address. The service's settings hold its
    secrets, the signing key and the model's API key, and a process that
    doesn't need a secret shouldn't be handed it.
    """
    kept = {name: value for name, value in environ.items() if not _service_setting(name)}
    return {**kept, "UI_API_URL": addresses.api_url}


def _service_setting(name: str) -> bool:
    return name.upper().startswith("APP_") or name.upper() == "SEED_PASSWORD"


def healthy(api_url: str) -> bool:
    """Whether the API answers its health check."""
    try:
        with urllib.request.urlopen(f"{api_url}/health", timeout=2) as response:  # noqa: S310 - our own http URL
            body = json.load(response)
            return response.status == 200 and isinstance(body, dict) and body.get("status") == "ok"
    except (OSError, ValueError):
        return False


def wait_for_api(
    api: Process,
    api_url: str,
    *,
    seconds: float = API_START_SECONDS,
    check: Callable[[str], bool] = healthy,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> str | None:
    """Wait until the API answers. Returns what went wrong, or None."""
    deadline = clock() + seconds
    while True:
        code = api.poll()
        if code is not None:
            return (
                f"The API stopped while starting (exit code {code}); its output above says why. "
                "It needs the company data (python -m scripts.seed) and an index "
                "(python -m scripts.index build)."
            )
        if check(api_url):
            return None
        if clock() >= deadline:
            return f"The API didn't answer at {api_url}/health within {seconds:.0f} seconds."
        sleep(POLL_SECONDS)


def stop(process: Process, seconds: float = STOP_SECONDS) -> None:
    """Ask a process to stop, and make it if it doesn't."""
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=seconds)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def first_to_stop(
    processes: Mapping[str, Process], sleep: Callable[[float], None] = time.sleep
) -> tuple[str, int]:
    """Wait until one of the processes stops; which one, and its exit code."""
    while True:
        for name, process in processes.items():
            code = process.poll()
            if code is not None:
                return name, code
        sleep(POLL_SECONDS)


def run(
    addresses: Addresses,
    *,
    start: Start = subprocess.Popen,
    environ: Mapping[str, str] = os.environ,
    say: Callable[[str], None] = print,
    wait: Callable[[Process, str], str | None] = wait_for_api,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    if addresses.host not in LOCAL_HOSTS:
        say(
            f"Listening on {addresses.host}: anyone who can reach this machine can reach the "
            "sign-in page, over plain HTTP."
        )
    say(f"Starting the API at {addresses.api_url}...")
    api = start(api_command(addresses))
    try:
        problem = wait(api, addresses.api_url)
        if problem:
            say(problem)
            return 1
        ui = start(ui_command(addresses), env=ui_environment(addresses, environ))
        try:
            say(f"The UI is at {addresses.ui_url}. Press Ctrl+C to stop both.")
            name, code = first_to_stop({"API": api, "UI": ui}, sleep)
            say(f"The {name} stopped (exit code {code}), so the other is being stopped too.")
            return code or 1
        finally:
            stop(ui)
    except KeyboardInterrupt:
        say("Stopping.")
        return 0
    finally:
        stop(api)


def port(text: str) -> int:
    value = int(text)
    if not 1 <= value <= 65535:
        raise argparse.ArgumentTypeError("a port is a number from 1 to 65535")
    return value


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Start the API and the UI together.")
    parser.add_argument("--host", default="127.0.0.1", help="address to listen on")
    parser.add_argument("--api-port", type=port, default=8000)
    parser.add_argument("--ui-port", type=port, default=8501)
    arguments = parser.parse_args(argv)
    if arguments.api_port == arguments.ui_port:
        parser.error("the API and the UI need different ports")
    return run(Addresses(arguments.host, arguments.api_port, arguments.ui_port))


if __name__ == "__main__":
    sys.exit(main())

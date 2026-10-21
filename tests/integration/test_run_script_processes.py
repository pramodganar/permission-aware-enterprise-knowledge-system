"""The launcher with real processes: small stand-ins for the API and the UI.

The real API needs the embedding model, so the full start is tested with
the model tests (tests/integration/test_run_everything.py). These check the
part that is the launcher's own: real processes started in the right order,
the UI pointed at the API, and nothing left running afterwards.
"""

import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from scripts.run import Addresses, run

STAND_IN_API = """
import json, sys
from http.server import BaseHTTPRequestHandler, HTTPServer

if sys.argv[2] != "ok":
    sys.exit(int(sys.argv[2]))

class Health(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"status": "ok"}).encode()
        self.send_response(200 if self.path == "/health" else 404)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass

HTTPServer(("127.0.0.1", int(sys.argv[1])), Health).serve_forever()
"""

STAND_IN_UI = """
import os, sys, time
with open(sys.argv[1], "w") as f:
    f.write(os.environ["UI_API_URL"])
while True:
    time.sleep(0.1)
"""


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port


class StandIns:
    """Starts the stand-ins in place of uvicorn and Streamlit, and keeps hold of them."""

    def __init__(self, folder: Path, api_outcome: str = "ok") -> None:
        self.api_program = folder / "api.py"
        self.ui_program = folder / "ui.py"
        self.api_program.write_text(STAND_IN_API, encoding="utf-8")
        self.ui_program.write_text(STAND_IN_UI, encoding="utf-8")
        self.seen = folder / "seen.txt"
        self.api_outcome = api_outcome
        self.started: list[subprocess.Popen[bytes]] = []

    def __call__(self, command: list[str], **options: Any) -> subprocess.Popen[bytes]:
        if "uvicorn" in command:
            port = command[command.index("--port") + 1]
            real = [sys.executable, str(self.api_program), port, self.api_outcome]
        else:
            real = [sys.executable, str(self.ui_program), str(self.seen)]
        process = subprocess.Popen(real, **options)  # noqa: S603 - our own stand-in programs
        self.started.append(process)
        return process

    def cleanup(self) -> None:
        for process in self.started:
            if process.poll() is None:
                process.kill()
                process.wait()


@pytest.fixture
def stand_ins(tmp_path: Path) -> Any:
    made: list[StandIns] = []

    def make(api_outcome: str = "ok") -> StandIns:
        made.append(StandIns(tmp_path, api_outcome))
        return made[-1]

    yield make
    for each in made:
        each.cleanup()


def stopping_when(condition: Callable[[], bool], within: float = 30) -> Callable[[float], None]:
    """A sleep that presses Ctrl+C once ``condition`` holds."""
    deadline = time.monotonic() + within

    def sleep(seconds: float) -> None:
        if condition() or time.monotonic() > deadline:
            raise KeyboardInterrupt
        time.sleep(seconds)

    return sleep


def test_both_start_in_order_and_ctrl_c_leaves_nothing_running(stand_ins: Any) -> None:
    started = stand_ins()
    addresses = Addresses("127.0.0.1", free_port(), free_port())
    said: list[str] = []
    result: list[int] = []

    def launch() -> None:
        result.append(
            run(
                addresses,
                start=started,
                say=said.append,
                sleep=stopping_when(started.seen.exists),
            )
        )

    thread = threading.Thread(target=launch)
    thread.start()
    thread.join(timeout=60)

    assert result == [0]
    assert started.seen.read_text(encoding="utf-8") == addresses.api_url
    assert len(started.started) == 2
    assert all(process.poll() is not None for process in started.started)
    assert said[-1] == "Stopping."


def test_an_api_that_fails_to_start_means_the_ui_is_never_started(stand_ins: Any) -> None:
    started = stand_ins(api_outcome="3")
    said: list[str] = []

    code = run(Addresses("127.0.0.1", free_port(), free_port()), start=started, say=said.append)

    assert code == 1
    assert len(started.started) == 1
    assert "exit code 3" in said[-1]
    assert not started.seen.exists()

"""One command, the real thing: the company loaded, the index built, the API and the UI up.

Needs the embedding model, so it runs with the model tests: pytest -m model
"""

import json
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import pytest

from scripts.run import Addresses, run

pytestmark = pytest.mark.model

ROOT = Path(__file__).resolve().parents[2]
PASSWORD = "run-everything-password"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port


def get(url: str) -> str:
    with urllib.request.urlopen(url, timeout=5) as response:  # noqa: S310 - local test server
        text: str = response.read().decode()
        return text


def sign_in(api_url: str, email: str) -> int:
    request = urllib.request.Request(  # noqa: S310 - local test server
        f"{api_url}/auth/login",
        data=json.dumps({"email": email, "password": PASSWORD}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
        status: int = response.status
        return status


def test_one_command_starts_an_api_and_a_ui_that_work_together(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = {
        "APP_DATABASE_URL": f"sqlite:///{(tmp_path / 'app.db').as_posix()}",
        "APP_INDEX_DIRECTORY": str(tmp_path / "index"),
        "APP_JWT_SECRET_KEY": "run-everything-" + "k" * 40,
        # Nothing leaves the machine: no language model, the embedding model from the cache.
        "APP_LLM_PROVIDER": "none",
        "APP_EMBEDDING_OFFLINE": "true",
        "APP_LOG_LEVEL": "WARNING",
    }
    for name, value in settings.items():
        monkeypatch.setenv(name, value)
    for step in (["scripts.seed", "--password", PASSWORD], ["scripts.index", "build"]):
        subprocess.run([sys.executable, "-m", *step], cwd=ROOT, check=True, capture_output=True)  # noqa: S603

    addresses = Addresses("127.0.0.1", free_port(), free_port())
    seen: dict[str, object] = {}

    def everything_up() -> bool:
        try:
            seen["ui"] = get(f"{addresses.ui_url}/_stcore/health")
        except OSError:
            return False
        seen["api"] = json.loads(get(f"{addresses.api_url}/health"))
        seen["sign_in"] = sign_in(addresses.api_url, "david@acmecorp.example")
        seen["page"] = get(addresses.ui_url)
        return True

    deadline = time.monotonic() + 240

    def sleep(seconds: float) -> None:
        if everything_up() or time.monotonic() > deadline:
            raise KeyboardInterrupt
        time.sleep(seconds)

    said: list[str] = []
    result: list[int] = []
    thread = threading.Thread(
        target=lambda: result.append(run(addresses, say=said.append, sleep=sleep))
    )
    thread.start()
    thread.join(timeout=300)

    assert result == [0], said
    assert seen["api"] == {"status": "ok"}
    assert seen["ui"] == "ok"
    assert seen["sign_in"] == 200
    assert "<html" in str(seen["page"]).lower()
    # Both stopped: nothing answers on either port any more.
    for url in (f"{addresses.api_url}/health", f"{addresses.ui_url}/_stcore/health"):
        with pytest.raises(OSError):
            get(url)

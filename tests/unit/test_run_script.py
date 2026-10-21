"""Starting the API and the UI together: the order, the waiting and the stopping."""

import io
import subprocess
import sys
from typing import Any
from urllib.error import URLError

import pytest

from scripts import run as launcher
from scripts.run import (
    Addresses,
    api_command,
    first_to_stop,
    healthy,
    main,
    run,
    stop,
    ui_command,
    ui_environment,
    wait_for_api,
)

HERE = Addresses("127.0.0.1", 8000, 8501)


class FakeProcess:
    """A process that has stopped once ``exit_code`` is set."""

    def __init__(self, name: str, log: list[str], exit_code: int | None = None) -> None:
        self.name = name
        self.log = log
        self.exit_code = exit_code
        self.ignores_terminate = False

    def poll(self) -> int | None:
        return self.exit_code

    def terminate(self) -> None:
        self.log.append(f"terminate {self.name}")
        if not self.ignores_terminate:
            self.exit_code = -15

    def kill(self) -> None:
        self.log.append(f"kill {self.name}")
        self.exit_code = -9

    def wait(self, timeout: float | None = None) -> int:
        if self.exit_code is None:
            raise subprocess.TimeoutExpired("fake", timeout or 0)
        return self.exit_code


class Starter:
    """Records what is started, in order, and hands out fake processes."""

    def __init__(self, *, api_exit: int | None = None) -> None:
        self.log: list[str] = []
        self.environments: dict[str, dict[str, str] | None] = {}
        self.processes: dict[str, FakeProcess] = {}
        self.api_exit = api_exit

    def __call__(self, command: list[str], env: dict[str, str] | None = None) -> FakeProcess:
        name = "API" if "uvicorn" in command else "UI"
        self.log.append(f"start {name}")
        self.environments[name] = env
        process = FakeProcess(name, self.log, self.api_exit if name == "API" else None)
        self.processes[name] = process
        return process


def test_the_commands_serve_the_api_and_the_ui_on_the_addresses_given() -> None:
    api = api_command(HERE)
    ui = ui_command(HERE)

    assert api[:4] == [sys.executable, "-m", "uvicorn", "app.main:serve"]
    assert "--factory" in api
    assert api[api.index("--host") + 1] == "127.0.0.1"
    assert api[api.index("--port") + 1] == "8000"
    assert ui[:4] == [sys.executable, "-m", "streamlit", "run"]
    assert ui[4].endswith("app.py")
    assert ui[ui.index("--server.port") + 1] == "8501"
    assert ui[ui.index("--server.address") + 1] == "127.0.0.1"
    assert ui[ui.index("--browser.gatherUsageStats") + 1] == "false"


def test_the_ui_is_pointed_at_the_api_and_keeps_the_rest_of_the_environment() -> None:
    env = ui_environment(HERE, {"PATH": "/bin", "UI_API_URL": "http://elsewhere"})

    assert env == {"PATH": "/bin", "UI_API_URL": "http://127.0.0.1:8000"}


def test_an_ipv6_host_is_bracketed_in_urls() -> None:
    assert Addresses("::1", 8000, 8501).api_url == "http://[::1]:8000"


# --- waiting for the API ----------------------------------------------------------------


class Clock:
    """Time that only moves when slept, and gives up after a while."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds
        if self.now > 1000:
            raise AssertionError("still waiting after 1000 seconds")


def test_waiting_ends_when_the_api_answers() -> None:
    clock = Clock()
    answers = iter([False, False, True])

    problem = wait_for_api(
        FakeProcess("API", []),
        HERE.api_url,
        check=lambda _: next(answers),
        clock=clock,
        sleep=clock.sleep,
    )

    assert problem is None
    assert clock.now == 1.0


def test_an_api_that_stops_while_starting_says_so_and_what_it_needs() -> None:
    clock = Clock()

    problem = wait_for_api(
        FakeProcess("API", [], exit_code=3),
        HERE.api_url,
        check=lambda _: False,
        clock=clock,
        sleep=clock.sleep,
    )

    assert problem is not None
    assert "exit code 3" in problem
    assert "scripts.seed" in problem
    assert "scripts.index build" in problem
    assert clock.now == 0


def test_an_api_that_never_answers_is_given_up_on() -> None:
    clock = Clock()

    problem = wait_for_api(
        FakeProcess("API", []),
        HERE.api_url,
        seconds=5,
        check=lambda _: False,
        clock=clock,
        sleep=clock.sleep,
    )

    assert problem == "The API didn't answer at http://127.0.0.1:8000/health within 5 seconds."
    assert clock.now == 5.0


# --- running both -----------------------------------------------------------------------


def give_up_after(times: int = 100) -> Any:
    """A sleep for a loop that should have ended long before."""
    count = iter(range(times))

    def sleep(_seconds: float) -> None:
        if next(count, None) is None:
            raise AssertionError("still looping")

    return sleep


def run_with(starter: Starter, problem: str | None = None, **options: Any) -> tuple[int, list[str]]:
    said: list[str] = []
    code = run(
        HERE,
        start=starter,
        environ={"PATH": "/bin"},
        say=said.append,
        wait=lambda _process, _url: problem,
        sleep=give_up_after(),
        **options,
    )
    return code, said


def test_the_ui_starts_only_after_the_api_answers_and_both_stop_together() -> None:
    starter = Starter()

    def ui_stops(_seconds: float) -> None:
        starter.processes["UI"].exit_code = 0

    said: list[str] = []
    code = run(
        HERE,
        start=starter,
        environ={"PATH": "/bin"},
        say=said.append,
        wait=lambda _process, _url: None,
        sleep=ui_stops,
    )

    assert starter.log == ["start API", "start UI", "terminate API"]
    assert starter.environments["UI"] == {"PATH": "/bin", "UI_API_URL": "http://127.0.0.1:8000"}
    assert starter.environments["API"] is None
    assert "The UI is at http://127.0.0.1:8501. Press Ctrl+C to stop both." in said
    assert said[-1] == "The UI stopped (exit code 0), so the other is being stopped too."
    assert code == 1


def test_an_api_that_does_not_come_up_means_no_ui() -> None:
    starter = Starter()

    code, said = run_with(starter, problem="The API stopped while starting (exit code 3).")

    assert code == 1
    assert starter.log == ["start API", "terminate API"]
    assert said[-1] == "The API stopped while starting (exit code 3)."


def test_the_api_stopping_stops_the_ui() -> None:
    starter = Starter()

    def api_stops(_seconds: float) -> None:
        starter.processes["API"].exit_code = 7

    said: list[str] = []
    code = run(
        HERE, start=starter, environ={}, say=said.append, wait=lambda *_: None, sleep=api_stops
    )

    assert code == 7
    assert starter.log == ["start API", "start UI", "terminate UI"]


def test_ctrl_c_stops_both_and_is_not_an_error() -> None:
    starter = Starter()

    def interrupted(_seconds: float) -> None:
        raise KeyboardInterrupt

    said: list[str] = []
    code = run(
        HERE, start=starter, environ={}, say=said.append, wait=lambda *_: None, sleep=interrupted
    )

    assert code == 0
    assert starter.log == ["start API", "start UI", "terminate UI", "terminate API"]
    assert said[-1] == "Stopping."


def test_ctrl_c_while_the_api_starts_stops_it() -> None:
    starter = Starter()

    def interrupted(_process: object, _url: str) -> str | None:
        raise KeyboardInterrupt

    said: list[str] = []
    code = run(
        HERE,
        start=starter,
        environ={},
        say=said.append,
        wait=interrupted,
        sleep=give_up_after(),
    )

    assert code == 0
    assert starter.log == ["start API", "terminate API"]


def test_listening_beyond_this_machine_is_warned_about() -> None:
    starter = Starter()
    said: list[str] = []

    run(
        Addresses("0.0.0.0", 8000, 8501),  # noqa: S104 - the case being tested
        start=starter,
        environ={},
        say=said.append,
        wait=lambda *_: "stop here",
        sleep=give_up_after(),
    )

    assert "plain HTTP" in said[0]


def test_listening_locally_is_not_warned_about() -> None:
    _, said = run_with(Starter(), problem="stop here")

    assert not [line for line in said if "plain HTTP" in line]


# --- stopping ---------------------------------------------------------------------------


def test_a_process_that_ignores_being_asked_to_stop_is_killed() -> None:
    log: list[str] = []
    stubborn = FakeProcess("UI", log)
    stubborn.ignores_terminate = True

    stop(stubborn, seconds=0)

    assert log == ["terminate UI", "kill UI"]
    assert stubborn.exit_code == -9


def test_a_process_that_has_already_stopped_is_left_alone() -> None:
    log: list[str] = []

    stop(FakeProcess("UI", log, exit_code=0))

    assert log == []


def test_whichever_stops_first_is_reported() -> None:
    log: list[str] = []
    api, ui = FakeProcess("API", log), FakeProcess("UI", log)
    ticks = iter([None, None])

    def later(_seconds: float) -> None:
        next(ticks, None)
        ui.exit_code = 2

    assert first_to_stop({"API": api, "UI": ui}, later) == ("UI", 2)


# --- the command line -------------------------------------------------------------------


def recording(started: list[Addresses]) -> Any:
    def fake_run(addresses: Addresses) -> int:
        started.append(addresses)
        return 0

    return fake_run


@pytest.mark.parametrize(
    "argv",
    [
        ["--api-port", "0"],
        ["--ui-port", "70000"],
        ["--api-port", "x"],
        ["--api-port", "9000", "--ui-port", "9000"],
    ],
)
def test_bad_ports_are_refused(argv: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[Addresses] = []
    monkeypatch.setattr(launcher, "run", recording(started))

    with pytest.raises(SystemExit) as stopped:
        main(argv)

    assert stopped.value.code == 2
    assert started == []


def test_good_ports_are_used(monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[Addresses] = []
    monkeypatch.setattr(launcher, "run", recording(started))

    assert main(["--api-port", "9000", "--ui-port", "9001"]) == 0
    assert started == [Addresses("127.0.0.1", 9000, 9001)]


# --- the health check -------------------------------------------------------------------


class Reply(io.BytesIO):
    def __init__(self, status: int, body: bytes) -> None:
        super().__init__(body)
        self.status = status


@pytest.mark.parametrize(
    ("reply", "answer"),
    [
        (Reply(200, b'{"status": "ok"}'), True),
        (Reply(200, b'{"status": "starting"}'), False),
        (Reply(200, b"<html>a proxy page</html>"), False),
        (Reply(200, b'["ok"]'), False),
        (Reply(204, b'{"status": "ok"}'), False),
        (URLError("refused"), False),
        (TimeoutError(), False),
    ],
)
def test_the_api_is_healthy_only_when_it_says_ok(
    monkeypatch: pytest.MonkeyPatch, reply: Reply | Exception, answer: bool
) -> None:
    def urlopen(url: str, timeout: float) -> Reply:
        assert url == "http://127.0.0.1:8000/health"
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr("urllib.request.urlopen", urlopen)

    assert healthy(HERE.api_url) is answer

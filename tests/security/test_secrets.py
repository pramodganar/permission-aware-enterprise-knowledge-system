"""Secrets stay where they belong (SR-17, threat T20).

Three places a secret can end up that it shouldn't:

- In the repository, committed by mistake, now or at any point in its
  history. ``scripts.scan_secrets`` looks, and these tests run it on this
  repository every time. They also check that it would find one: in a
  repository made for the purpose, with a key committed and then deleted.
- In a log line or an error, on its way to the model's provider. The
  language model's key is sent in a header and nowhere else, with every
  logger at its most talkative and every way a call can fail.
- In a process that doesn't need it. Tested with the run script, in
  tests/unit/test_run_script.py.

Nothing here contains a secret-shaped string as written: the examples are
put together when the tests run, or the scan would find this file.
"""

import logging
import subprocess
import traceback
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from app.rag.llm import LLMError, LLMUnavailableError
from app.rag.prompt import INSTRUCTIONS, PROMPT_VERSION, Prompt
from app.rag.providers import OpenAICompatibleProvider
from logcheck import everything_logged
from scripts import scan_secrets
from scripts.scan_secrets import PATTERNS, Finding, kinds_in, local_secrets, scan

ROOT = Path(__file__).resolve().parents[2]

# One example of each kind, made up and put together here.
EXAMPLES = {
    "Groq API key": "gsk" + "_" + "Q7w" * 12,
    "OpenAI-style API key": "sk" + "-proj-" + "Zx9" * 10,
    "AWS access key": "AKIA" + "QWERTYUIOP123456",
    "GitHub token": "ghp" + "_" + "a1B2" * 9,
    "Hugging Face token": "hf" + "_" + "k9L" * 12,
    "private key": "-----BEGIN " + "RSA PRIVATE KEY-----",
    "signed token": ".".join(["eyJ" + "hbGciOiJIUzI1NiJ9", "eyJ" + "zdWIiOiI0In0xx", "x" * 30]),
    "value for a secret setting": "APP_JWT" + "_SECRET_KEY=" + "q" * 20,
}


def git(root: Path, *args: str) -> None:
    subprocess.run(  # noqa: S603 - git, with fixed arguments
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", *args],  # noqa: S607
        cwd=root,
        check=True,
        capture_output=True,
    )


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    git(tmp_path, "init", "-q")
    (tmp_path / "README.md").write_text("Nothing to see.\n", encoding="utf-8")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-q", "-m", "first")
    return tmp_path


# --- this repository --------------------------------------------------------------------


def test_no_secret_is_in_any_tracked_file_or_anywhere_in_the_history() -> None:
    try:
        findings = scan(ROOT)
    except scan_secrets.NotACheckoutError:
        pytest.skip("not a git checkout")

    assert [str(finding) for finding in findings] == []


def test_the_real_secrets_in_env_are_looked_for_by_their_values() -> None:
    found = local_secrets(ROOT / ".env")
    if not found:
        pytest.skip("no .env here, so no real secrets to look for")
    # The scan above searched for each of these, word for word.
    assert {"APP_JWT_SECRET_KEY"} <= set(found)


def test_env_and_streamlit_secrets_are_ignored_and_never_tracked() -> None:
    for name in (".env", ".streamlit/secrets.toml"):
        ignored = subprocess.run(  # noqa: S603 - git, with fixed arguments
            ["git", "check-ignore", "-q", name],  # noqa: S607
            cwd=ROOT,
            check=False,
        )
        assert ignored.returncode == 0, name
    tracked = subprocess.run(
        ["git", "ls-files"],  # noqa: S607
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()
    assert [n for n in tracked if Path(n).name.startswith(".env") and n != ".env.example"] == []
    assert not [n for n in tracked if n.endswith("secrets.toml")]


def test_the_example_settings_hold_no_secret_value() -> None:
    example = local_secrets(ROOT / ".env.example")

    assert example == {}


# --- the scan finds what it says --------------------------------------------------------


@pytest.mark.parametrize("kind", sorted(PATTERNS))
def test_each_kind_of_secret_is_found_by_its_shape(kind: str) -> None:
    assert kinds_in(f"some text {EXAMPLES[kind]} more text") == [kind]


def test_ordinary_text_is_not_mistaken_for_a_secret() -> None:
    text = (ROOT / "docs" / "audit.md").read_text(encoding="utf-8")
    # Empty settings, as in .env.example, and prefixes too short to be keys.
    text += "\n" + "APP_JWT_SECRET_KEY" + "=\n# " + "APP_LLM_API_KEY" + "=\nsk-short gsk_short\n"

    assert kinds_in(text) == []


def test_a_secret_committed_and_then_deleted_is_still_found_in_the_history(
    repository: Path,
) -> None:
    (repository / "settings.py").write_text(f'KEY = "{EXAMPLES["Groq API key"]}"\n', "utf-8")
    git(repository, "add", ".")
    git(repository, "commit", "-q", "-m", "oops")
    (repository / "settings.py").write_text("KEY = None\n", "utf-8")
    git(repository, "commit", "-q", "-am", "remove the key")

    assert scan(repository, secrets={}) == [Finding("history", "Groq API key")]


def test_a_local_secret_is_found_by_its_value_wherever_it_is(repository: Path) -> None:
    signing_key = "nothing-about-this-looks-like-a-key-" + "7f3a9c" * 4
    setting = "APP_JWT" + "_SECRET_KEY"
    (repository / ".env").write_text(f"{setting}={signing_key}\n", "utf-8")
    (repository / "notes.md").write_text(f"The key is {signing_key}, keep it safe.\n", "utf-8")
    git(repository, "add", "notes.md")

    findings = scan(repository)

    assert Finding("notes.md", "the value of APP_JWT_SECRET_KEY") in findings
    assert Finding("history", "the value of APP_JWT_SECRET_KEY") not in findings
    assert all(signing_key not in str(finding) for finding in findings)


def test_the_command_names_what_it_found_and_never_prints_it(
    repository: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    key = EXAMPLES["Groq API key"]
    (repository / "config.toml").write_text(f'api_key = "{key}"\n', "utf-8")
    git(repository, "add", ".")
    git(repository, "commit", "-q", "-m", "config")
    monkeypatch.setattr(scan_secrets, "ROOT", repository)

    assert scan_secrets.main([]) == 1

    out = capsys.readouterr().out
    assert "FOUND config.toml: Groq API key" in out
    assert "FOUND history: Groq API key" in out
    assert key not in out


def test_outside_a_checkout_the_command_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(scan_secrets, "ROOT", tmp_path)

    assert scan_secrets.main([]) == scan_secrets.NOT_A_CHECKOUT


# --- the model's key in use -------------------------------------------------------------

KEY = "provider" + "-key-" + "5d8e" * 8
PROMPT = Prompt(
    version=PROMPT_VERSION, system=INSTRUCTIONS, user="<question>Hi</question>", labels=()
)


def answer(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "model": "m",
            "choices": [{"message": {"content": "Hello."}, "finish_reason": "stop"}],
        },
    )


def echo_status(status: int) -> Callable[[httpx.Request], httpx.Response]:
    # A provider whose error body quotes the key it was sent. httpx hides the
    # header when the headers are printed, so the value is taken out of it.
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text=f"refused: {request.headers['Authorization']}")

    return respond


def raising(error: type[Exception]) -> Callable[[httpx.Request], httpx.Response]:
    def respond(request: httpx.Request) -> httpx.Response:
        sent = request.headers["Authorization"]
        raise error(f"failed sending {sent}", request=request)  # type: ignore[call-arg]

    return respond


@pytest.mark.parametrize(
    "server",
    [
        answer,
        echo_status(401),
        echo_status(429),
        echo_status(500),
        raising(httpx.ConnectTimeout),
        raising(httpx.ConnectError),
    ],
    ids=["answered", "401", "429", "500", "timeout", "unreachable"],
)
def test_the_models_key_goes_in_its_header_and_nowhere_else(
    caplog: pytest.LogCaptureFixture, server: Callable[[httpx.Request], httpx.Response]
) -> None:
    sent: list[httpx.Request] = []

    def recording(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return server(request)

    provider = OpenAICompatibleProvider(
        name="groq",
        base_url="https://api.groq.com/openai/v1",
        model="m",
        context_tokens=131_072,
        api_key=KEY,
        client=httpx.Client(transport=httpx.MockTransport(recording)),
    )
    # Every logger at DEBUG, the HTTP libraries' too, whatever the service sets.
    with caplog.at_level(logging.DEBUG):
        for library in ("httpx", "httpcore"):
            caplog.set_level(logging.DEBUG, logger=library)
        try:
            outcome = repr(provider.complete(PROMPT, max_output_tokens=100, timeout_seconds=1))
        except (LLMError, LLMUnavailableError) as error:
            # What a log would get: the error and its traceback, as Python prints them.
            outcome = "".join(traceback.format_exception(error))

    assert sent[0].headers["Authorization"] == f"Bearer {KEY}"
    assert KEY not in outcome
    assert KEY not in everything_logged(caplog)
    assert KEY not in repr(provider.__dict__.get("_url"))

"""Calling the language model: what is sent, retries, failures and logs."""

import logging

import pytest
from pydantic import SecretStr, ValidationError

from app.core.config import Settings
from app.core.exceptions import ServiceUnavailableError, error_response
from app.rag.llm import (
    FIRST_PAUSE,
    Completion,
    LLMError,
    LLMUnavailableError,
    StopReason,
    complete,
)
from app.rag.prompt import INSTRUCTIONS, PROMPT_VERSION, Prompt
from fakes import FakeLLM
from logcheck import everything_logged

SECRET_ANSWER = "The hotel limit is 160 EUR per night [S1]."
PROMPT = Prompt(
    version=PROMPT_VERSION,
    system=INSTRUCTIONS,
    user='<source id="S1" document="Travel and Expense Policy" version="1">\n'
    "The hotel limit is 160 EUR per night.\n</source>\n\n"
    "<question>\nWhat is the hotel limit?\n</question>",
    labels=("S1",),
)


def make_settings(**changes: object) -> Settings:
    return Settings(_env_file=None, jwt_secret_key=SecretStr("x" * 64), **changes)  # type: ignore[arg-type]


class Pauses:
    def __init__(self) -> None:
        self.taken: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.taken.append(seconds)


@pytest.fixture
def settings() -> Settings:
    return make_settings()


@pytest.fixture
def pauses() -> Pauses:
    return Pauses()


# --- a normal call ----------------------------------------------------------------------


def test_the_answer_comes_back(settings: Settings, pauses: Pauses) -> None:
    llm = FakeLLM([SECRET_ANSWER])

    completion = complete(llm, PROMPT, settings, sleep=pauses)

    assert completion.text == SECRET_ANSWER
    assert completion.model == "fake-model"
    assert completion.stop_reason is StopReason.END
    assert pauses.taken == []


def test_the_provider_receives_exactly_the_prompt(settings: Settings) -> None:
    llm = FakeLLM()

    complete(llm, PROMPT, settings)

    assert llm.prompts == [PROMPT]
    assert llm.prompts[0] is PROMPT


def test_every_call_has_the_timeout_and_the_length_cap(settings: Settings) -> None:
    llm = FakeLLM()

    complete(llm, PROMPT, make_settings(llm_max_output_tokens=300, llm_timeout_seconds=12.5))

    assert llm.calls == [{"max_output_tokens": 300, "timeout_seconds": 12.5}]


def test_only_a_prompt_can_be_sent(settings: Settings) -> None:
    # A plain string could hold anything; a Prompt is only built from an
    # authorized context.
    llm = FakeLLM()

    with pytest.raises(TypeError):
        complete(llm, PROMPT.user, settings)  # type: ignore[arg-type]
    assert llm.prompts == []


def test_whitespace_around_the_answer_is_removed(settings: Settings) -> None:
    assert complete(FakeLLM(["\n  Yes [S1].  \n"]), PROMPT, settings).text == "Yes [S1]."


def test_an_answer_cut_off_at_the_cap_says_so(settings: Settings) -> None:
    cut = Completion("The hotel limit is", "fake-model", StopReason.MAX_TOKENS, 100, 600)

    completion = complete(FakeLLM([cut]), PROMPT, settings)

    assert completion.stop_reason is StopReason.MAX_TOKENS


# --- failures worth retrying ------------------------------------------------------------


def test_a_provider_that_recovers_is_retried(settings: Settings, pauses: Pauses) -> None:
    llm = FakeLLM([LLMUnavailableError("timeout"), LLMUnavailableError("503"), SECRET_ANSWER])

    completion = complete(llm, PROMPT, settings, sleep=pauses)

    assert completion.text == SECRET_ANSWER
    assert len(llm.prompts) == 3
    assert pauses.taken == [FIRST_PAUSE, FIRST_PAUSE * 2]


def test_a_provider_that_stays_down_fails_after_the_retries(
    settings: Settings, pauses: Pauses
) -> None:
    llm = FakeLLM([LLMUnavailableError("down")])

    with pytest.raises(LLMUnavailableError, match="after 3 attempts"):
        complete(llm, PROMPT, settings, sleep=pauses)

    assert len(llm.prompts) == settings.llm_max_retries + 1 == 3
    # No pause after the last attempt.
    assert pauses.taken == [0.5, 1.0]


def test_with_no_retries_there_is_one_attempt(pauses: Pauses) -> None:
    llm = FakeLLM([LLMUnavailableError("down")])

    with pytest.raises(LLMUnavailableError):
        complete(llm, PROMPT, make_settings(llm_max_retries=0), sleep=pauses)

    assert len(llm.prompts) == 1
    assert pauses.taken == []


# --- failures not worth retrying --------------------------------------------------------


def test_a_rejected_request_is_not_retried(settings: Settings, pauses: Pauses) -> None:
    llm = FakeLLM([LLMError("invalid api key"), SECRET_ANSWER])

    with pytest.raises(LLMError) as raised:
        complete(llm, PROMPT, settings, sleep=pauses)

    assert not isinstance(raised.value, LLMUnavailableError)
    assert len(llm.prompts) == 1
    assert pauses.taken == []


def test_an_unexpected_failure_is_not_retried_and_its_message_is_dropped(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    # Provider libraries sometimes put the request into their error messages.
    leaky = RuntimeError(f"bad request: {PROMPT.user}")
    llm = FakeLLM([leaky])

    with caplog.at_level(logging.INFO), pytest.raises(LLMError) as raised:
        complete(llm, PROMPT, settings)

    assert len(llm.prompts) == 1
    assert "160 EUR" not in str(raised.value)
    assert raised.value.__cause__ is None
    assert "160 EUR" not in everything_logged(caplog)
    assert "RuntimeError" in everything_logged(caplog)


@pytest.mark.parametrize("text", ["", "   ", "\n\n"])
def test_an_empty_answer_is_a_failure(settings: Settings, text: str) -> None:
    with pytest.raises(LLMError, match="empty"):
        complete(FakeLLM([text]), PROMPT, settings)


def test_an_answer_longer_than_the_cap_is_a_failure(settings: Settings) -> None:
    too_long = Completion("x" * 10, "fake-model", StopReason.END, 100, 601)

    with pytest.raises(LLMError, match="length cap"):
        complete(FakeLLM([too_long]), PROMPT, settings)


def test_a_provider_that_reports_no_token_counts_is_accepted(settings: Settings) -> None:
    quiet = Completion("Yes [S1].", "fake-model", StopReason.END, None, None)

    assert complete(FakeLLM([quiet]), PROMPT, settings).output_tokens is None


# --- what the client sees ---------------------------------------------------------------


@pytest.mark.parametrize("error", [LLMError("x"), LLMUnavailableError("y")])
def test_a_model_failure_is_service_unavailable_with_a_fixed_message(error: LLMError) -> None:
    status, body = error_response(error)

    assert isinstance(error, ServiceUnavailableError)
    assert status == 503
    assert body["error"]["message"] == "The service is temporarily unavailable."
    assert "x" not in body["error"]["message"].split()


# --- logs -------------------------------------------------------------------------------


def test_a_call_logs_cost_and_timing_but_no_text(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO):
        complete(FakeLLM([SECRET_ANSWER]), PROMPT, settings)

    record = next(r for r in caplog.records if r.message == "model answered")
    assert record.provider == "fake"  # type: ignore[attr-defined]
    assert record.model == "fake-model"  # type: ignore[attr-defined]
    assert record.attempts == 1  # type: ignore[attr-defined]
    assert record.prompt_version == PROMPT_VERSION  # type: ignore[attr-defined]
    assert record.input_tokens > 0  # type: ignore[attr-defined]
    assert isinstance(record.milliseconds, int)  # type: ignore[attr-defined]
    logged = everything_logged(caplog)
    assert "160 EUR" not in logged
    assert "hotel limit" not in logged.lower()
    assert "Use only the information" not in logged


def test_retries_are_logged_without_text(
    settings: Settings, pauses: Pauses, caplog: pytest.LogCaptureFixture
) -> None:
    llm = FakeLLM([LLMUnavailableError(f"timeout while sending {PROMPT.user}"), SECRET_ANSWER])

    with caplog.at_level(logging.INFO):
        complete(llm, PROMPT, settings, sleep=pauses)

    failures = [r for r in caplog.records if r.message == "model call failed"]
    assert [r.attempt for r in failures] == [1]  # type: ignore[attr-defined]
    assert "160 EUR" not in everything_logged(caplog)


# --- settings ---------------------------------------------------------------------------


def test_the_defaults() -> None:
    settings = make_settings()

    assert settings.llm_max_output_tokens == 600
    assert settings.llm_timeout_seconds == 30.0
    assert settings.llm_max_retries == 2


@pytest.mark.parametrize(
    "changes",
    [
        {"llm_max_output_tokens": 10},
        {"llm_max_output_tokens": 5000},
        {"llm_timeout_seconds": 0},
        {"llm_timeout_seconds": 600},
        {"llm_max_retries": -1},
        {"llm_max_retries": 10},
    ],
)
def test_bad_settings_are_refused(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        make_settings(**changes)

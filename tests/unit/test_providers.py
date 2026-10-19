"""The OpenAI-compatible provider, against a fake HTTP server."""

import json
import logging
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from app.core.config import Settings
from app.rag.llm import LLMError, LLMUnavailableError, StopReason, complete
from app.rag.prompt import INSTRUCTIONS, PROMPT_VERSION, Prompt
from app.rag.providers import (
    CHARACTERS_PER_TOKEN,
    PRESETS,
    OpenAICompatibleProvider,
    load_provider,
)
from logcheck import everything_logged

SOURCE_TEXT = "The hotel limit is 160 EUR per night."
PROMPT = Prompt(
    version=PROMPT_VERSION,
    system=INSTRUCTIONS,
    user=f'<source id="S1" document="Travel and Expense Policy" version="1">\n{SOURCE_TEXT}\n'
    "</source>\n\n<question>\nWhat is the hotel limit?\n</question>",
    labels=("S1",),
)


def reply(
    text: str | None = "160 EUR per night [S1].",
    finish: str = "stop",
    usage: dict[str, Any] | None = None,
    model: str = "llama3.1",
) -> dict[str, Any]:
    return {
        "model": model,
        "choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": finish}],
        "usage": usage if usage is not None else {"prompt_tokens": 120, "completion_tokens": 9},
    }


class Server:
    """Records requests and answers them with whatever the test says."""

    def __init__(self, answer: Callable[[httpx.Request], httpx.Response]) -> None:
        self.answer = answer
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.answer(request)

    def body(self, index: int = 0) -> dict[str, Any]:
        result: dict[str, Any] = json.loads(self.requests[index].content)
        return result


def provider(
    server: Server, api_key: str | None = None, context_tokens: int = 4096
) -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        name="ollama",
        base_url="http://localhost:11434/v1/",
        model="llama3.1",
        context_tokens=context_tokens,
        api_key=api_key,
        client=httpx.Client(transport=httpx.MockTransport(server)),
    )


def ok(body: dict[str, Any]) -> Callable[[httpx.Request], httpx.Response]:
    return lambda _: httpx.Response(200, json=body)


def ask(llm: OpenAICompatibleProvider, timeout: float = 30.0) -> Any:
    return llm.complete(PROMPT, max_output_tokens=600, timeout_seconds=timeout)


def make_settings(**changes: object) -> Settings:
    return Settings(_env_file=None, jwt_secret_key=SecretStr("x" * 64), **changes)  # type: ignore[arg-type]


# --- the request ------------------------------------------------------------------------


def test_the_request_is_a_chat_completion_with_exactly_the_prompt() -> None:
    server = Server(ok(reply()))

    ask(provider(server))

    request = server.requests[0]
    assert request.method == "POST"
    assert str(request.url) == "http://localhost:11434/v1/chat/completions"
    assert server.body() == {
        "model": "llama3.1",
        "messages": [
            {"role": "system", "content": PROMPT.system},
            {"role": "user", "content": PROMPT.user},
        ],
        "max_tokens": 600,
        "temperature": 0,
        "stream": False,
    }


def test_the_timeout_is_sent_with_the_request() -> None:
    server = Server(ok(reply()))

    ask(provider(server), timeout=12.5)

    assert server.requests[0].extensions["timeout"] == {
        "connect": 12.5,
        "read": 12.5,
        "write": 12.5,
        "pool": 12.5,
    }


def test_a_key_is_sent_only_when_there_is_one() -> None:
    with_key, without = Server(ok(reply())), Server(ok(reply()))

    ask(provider(with_key, api_key="gsk_secret"))
    ask(provider(without))

    assert with_key.requests[0].headers["Authorization"] == "Bearer gsk_secret"
    assert "Authorization" not in without.requests[0].headers


# --- the reply --------------------------------------------------------------------------


def test_the_answer_and_its_details_are_read() -> None:
    server = Server(ok(reply(usage={"prompt_tokens": 512, "completion_tokens": 14})))

    completion = ask(provider(server))

    assert completion.text == "160 EUR per night [S1]."
    assert completion.model == "llama3.1"
    assert completion.stop_reason is StopReason.END
    assert (completion.input_tokens, completion.output_tokens) == (512, 14)


@pytest.mark.parametrize(
    ("finish", "reason"),
    [
        ("stop", StopReason.END),
        ("length", StopReason.MAX_TOKENS),
        ("content_filter", StopReason.REFUSED),
        ("tool_calls", StopReason.OTHER),
        (None, StopReason.OTHER),
    ],
)
def test_why_the_answer_stopped(finish: str, reason: StopReason) -> None:
    assert ask(provider(Server(ok(reply(finish=finish))))).stop_reason is reason


@pytest.mark.parametrize("content", [None, ""])
def test_a_filtered_reply_with_no_content_is_a_refusal(content: str | None) -> None:
    completion = ask(provider(Server(ok(reply(text=content, finish="content_filter")))))

    assert completion.stop_reason is StopReason.REFUSED
    assert completion.text == ""


def test_no_content_is_only_accepted_from_a_filtered_reply() -> None:
    with pytest.raises(LLMError, match="expected form"):
        ask(provider(Server(ok(reply(text=None, finish="stop")))))


@pytest.mark.parametrize(
    "usage",
    [
        {},
        {"prompt_tokens": None},
        {"prompt_tokens": -3},
        {"prompt_tokens": True},
        {"prompt_tokens": "12"},
    ],
)
def test_odd_token_counts_are_treated_as_unknown(usage: dict[str, Any]) -> None:
    completion = ask(provider(Server(ok(reply(usage=usage)))))

    assert completion.input_tokens is None


def test_a_reply_without_usage_is_accepted() -> None:
    body = reply()
    del body["usage"]

    assert ask(provider(Server(ok(body)))).output_tokens is None


def test_the_model_that_answered_is_recorded() -> None:
    completion = ask(provider(Server(ok(reply(model="llama3.1:8b-instruct-q4_K_M")))))

    assert completion.model == "llama3.1:8b-instruct-q4_K_M"


@pytest.mark.parametrize(
    "body",
    [
        {"choices": []},
        {"choices": [{"message": {}}]},
        {"choices": [{"message": {"content": None}}]},
        {"choices": [{"message": {"content": ["not", "text"]}}]},
        {"unexpected": True},
    ],
)
def test_a_reply_in_the_wrong_form_is_a_failure(body: dict[str, Any]) -> None:
    with pytest.raises(LLMError, match="expected form") as raised:
        ask(provider(Server(ok(body))))

    assert not isinstance(raised.value, LLMUnavailableError)


def test_a_reply_that_is_not_json_is_a_failure() -> None:
    server = Server(lambda _: httpx.Response(200, text="<html>gateway</html>"))

    with pytest.raises(LLMError, match="expected form"):
        ask(provider(server))


# --- errors -----------------------------------------------------------------------------


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_errors_the_provider_may_recover_from(status: int) -> None:
    server = Server(lambda _: httpx.Response(status, json={"error": "busy"}))

    with pytest.raises(LLMUnavailableError, match=str(status)):
        ask(provider(server))


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_errors_it_will_not_recover_from(status: int) -> None:
    server = Server(lambda _: httpx.Response(status, json={"error": "no"}))

    with pytest.raises(LLMError) as raised:
        ask(provider(server))

    assert not isinstance(raised.value, LLMUnavailableError)


def test_an_error_body_that_echoes_the_request_is_not_read() -> None:
    echo = Server(lambda r: httpx.Response(400, json={"error": f"bad request: {r.content!r}"}))

    with pytest.raises(LLMError) as raised:
        ask(provider(echo))

    assert "160 EUR" not in str(raised.value)
    assert str(raised.value) == "ollama answered 400"


def test_a_timeout_is_worth_retrying() -> None:
    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    with pytest.raises(LLMUnavailableError, match="timed out"):
        ask(provider(Server(slow)))


def test_a_provider_that_is_not_running_is_worth_retrying() -> None:
    # As when Ollama hasn't been started.
    def refused(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(LLMUnavailableError, match="could not be reached"):
        ask(provider(Server(refused)))


def test_through_complete_a_provider_that_recovers_is_retried() -> None:
    answers = iter([httpx.Response(503), httpx.Response(503), httpx.Response(200, json=reply())])
    server = Server(lambda _: next(answers))

    completion = complete(provider(server), PROMPT, make_settings(), sleep=lambda _: None)

    assert completion.text == "160 EUR per night [S1]."
    assert len(server.requests) == 3


# --- a prompt too long for the model ----------------------------------------------------


def test_a_prompt_that_may_not_fit_is_not_sent() -> None:
    # Ollama would cut the start of the prompt, which is the instructions.
    server = Server(ok(reply()))
    size = (len(PROMPT.system) + len(PROMPT.user)) // CHARACTERS_PER_TOKEN + 1

    with pytest.raises(LLMError, match="more than the") as raised:
        ask(provider(server, context_tokens=size + 600 - 1))

    assert server.requests == []
    assert not isinstance(raised.value, LLMUnavailableError)


def test_the_estimate_is_the_one_measured_to_be_pessimistic() -> None:
    # The tests above work out sizes with the same constant as the code, so
    # they can't notice if it changes. Measured with llama3.1 on four real
    # prompts: 3.96 to 4.42 characters a token, lowest for the salary bands,
    # which are mostly numbers. 3 overestimates every one of them.
    # tests/integration/test_local_llm.py checks it against the real model.
    assert CHARACTERS_PER_TOKEN == 3


def test_a_prompt_that_fits_exactly_is_sent() -> None:
    server = Server(ok(reply()))
    size = (len(PROMPT.system) + len(PROMPT.user)) // CHARACTERS_PER_TOKEN + 1

    ask(provider(server, context_tokens=size + 600))

    assert len(server.requests) == 1


def test_the_largest_default_prompt_fits_ollamas_default_window() -> None:
    settings = make_settings()
    # The instructions, a full context and the longest question, all tags included.
    largest = (
        len(INSTRUCTIONS)
        + settings.context_max_characters
        + settings.max_question_characters
        + 5 * 120  # five source blocks' tags and titles
        + 30  # the question block's tags
    )

    tokens = largest // CHARACTERS_PER_TOKEN + 1 + settings.llm_max_output_tokens

    assert tokens <= PRESETS["ollama"].context_tokens


# --- choosing a provider ----------------------------------------------------------------


def test_closing_the_provider_closes_its_connections() -> None:
    client = httpx.Client(transport=httpx.MockTransport(Server(ok(reply()))))
    llm = OpenAICompatibleProvider("ollama", "http://x/v1", "m", 4096, client=client)

    llm.close()

    assert client.is_closed


def test_by_default_there_is_no_provider_and_nothing_is_sent() -> None:
    assert make_settings().llm_provider == "none"
    assert load_provider(make_settings()) is None


def test_ollama_uses_its_preset() -> None:
    llm = load_provider(make_settings(llm_provider="ollama"))

    assert isinstance(llm, OpenAICompatibleProvider)
    assert (llm.name, llm.model, llm.context_tokens) == ("ollama", "llama3.1", 4096)


def test_the_preset_can_be_overridden() -> None:
    llm = load_provider(
        make_settings(
            llm_provider="ollama",
            llm_model="qwen2.5:3b",
            llm_base_url="http://gpu-box:11434/v1",
            llm_context_tokens=8192,
        )
    )

    assert isinstance(llm, OpenAICompatibleProvider)
    assert (llm.model, llm.context_tokens) == ("qwen2.5:3b", 8192)


def test_groq_needs_a_key() -> None:
    with pytest.raises(ValidationError, match="groq needs llm_api_key"):
        make_settings(llm_provider="groq")


def test_groq_with_a_key(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO):
        llm = load_provider(make_settings(llm_provider="groq", llm_api_key=SecretStr("gsk_secret")))

    assert isinstance(llm, OpenAICompatibleProvider)
    assert llm.name == "groq"
    assert "gsk_secret" not in everything_logged(caplog)
    assert "gsk_secret" not in repr(
        make_settings(llm_provider="groq", llm_api_key=SecretStr("gsk_secret"))
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"llm_provider": "openai"},
        {"llm_model": ""},
        {"llm_context_tokens": 100},
        {"llm_timeout_seconds": 601},
    ],
)
def test_bad_provider_settings_are_refused(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        make_settings(**changes)

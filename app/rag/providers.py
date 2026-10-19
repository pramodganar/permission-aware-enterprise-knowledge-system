"""Language model providers that speak the OpenAI chat completions API.

Ollama (run locally), Groq and several others accept the same request, so one
class covers them and the choice is configuration:

- ``ollama``: the model runs on this machine. No document text leaves it,
  which is what SR-13 asks for. Slow on a laptop CPU: about a minute per
  answer was measured on the development machine, so it needs a long timeout.
- ``groq``: hosted and fast, with a free tier. The prompt, which is company
  text, is sent to Groq. Needs an API key.
- ``none``, the default: no provider. Questions get search results but no
  answers, and nothing is sent anywhere until someone chooses to.

Two things are checked here because a provider wouldn't report them:

- A prompt longer than the model's context window. Ollama cuts the start of
  such a prompt without an error, and the start is the instructions. A model
  without them answers from what it knows. So a prompt that might not fit,
  counted pessimistically at 3 characters a token, is not sent.
- Errors. A provider's error body can echo the request, so it is never read
  into a message; only the status code is.
"""

import logging
from dataclasses import dataclass
from typing import Any

import httpx

from app.core.config import Settings
from app.rag.llm import Completion, LLMError, LLMProvider, LLMUnavailableError, StopReason
from app.rag.prompt import Prompt

logger = logging.getLogger(__name__)

# Fewer characters per token than any text in the company documents, so the
# estimate of a prompt's size is too high rather than too low.
CHARACTERS_PER_TOKEN = 3


@dataclass(frozen=True)
class Preset:
    base_url: str
    model: str
    # The context window the model runs with, in tokens.
    context_tokens: int
    needs_key: bool


PRESETS = {
    # Ollama's default context for a loaded model, as reported by `ollama ps`.
    "ollama": Preset("http://localhost:11434/v1", "llama3.1", 4096, needs_key=False),
    "groq": Preset("https://api.groq.com/openai/v1", "openai/gpt-oss-20b", 131_072, needs_key=True),
}

_STOP_REASONS = {
    "stop": StopReason.END,
    "length": StopReason.MAX_TOKENS,
    "content_filter": StopReason.REFUSED,
}


class OpenAICompatibleProvider:
    def __init__(
        self,
        name: str,
        base_url: str,
        model: str,
        context_tokens: int,
        api_key: str | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self.name = name
        self.model = model
        self.context_tokens = context_tokens
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._api_key = api_key
        self._client = client or httpx.Client()

    def close(self) -> None:
        """Close the connections to the provider. Call once, when shutting down."""
        self._client.close()

    def complete(
        self, prompt: Prompt, *, max_output_tokens: int, timeout_seconds: float
    ) -> Completion:
        estimated = (len(prompt.system) + len(prompt.user)) // CHARACTERS_PER_TOKEN + 1
        if estimated + max_output_tokens > self.context_tokens:
            # Not retried: the same prompt won't fit the next time either.
            raise LLMError(
                f"the prompt may be about {estimated} tokens, and with {max_output_tokens} "
                f"for the answer that is more than the {self.context_tokens} {self.model} reads"
            )

        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": prompt.system},
                {"role": "user", "content": prompt.user},
            ],
            "max_tokens": max_output_tokens,
            # The same question and sources should give the same answer, so
            # answers can be compared and evaluated.
            "temperature": 0,
            "stream": False,
        }
        try:
            response = self._client.post(
                self._url, json=body, headers=headers, timeout=timeout_seconds
            )
        except httpx.TimeoutException:
            raise LLMUnavailableError(f"{self.name} timed out") from None
        except httpx.TransportError:
            raise LLMUnavailableError(f"{self.name} could not be reached") from None

        if response.status_code == 429 or response.status_code >= 500:
            raise LLMUnavailableError(f"{self.name} answered {response.status_code}")
        if response.status_code != 200:
            raise LLMError(f"{self.name} answered {response.status_code}")
        return _completion(response, self.model)


def _completion(response: httpx.Response, asked_for: str) -> Completion:
    try:
        data: Any = response.json()
        choice = data["choices"][0]
        text = choice["message"]["content"]
        if text is None and choice.get("finish_reason") == "content_filter":
            # A filtered reply can come without any content.
            text = ""
        usage = data.get("usage") or {}
        if not isinstance(text, str):
            raise TypeError
    except (ValueError, KeyError, IndexError, TypeError):
        raise LLMError("the provider's reply was not in the expected form") from None

    return Completion(
        text=text,
        model=str(data.get("model") or asked_for),
        stop_reason=_STOP_REASONS.get(choice.get("finish_reason"), StopReason.OTHER),
        input_tokens=_count(usage.get("prompt_tokens")),
        output_tokens=_count(usage.get("completion_tokens")),
    )


def _count(value: object) -> int | None:
    # bool is an int in Python; true is not a token count.
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def load_provider(settings: Settings) -> LLMProvider | None:
    """The provider named in the settings, or None if answers are switched off."""
    if settings.llm_provider == "none":
        return None
    preset = PRESETS[settings.llm_provider]
    key = settings.llm_api_key.get_secret_value() if settings.llm_api_key else None
    provider = OpenAICompatibleProvider(
        name=settings.llm_provider,
        base_url=settings.llm_base_url or preset.base_url,
        model=settings.llm_model or preset.model,
        context_tokens=settings.llm_context_tokens or preset.context_tokens,
        api_key=key,
    )
    logger.info(
        "language model provider chosen",
        extra={"provider": provider.name, "model": provider.model},
    )
    return provider

"""The language models a measurement can use.

``worst``  a stand-in that repeats everything it was given, the question
           included, and cites every source it was shown. It needs no
           network, gives the same answer every time, and is the hardest
           test of leakage: if even this model leaks nothing, it's because
           it was given nothing to leak.

``groq``   the real hosted model, through the service's own provider. The
           only setting taken from this machine is the API key, read from
           the environment or .env the way the service reads it, and never
           written anywhere.
"""

from typing import Literal

from pydantic import SecretStr

from app.core.config import Settings
from app.rag.llm import Completion, LLMProvider, StopReason
from app.rag.prompt import Prompt
from app.rag.providers import load_provider

ModelChoice = Literal["worst", "groq"]
# gpt-oss reasons before answering and the reasoning counts against the
# cap; docs/generation.md measured 1,000 as enough.
GROQ_MAX_OUTPUT_TOKENS = 1000


class RepeatEverything:
    """A model that does the worst a model could: hand back its whole context."""

    name = "worst"
    model = "repeat-everything"

    def complete(
        self, prompt: Prompt, *, max_output_tokens: int, timeout_seconds: float
    ) -> Completion:
        cited = " ".join(f"[{label}]" for label in prompt.labels)
        text = f"{prompt.user}\n{cited}"
        return Completion(
            text=text,
            model=self.model,
            stop_reason=StopReason.END,
            input_tokens=None,
            output_tokens=None,
        )


def groq_key() -> SecretStr:
    """The Groq key the service would use on this machine."""
    configured = Settings()
    if configured.llm_api_key is None:
        raise SystemExit("Set APP_LLM_API_KEY (in .env or the environment) to measure with Groq.")
    return configured.llm_api_key


def provider_for(choice: ModelChoice, settings: Settings) -> LLMProvider:
    if choice == "worst":
        return RepeatEverything()
    provider = load_provider(settings)
    if provider is None:
        raise ValueError("the settings name no language model")
    return provider

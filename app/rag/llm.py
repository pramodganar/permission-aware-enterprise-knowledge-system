"""Asking a language model, behind an interface the rest of the code depends on.

This is the one place where document text leaves the service (SR-13). What
leaves is a ``Prompt``, which is only ever built from a context of chunks the
person may read, and nothing else: ``complete`` refuses anything that isn't
a Prompt. Which provider receives it is configuration (decision record 0009).

Every call has a timeout and a cap on the answer's length. Failures the
provider may recover from (it is down, slow or rate-limiting) are retried a
few times with a growing pause; failures it won't recover from (a bad key, a
request it rejects) are not. Either way the question fails with "service
unavailable" and search keeps working (NFR-5). A provider's own error message
is never passed on: it can contain parts of the request.

The prompt and the answer are never logged. The model name, token counts and
timing are, so cost and latency can be followed.
"""

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from app.core.config import Settings
from app.core.exceptions import ServiceUnavailableError
from app.core.logging_setup import milliseconds_since
from app.rag.prompt import Prompt

logger = logging.getLogger(__name__)

# The first pause before a retry, in seconds. It doubles with each retry.
FIRST_PAUSE = 0.5


class LLMError(ServiceUnavailableError):
    """The model didn't give a usable answer. Not worth retrying."""


class LLMUnavailableError(LLMError):
    """The provider couldn't answer just now: down, too slow or rate-limiting."""


class StopReason(StrEnum):
    # The model finished its answer.
    END = "end"
    # The answer was cut off at the length cap.
    MAX_TOKENS = "max_tokens"
    # The provider's own safety system declined to answer.
    REFUSED = "refused"
    OTHER = "other"


@dataclass(frozen=True)
class Completion:
    text: str
    # The model that actually answered, which a provider may report more
    # exactly than it was asked for.
    model: str
    stop_reason: StopReason
    # As the provider counts them; None if it doesn't say.
    input_tokens: int | None
    output_tokens: int | None


class LLMProvider(Protocol):
    # Recorded with every answer.
    name: str
    model: str

    def complete(
        self, prompt: Prompt, *, max_output_tokens: int, timeout_seconds: float
    ) -> Completion:
        """Send the prompt and return the answer.

        Raises LLMUnavailableError for failures worth retrying and LLMError
        for others.
        """
        ...


def complete(
    provider: LLMProvider,
    prompt: Prompt,
    settings: Settings,
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> Completion:
    """Ask the provider, retrying failures it may recover from."""
    if not isinstance(prompt, Prompt):
        raise TypeError("only a Prompt built from an authorized context can be sent")

    attempts = settings.llm_max_retries + 1
    for attempt in range(1, attempts + 1):
        started = time.perf_counter()
        try:
            completion = provider.complete(
                prompt,
                max_output_tokens=settings.llm_max_output_tokens,
                timeout_seconds=settings.llm_timeout_seconds,
            )
        except LLMUnavailableError as error:
            _log_failure(provider, attempt, error)
            if attempt == attempts:
                raise LLMUnavailableError(
                    f"{provider.name} did not answer after {attempts} attempts"
                ) from None
            sleep(FIRST_PAUSE * 2 ** (attempt - 1))
            continue
        except LLMError as error:
            _log_failure(provider, attempt, error)
            raise LLMError(f"{provider.name} refused the request") from None
        except Exception as error:
            # A provider library failing in its own way. Its message may hold
            # part of the request, so only the type goes into the log.
            _log_failure(provider, attempt, error)
            raise LLMError(f"{provider.name} failed unexpectedly") from None

        checked = _checked(completion, settings)
        logger.info(
            "model answered",
            extra={
                "provider": provider.name,
                "model": checked.model,
                "attempts": attempt,
                "milliseconds": milliseconds_since(started),
                "input_tokens": checked.input_tokens,
                "output_tokens": checked.output_tokens,
                "stop_reason": checked.stop_reason.value,
                "prompt_version": prompt.version,
            },
        )
        return checked

    raise AssertionError("unreachable")  # pragma: no cover


def _checked(completion: Completion, settings: Settings) -> Completion:
    if not isinstance(completion.text, str):
        raise LLMError("the model returned an answer that is not text")
    # A refusal often comes with no text at all. It is still a reply: the
    # person is told there is no information, not that the service failed.
    if not completion.text.strip() and completion.stop_reason is not StopReason.REFUSED:
        raise LLMError("the model returned an empty answer")
    if (
        completion.output_tokens is not None
        and completion.output_tokens > settings.llm_max_output_tokens
    ):
        raise LLMError("the model returned more than the length cap")
    return Completion(
        text=completion.text.strip(),
        model=completion.model,
        stop_reason=completion.stop_reason,
        input_tokens=completion.input_tokens,
        output_tokens=completion.output_tokens,
    )


def _log_failure(provider: LLMProvider, attempt: int, error: Exception) -> None:
    logger.warning(
        "model call failed",
        extra={"provider": provider.name, "attempt": attempt, "error": type(error).__name__},
    )

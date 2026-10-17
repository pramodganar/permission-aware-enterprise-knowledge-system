"""The provider against a real local model. Run with: pytest -m llm

Needs Ollama running with llama3.1 pulled. Slow on a laptop CPU, about a
minute per answer, so there are only a few calls. Nothing is sent off the
machine. Skipped, not failed, when Ollama isn't running.
"""

from collections.abc import Iterator

import httpx
import pytest
from sqlalchemy import select

from app.models import User
from app.rag.answer import NO_INFORMATION_MESSAGE, Answer, NoAnswerReason, answer_question
from app.rag.context import build_context
from app.rag.llm import complete
from app.rag.prompt import Prompt, build_prompt
from app.rag.providers import CHARACTERS_PER_TOKEN, OpenAICompatibleProvider, load_provider
from app.retrieval.query import process_question
from questions import Company, real_company

pytestmark = pytest.mark.llm


@pytest.fixture(scope="module")
def acme() -> Iterator[Company]:
    try:
        httpx.get("http://localhost:11434/api/tags", timeout=5).raise_for_status()
    except httpx.HTTPError:
        pytest.skip("Ollama is not running")
    with real_company() as company:
        company.settings = company.settings.model_copy(
            update={"llm_provider": "ollama", "llm_timeout_seconds": 300.0, "llm_max_retries": 0}
        )
        yield company


@pytest.fixture(scope="module")
def llm(acme: Company) -> Iterator[OpenAICompatibleProvider]:
    provider = load_provider(acme.settings)
    assert isinstance(provider, OpenAICompatibleProvider)
    yield provider
    provider.close()


def prompt_for(acme: Company, name: str, question: str) -> Prompt:
    found = acme.ask(name, question)
    context = build_context(found, acme.settings.context_max_characters)
    return build_prompt(process_question(question, acme.settings, acme.embedder), context)


def test_a_real_answer_from_the_travel_policy(acme: Company, llm: OpenAICompatibleProvider) -> None:
    prompt = prompt_for(acme, "frank", "What is the hotel limit when travelling?")

    completion = complete(llm, prompt, acme.settings)

    # The policy says 160 EUR, and 220 EUR in high-cost cities.
    assert "160" in completion.text
    assert "[S" in completion.text
    assert completion.input_tokens is not None
    assert completion.output_tokens is not None


def test_the_size_estimate_is_never_below_the_real_token_count(
    acme: Company, llm: OpenAICompatibleProvider
) -> None:
    # The check that keeps Ollama from cutting the instructions off relies on
    # 3 characters a token being pessimistic for real prompts.
    prompt = prompt_for(acme, "grace", "What acquisitions is the company considering?")

    completion = complete(
        llm, prompt, acme.settings.model_copy(update={"llm_max_output_tokens": 50})
    )

    estimated = (len(prompt.system) + len(prompt.user)) // CHARACTERS_PER_TOKEN + 1
    assert completion.input_tokens is not None
    assert completion.input_tokens <= estimated


# --- whole answers ----------------------------------------------------------------------


def ask(acme: Company, llm: OpenAICompatibleProvider, name: str, question: str) -> Answer:
    user = acme.db.scalars(select(User).where(User.email == f"{name}@acmecorp.example")).one()
    return answer_question(acme.db, acme.index, acme.embedder, llm, user, question, acme.settings)


def test_an_answerable_question_is_answered(acme: Company, llm: OpenAICompatibleProvider) -> None:
    answer = ask(acme, llm, "alice", "How many days of annual leave do I get?")

    assert answer.answered
    assert "25" in answer.text
    assert not answer.cut_off
    # It cites the leave policy, by a label that was in its context.
    assert "Leave Policy" in {s.document_title for s in answer.citations}
    assert answer.unknown_citations == 0


@pytest.mark.parametrize(
    ("name", "question"),
    [
        # The runbook rolls back services. Nothing says how to roll back an
        # app, and the closest command is not the answer.
        ("alice", "How do I roll back a mobile app release?"),
        # David may read his own discount policy, which is also about approval
        # limits, but not the finance one that answers this.
        ("david", "What is the approval limit for vendor payments?"),
    ],
)
def test_a_related_but_unanswered_question_gets_no_information(
    acme: Company, llm: OpenAICompatibleProvider, name: str, question: str
) -> None:
    answer = ask(acme, llm, name, question)

    # It passed the relevance gate, so the model was asked, and said no.
    assert answer.context.sources
    assert answer.reason is NoAnswerReason.NOT_IN_SOURCES
    assert answer.text == NO_INFORMATION_MESSAGE

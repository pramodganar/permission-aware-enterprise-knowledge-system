"""The relevance threshold, checked with the real model. Run with: pytest -m model

The threshold was chosen from these measurements (docs/retrieval.md). These
tests keep it honest: if the model, the chunking or the documents change and
the threshold no longer separates the two groups, they fail.
"""

from collections.abc import Iterator

import pytest
from pydantic import SecretStr

from app.core.config import Settings
from app.retrieval.filters import SearchFilters
from questions import ANSWERABLE, OFF_TOPIC, Company, real_company

pytestmark = pytest.mark.model


@pytest.fixture(scope="module")
def acme() -> Iterator[Company]:
    # Seeded and embedded once for the module: embedding is the slow part.
    with real_company() as company:
        yield company


def test_the_threshold_is_the_one_measured() -> None:
    settings = Settings(_env_file=None, jwt_secret_key=SecretStr("x" * 64))

    assert settings.retrieval_min_score == 0.35


@pytest.mark.parametrize("question", list(ANSWERABLE))
def test_an_answerable_question_passes_the_gate(acme: Company, question: str) -> None:
    asker, _ = ANSWERABLE[question]
    retrieval = acme.ask(asker, question)

    assert not retrieval.below_threshold
    assert len(retrieval.chunks) == acme.settings.retrieval_top_k


@pytest.mark.parametrize("question", OFF_TOPIC)
def test_an_off_topic_question_is_stopped(acme: Company, question: str) -> None:
    retrieval = acme.ask("grace", question)

    assert retrieval.below_threshold
    assert retrieval.is_empty


def test_the_bonus_question_gives_david_nothing(acme: Company) -> None:
    # The incident the project starts from. Before the gate, David got five
    # weak chunks of his own documents; now he gets none, and the LLM
    # would not be called.
    retrieval = acme.ask("david", "How are executive bonuses calculated?")

    assert retrieval.is_empty
    assert retrieval.below_threshold


def test_carol_still_gets_the_case_file_that_ranks_below_the_threshold(acme: Company) -> None:
    # Why the gate is on the best chunk and not on each one.
    retrieval = acme.ask("carol", "Which employees are on a performance improvement plan?")

    case_file = [c for c in retrieval.chunks if c.document_title == "HR Case File 2026-014"]
    assert case_file
    assert all(c.score < acme.settings.retrieval_min_score for c in case_file)


def test_a_related_question_with_no_answer_is_not_stopped(acme: Company) -> None:
    # A known limit, kept as a test so it stays known. No document describes a
    # customer data retention design, but the contract retention obligations
    # are close enough in meaning to pass. The threshold can tell "about
    # nothing here" from "about something here", not "answered here".
    retrieval = acme.ask("emma", "What is our customer data retention design?")

    assert not retrieval.below_threshold
    assert retrieval.chunks[0].document_title == "Contract Data Retention Obligations"


def test_a_filter_does_not_push_carols_case_file_under_the_threshold(acme: Company) -> None:
    # Found with the real model: asked for restricted documents only, Carol
    # got nothing, because the case file's best chunk scores 0.347 and the
    # chunk that made the question relevant was filtered out. The gate now
    # looks at everything she may read.
    restricted = SearchFilters(classifications=frozenset({"restricted"}))

    retrieval = acme.ask(
        "carol", "Which employees are on a performance improvement plan?", filters=restricted
    )

    assert not retrieval.below_threshold
    assert {c.document_title for c in retrieval.chunks} == {"HR Case File 2026-014"}
    assert retrieval.chunks[0].score < acme.settings.retrieval_min_score

"""The relevance threshold, checked with the real model. Run with: pytest -m model

The threshold was chosen from these measurements (docs/retrieval.md). These
tests keep it honest: if the model, the chunking or the documents change and
the threshold no longer separates the two groups, they fail.
"""

from collections.abc import Iterator
from dataclasses import dataclass

import pytest
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.database import Base, create_db_engine, create_session_factory
from app.models import User
from app.retrieval.embeddings import Embedder, load_embedder
from app.retrieval.index import FaissVectorIndex, build_index
from app.retrieval.query import process_question
from app.retrieval.retriever import Retrieval, retrieve
from scripts.seed import seed

pytestmark = pytest.mark.model

# question: who asks it, someone who may read the document that answers it
ANSWERABLE = {
    "How does our deployment pipeline handle rollbacks?": "grace",
    "How many days of annual leave do I get?": "alice",
    "What is the approval limit for vendor payments?": "bob",
    "What was the root cause of the last production outage?": "alice",
    "What is the process for a performance improvement plan?": "carol",
    "What is next year's headcount budget?": "bob",
    "What discount can I offer without approval?": "david",
    "What is the status of the dispute with our supplier?": "emma",
    "How are executive bonuses calculated?": "grace",
    "What is our data retention obligation for contracts?": "emma",
    "What is the SOP for a warehouse incident?": "frank",
    "What is the company's three-year strategy?": "grace",
    "What are the salary bands for senior engineers?": "carol",
    "What acquisitions is the company considering?": "emma",
    "Which employees are on a performance improvement plan?": "carol",
    "What is the hotel limit when travelling?": "frank",
    "Can I accept a gift from a supplier?": "david",
    # Worded unlike the documents.
    "How long do I have to submit my receipts after a trip?": "alice",
    "Who signs off a large payment to a supplier?": "bob",
    "What happened when payments went down?": "alice",
    "How much parental leave is there?": "carol",
    "What should I do if someone is hurt in the warehouse?": "frank",
}

# Nothing in the company documents is about these. Asked by Grace, who can
# read the most, so the whole company had the chance to match.
OFF_TOPIC = [
    "What is the capital of France?",
    "Who won the football world cup in 2022?",
    "How do I bake sourdough bread?",
    "What is the weather tomorrow?",
    "Explain quantum entanglement simply.",
    "Who is the CEO's favourite actor?",
    "What is the office wifi password?",
    "When is the company picnic?",
    "How do I reset my laptop password?",
    "What is the share price today?",
    "Tell me a joke.",
]


@dataclass
class Company:
    db: Session
    settings: Settings
    embedder: Embedder
    index: FaissVectorIndex

    def ask(self, name: str, question: str) -> Retrieval:
        user = self.db.scalars(select(User).where(User.email == f"{name}@acmecorp.example")).one()
        return retrieve(
            self.db,
            self.index,
            user,
            process_question(question, self.settings, self.embedder),
            self.settings.retrieval_top_k,
            self.settings.retrieval_min_score,
        )


@pytest.fixture(scope="module")
def acme() -> Iterator[Company]:
    # Seeded and embedded once for the module: embedding is the slow part.
    settings = Settings(_env_file=None, jwt_secret_key=SecretStr("x" * 64))
    engine = create_db_engine("sqlite://")
    Base.metadata.create_all(engine)
    with create_session_factory(engine)() as db:
        seed(db, settings, "relevance-test-password")
        embedder = load_embedder(settings)
        yield Company(db, settings, embedder, build_index(db, embedder))
    engine.dispose()


def test_the_threshold_is_the_one_measured() -> None:
    settings = Settings(_env_file=None, jwt_secret_key=SecretStr("x" * 64))

    assert settings.retrieval_min_score == 0.35


@pytest.mark.parametrize("question", list(ANSWERABLE))
def test_an_answerable_question_passes_the_gate(acme: Company, question: str) -> None:
    retrieval = acme.ask(ANSWERABLE[question], question)

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

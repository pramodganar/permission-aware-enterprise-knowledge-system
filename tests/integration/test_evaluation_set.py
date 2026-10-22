"""The evaluation set is what it says, checked against the company as it is loaded.

A wrong label in an evaluation set is worse than none: a "forbidden" question
the asker can actually read would report a leak that isn't one, and a quote
that isn't in its document would make recall look worse than it is. So every
claim the set makes is checked here, against the documents' real text and
chunks, and against who may read what twice over: the table written from the
personas (tests/questions.py) and the service's own rule. If those two ever
disagree, that's a finding in itself.
"""

import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.authorization.service import can_read
from app.core.config import Settings
from app.core.database import Base, create_db_engine, create_session_factory
from app.models import Chunk, Document, DocumentStatus, User
from evaluation.dataset import Kind, Question, contains, default_set
from questions import MAY_READ
from scripts.seed import seed

PERSONAS = Path(__file__).resolve().parents[2] / "docs" / "personas.md"
QUESTIONS = default_set().questions
READERS = sorted(name for name, titles in MAY_READ.items() if titles)


@pytest.fixture(scope="module")
def company() -> Iterator[Session]:
    """The company loaded once for the whole file: every test here only reads."""
    engine = create_db_engine("sqlite://")
    Base.metadata.create_all(engine)
    settings = Settings(_env_file=None, jwt_secret_key=SecretStr("evaluation-set-" + "x" * 60))
    with create_session_factory(engine)() as db:
        seed(db, settings, "evaluation-set-password")
        yield db
    engine.dispose()


def person(db: Session, name: str) -> User:
    return db.scalars(select(User).where(User.email == f"{name}@acmecorp.example")).one()


def document(db: Session, title: str) -> Document:
    return db.scalars(
        select(Document).where(Document.title == title, Document.status == DocumentStatus.ACTIVE)
    ).one()


def chunks_of(db: Session, title: str) -> list[str]:
    return list(db.scalars(select(Chunk.text).where(Chunk.document_id == document(db, title).id)))


def by_kind(*kinds: Kind) -> list[Question]:
    return [q for q in QUESTIONS if q.kind in kinds]


def ids(questions: list[Question]) -> list[str]:
    return [q.id for q in questions]


# --- the shape of the set ---------------------------------------------------------------


def test_there_are_enough_of_each_kind_to_say_something() -> None:
    counts = default_set().counts()

    assert counts["answerable"] >= 30
    assert counts["related"] >= 10
    assert counts["forbidden"] >= 12
    assert counts["injection"] >= 6
    assert counts["off_topic"] >= 10


@pytest.mark.parametrize("name", READERS)
def test_everyone_who_reads_has_answerable_questions_of_their_own(name: str) -> None:
    # Quality is measured per person, so each needs enough of their own.
    assert len([q for q in by_kind(Kind.ANSWERABLE) if q.asker == name]) >= 4


def test_the_probes_cover_both_restricted_documents_and_most_departments() -> None:
    probed = {q.document for q in by_kind(Kind.FORBIDDEN, Kind.INJECTION)}

    # A restricted document is the hardest case: even an executive needs a grant.
    assert {"HR Case File 2026-014", "Project Falcon Acquisition Paper"} <= probed
    assert len(probed) >= 10


def personas_lines(start: str) -> list[tuple[str, str]]:
    """(person, question) for every line of docs/personas.md beginning with ``start``."""
    found: list[tuple[str, str]] = []
    who = ""
    for line in PERSONAS.read_text(encoding="utf-8").splitlines():
        heading = re.match(r"^### (\w+),", line)
        if heading:
            who = heading.group(1).lower()
        quoted = re.match(rf'^- {start}: "([^"]+)"', line)
        if quoted:
            found.append((who, quoted.group(1)))
    return found


def test_every_question_the_personas_must_be_refused_is_in_the_set() -> None:
    refused = personas_lines("Must be refused")
    asked = {(q.asker, q.question) for q in QUESTIONS}

    assert len(refused) >= 12
    assert set(refused) <= asked


def test_every_question_the_personas_should_have_answered_is_in_the_set() -> None:
    answered = personas_lines("Should get an answer")
    asked = {(q.asker, q.question) for q in by_kind(Kind.ANSWERABLE)}

    assert len(answered) >= 7
    assert set(answered) <= asked


# --- every claim, against the company as loaded -----------------------------------------


@pytest.mark.parametrize(
    "question", [q for q in QUESTIONS if q.document], ids=ids([q for q in QUESTIONS if q.document])
)
def test_the_document_named_exists(company: Session, question: Question) -> None:
    assert question.document is not None
    document(company, question.document)


@pytest.mark.parametrize("question", by_kind(Kind.ANSWERABLE), ids=ids(by_kind(Kind.ANSWERABLE)))
def test_every_quote_is_in_its_document_and_whole_in_one_of_its_chunks(
    company: Session, question: Question
) -> None:
    assert question.document is not None
    text = document(company, question.document).content
    chunks = chunks_of(company, question.document)

    for quote in question.evidence:
        assert contains(text, quote), quote
        # A quote split across two chunks would leave nothing relevant to find.
        assert any(contains(chunk, quote) for chunk in chunks), quote


SHOULD_READ = by_kind(Kind.ANSWERABLE, Kind.RELATED)


@pytest.mark.parametrize("question", SHOULD_READ, ids=ids(SHOULD_READ))
def test_the_asker_may_read_the_document_by_both_tables(
    company: Session, question: Question
) -> None:
    assert question.document is not None
    assert question.document in MAY_READ[question.asker]
    assert can_read(person(company, question.asker), document(company, question.document))


PROBES = by_kind(Kind.FORBIDDEN, Kind.INJECTION)


@pytest.mark.parametrize("question", PROBES, ids=ids(PROBES))
def test_the_asker_may_not_read_it_and_the_control_may_by_both_tables(
    company: Session, question: Question
) -> None:
    assert question.document is not None
    assert question.control is not None
    forbidden = document(company, question.document)

    assert question.document not in MAY_READ[question.asker]
    assert not can_read(person(company, question.asker), forbidden)
    assert question.document in MAY_READ[question.control]
    assert can_read(person(company, question.control), forbidden)


@pytest.mark.parametrize("question", PROBES, ids=ids(PROBES))
def test_every_secret_is_in_its_document_and_no_other(company: Session, question: Question) -> None:
    others = company.scalars(
        select(Document).where(
            Document.status == DocumentStatus.ACTIVE, Document.title != question.document
        )
    ).all()
    assert question.document is not None
    text = document(company, question.document).content

    for secret in question.secrets:
        assert contains(text, secret), secret
        assert not [d.title for d in others if contains(d.content, secret)], secret


@pytest.mark.parametrize("question", by_kind(Kind.OFF_TOPIC), ids=ids(by_kind(Kind.OFF_TOPIC)))
def test_an_off_topic_question_is_asked_by_someone_who_reads(question: Question) -> None:
    # Asked by someone who reads, so a refusal means nothing matched, not
    # that there was nothing to search.
    assert MAY_READ[question.asker]

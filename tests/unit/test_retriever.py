"""Retrieving the chunks a person may read, with a second check on each one."""

import dataclasses
import logging
from datetime import datetime

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.authorization import service
from app.core.config import Settings
from app.models import Chunk, Document, User
from app.retrieval import retriever as retriever_module
from app.retrieval.index import FaissVectorIndex, Hit, build_index
from app.retrieval.query import ProcessedQuestion, process_question
from app.retrieval.retriever import Retrieval, RetrievedChunk, retrieve
from app.retrieval.scope import AccessScope, access_scope
from fakes import FakeEmbedder
from logcheck import everything_logged
from scripts.seed import seed

READERS = ["alice", "bob", "carol", "david", "emma", "frank", "grace"]
QUESTIONS = [
    "How does our deployment pipeline handle rollbacks?",
    "What are the salary bands for senior engineers?",
    "Which employees are on a performance improvement plan?",
    "What acquisitions is the company considering?",
    "How are executive bonuses calculated?",
]
# The lowest possible cosine similarity, so the relevance gate lets everything
# through. The fake embedder's scores mean nothing; the gate has its own tests.
NO_GATE = -1.0


class Company:
    def __init__(self, db: Session, settings: Settings) -> None:
        self.db = db
        self.settings = settings
        self.embedder = FakeEmbedder()
        self.index: FaissVectorIndex = build_index(db, self.embedder)

    def user(self, name: str) -> User:
        return self.db.scalars(select(User).where(User.email == f"{name}@acmecorp.example")).one()

    def document(self, title: str) -> Document:
        return self.db.scalars(select(Document).where(Document.title == title)).one()

    def ask(self, text: str) -> ProcessedQuestion:
        return process_question(text, self.settings, self.embedder)

    def retrieve(
        self, name: str, text: str, limit: int = 10, min_score: float = NO_GATE
    ) -> Retrieval:
        return retrieve(self.db, self.index, self.user(name), self.ask(text), limit, min_score)


@pytest.fixture
def acme(db: Session, settings: Settings) -> Company:
    seed(db, settings, "retriever-test-password")
    return Company(db, settings)


def widen_scope(monkeypatch: pytest.MonkeyPatch, acme: Company) -> None:
    """Pretend the scope query has a bug that lets in every chunk of the company."""
    every_chunk = frozenset(acme.db.scalars(select(Chunk.id)))
    every_document = frozenset(acme.db.scalars(select(Document.id)))

    def too_wide(db: Session, user: User, now: datetime | None = None) -> AccessScope:
        scope = access_scope(db, user, now)
        return dataclasses.replace(scope, chunk_ids=every_chunk, document_ids=every_document)

    monkeypatch.setattr(retriever_module, "access_scope", too_wide)


# --- what comes back --------------------------------------------------------------------


@pytest.mark.parametrize("name", READERS)
def test_every_chunk_returned_is_one_the_person_may_read(acme: Company, name: str) -> None:
    user = acme.user(name)

    for text in QUESTIONS:
        retrieval = acme.retrieve(name, text)
        assert retrieval.chunks
        assert retrieval.dropped == 0
        for found in retrieval.chunks:
            assert service.can_read(user, acme.db.get_one(Document, found.document_id))


def test_chunks_come_best_first_up_to_the_limit(acme: Company) -> None:
    retrieval = acme.retrieve("grace", "What acquisitions is the company considering?", 7)

    scores = [found.score for found in retrieval.chunks]
    assert len(scores) == 7
    assert scores == sorted(scores, reverse=True)


def test_each_chunk_carries_its_text_and_what_is_needed_to_cite_it(acme: Company) -> None:
    retrieval = acme.retrieve("carol", "What are the salary bands for senior engineers?")

    for found in retrieval.chunks:
        chunk = acme.db.get_one(Chunk, found.chunk_id)
        assert found == RetrievedChunk(
            chunk_id=chunk.id,
            document_id=chunk.document_id,
            document_public_id=chunk.document.public_id,
            document_title=chunk.document.title,
            document_version=chunk.document.version,
            position=chunk.position,
            start_offset=chunk.start_offset,
            end_offset=chunk.end_offset,
            text=chunk.text,
            score=found.score,
        )


def test_the_text_comes_from_the_database_not_the_index(acme: Company) -> None:
    first = acme.retrieve("alice", QUESTIONS[0]).chunks[0]
    chunk = acme.db.get_one(Chunk, first.chunk_id)
    chunk.text = "corrected text"
    acme.db.commit()

    again = acme.retrieve("alice", QUESTIONS[0]).chunks[0]

    assert again.chunk_id == first.chunk_id
    assert again.text == "corrected text"


def test_the_retrieval_says_who_it_was_for(acme: Company) -> None:
    assert acme.retrieve("frank", QUESTIONS[0]).user_id == acme.user("frank").id


@pytest.mark.parametrize("name", ["henry", "irene"])
def test_someone_who_may_read_nothing_gets_an_empty_retrieval(acme: Company, name: str) -> None:
    retrieval = acme.retrieve(name, QUESTIONS[1])

    assert retrieval.is_empty
    assert retrieval.dropped == 0


def test_a_retrieval_cannot_be_changed(acme: Company) -> None:
    retrieval = acme.retrieve("david", QUESTIONS[0])

    with pytest.raises(dataclasses.FrozenInstanceError):
        retrieval.chunks = ()  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        retrieval.chunks[0].text = "changed"  # type: ignore[misc]


# --- the second check -------------------------------------------------------------------


def test_the_second_check_stops_a_scope_that_is_too_wide(
    acme: Company, monkeypatch: pytest.MonkeyPatch
) -> None:
    widen_scope(monkeypatch, acme)
    case_file = acme.document("HR Case File 2026-014")

    retrieval = acme.retrieve("david", "Which employees are on a performance improvement plan?", 50)

    assert retrieval.dropped > 0
    assert case_file.id not in {found.document_id for found in retrieval.chunks}
    david = acme.user("david")
    for found in retrieval.chunks:
        assert service.can_read(david, acme.db.get_one(Document, found.document_id))


def test_what_passes_the_second_check_is_still_returned(
    acme: Company, monkeypatch: pytest.MonkeyPatch
) -> None:
    expected = acme.retrieve("david", QUESTIONS[4], 50)
    widen_scope(monkeypatch, acme)

    retrieval = acme.retrieve("david", QUESTIONS[4], 50)

    # The forbidden chunks are removed and the rest of the answer goes on.
    assert retrieval.chunks
    assert {found.chunk_id for found in retrieval.chunks} <= {
        found.chunk_id for found in expected.chunks
    }
    assert len(retrieval.chunks) + retrieval.dropped == 50


def test_a_removed_chunk_is_logged_as_an_error_without_saying_which(
    acme: Company, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    widen_scope(monkeypatch, acme)

    with caplog.at_level(logging.INFO):
        retrieval = acme.retrieve("david", QUESTIONS[2], 50)

    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    assert errors[0].dropped == retrieval.dropped  # type: ignore[attr-defined]
    logged = everything_logged(caplog)
    assert "Case File" not in logged
    assert "performance improvement" not in logged.lower()


def test_the_second_check_uses_the_moment_of_the_scope(
    acme: Company, monkeypatch: pytest.MonkeyPatch
) -> None:
    moments: list[datetime | None] = []
    decided: list[datetime] = []
    real_can_read = service.can_read
    real_access_scope = access_scope

    def recording_can_read(user: User, document: Document, now: datetime | None = None) -> bool:
        moments.append(now)
        return real_can_read(user, document, now)

    def recording_scope(db: Session, user: User, now: datetime | None = None) -> AccessScope:
        scope = real_access_scope(db, user, now)
        decided.append(scope.decided_at)
        return scope

    monkeypatch.setattr(retriever_module, "can_read", recording_can_read)
    monkeypatch.setattr(retriever_module, "access_scope", recording_scope)

    acme.retrieve("emma", QUESTIONS[3], 10)

    assert len(moments) == 10
    assert set(moments) == set(decided)


def test_every_chunk_is_checked(acme: Company, monkeypatch: pytest.MonkeyPatch) -> None:
    checked: list[int] = []
    real_can_read = service.can_read

    def recording_can_read(user: User, document: Document, now: datetime | None = None) -> bool:
        checked.append(document.id)
        return real_can_read(user, document, now)

    monkeypatch.setattr(retriever_module, "can_read", recording_can_read)

    retrieval = acme.retrieve("grace", QUESTIONS[3], 12)

    assert len(checked) == 12
    assert checked == [found.document_id for found in retrieval.chunks]


def test_a_chunk_that_is_gone_from_the_database_is_dropped(
    acme: Company, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Found by the search, then removed before it was loaded.
    def search_finding_a_missing_chunk(*_: object, **__: object) -> list[Hit]:
        return [Hit(chunk_id=999_999, score=0.9)]

    monkeypatch.setattr(retriever_module, "search_in_scope", search_finding_a_missing_chunk)

    retrieval = acme.retrieve("alice", QUESTIONS[0])

    assert retrieval.is_empty
    assert retrieval.dropped == 1


# --- logging ----------------------------------------------------------------------------


def test_retrieval_logs_counts_only(acme: Company, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO):
        retrieval = acme.retrieve("carol", "What are the salary bands for senior engineers?", 5)

    record = next(r for r in caplog.records if r.message == "chunks retrieved")
    assert record.chunks == len(retrieval.chunks) == 5  # type: ignore[attr-defined]
    assert record.dropped == 0  # type: ignore[attr-defined]
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    logged = everything_logged(caplog)
    assert "Salary Bands" not in logged
    assert "salary bands" not in logged.lower()
    for found in retrieval.chunks:
        assert found.text[:40] not in logged


# --- the relevance gate -----------------------------------------------------------------


def test_below_the_threshold_nothing_is_returned(acme: Company) -> None:
    best = acme.retrieve("alice", QUESTIONS[0]).chunks[0].score

    retrieval = acme.retrieve("alice", QUESTIONS[0], min_score=best + 0.001)

    assert retrieval.is_empty
    assert retrieval.below_threshold
    assert retrieval.best_score == best


def test_a_best_score_equal_to_the_threshold_passes(acme: Company) -> None:
    best = acme.retrieve("alice", QUESTIONS[0]).chunks[0].score

    retrieval = acme.retrieve("alice", QUESTIONS[0], min_score=best)

    assert retrieval.chunks
    assert not retrieval.below_threshold


def test_the_gate_is_on_the_best_chunk_not_on_each_chunk(acme: Company) -> None:
    # A real answer can rank fifth with a score under the threshold, while
    # the best chunk is well above it. Measured in docs/retrieval.md.
    everything = acme.retrieve("grace", QUESTIONS[3], 5)
    best, weakest = everything.chunks[0].score, everything.chunks[-1].score
    assert weakest < best

    retrieval = acme.retrieve("grace", QUESTIONS[3], 5, min_score=(best + weakest) / 2)

    assert retrieval.chunks == everything.chunks
    assert retrieval.chunks[-1].score < (best + weakest) / 2


def test_the_gate_only_looks_at_chunks_the_person_may_read(
    acme: Company, monkeypatch: pytest.MonkeyPatch
) -> None:
    david = acme.user("david")
    scope = access_scope(acme.db, david)
    forbidden = acme.document("HR Case File 2026-014").chunks[0].id
    allowed = min(scope.chunk_ids)

    # A forbidden chunk with a high score, as a faulty scope would let through.
    def search(*_: object, **__: object) -> list[Hit]:
        return [Hit(chunk_id=forbidden, score=0.9), Hit(chunk_id=allowed, score=0.1)]

    monkeypatch.setattr(retriever_module, "search_in_scope", search)

    retrieval = acme.retrieve("david", QUESTIONS[2], min_score=0.5)

    # The 0.9 was someone else's document. David's best is 0.1, so no answer.
    assert retrieval.dropped == 1
    assert retrieval.best_score == 0.1
    assert retrieval.below_threshold
    assert retrieval.is_empty


def test_an_empty_scope_is_not_below_the_threshold(acme: Company) -> None:
    retrieval = acme.retrieve("henry", QUESTIONS[0], min_score=0.5)

    assert retrieval.is_empty
    assert retrieval.best_score is None
    assert not retrieval.below_threshold


def test_the_gate_is_logged_with_the_best_score(
    acme: Company, caplog: pytest.LogCaptureFixture
) -> None:
    best = acme.retrieve("carol", QUESTIONS[1]).chunks[0].score

    with caplog.at_level(logging.INFO):
        retrieval = acme.retrieve("carol", QUESTIONS[1], min_score=0.99)

    record = next(r for r in caplog.records if r.message == "chunks retrieved")
    assert record.below_threshold is True  # type: ignore[attr-defined]
    assert record.best_score == round(best, 3)  # type: ignore[attr-defined]
    assert record.chunks == 0  # type: ignore[attr-defined]
    assert retrieval.is_empty
    assert "salary" not in everything_logged(caplog).lower()

"""Searching inside one person's access scope."""

import logging
from collections.abc import Collection, Sequence
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.authorization.document_access import revoke_access
from app.core.config import Settings
from app.ingestion.pipeline import Upload, delete_document, ingest
from app.models import Chunk, Document, User
from app.retrieval.embeddings import Vectors
from app.retrieval.index import FaissVectorIndex, Hit, VectorIndexError, build_index
from app.retrieval.query import ProcessedQuestion, process_question
from app.retrieval.scope import AccessScope, access_scope
from app.retrieval.search import MAX_SCOPE_AGE, ScopeError, search_in_scope
from fakes import FakeEmbedder
from logcheck import everything_logged
from scripts.seed import seed

PEOPLE = ["alice", "bob", "carol", "david", "emma", "frank", "grace"]
QUESTIONS = [
    "How does our deployment pipeline handle rollbacks?",
    "What are the salary bands for senior engineers?",
    "Which employees are on a performance improvement plan?",
    "What acquisitions is the company considering?",
    "How many days of annual leave do I get?",
]


class SpyIndex:
    """The real index, with a record of every search it was asked to do."""

    def __init__(self, index: FaissVectorIndex) -> None:
        self.inner = index
        self.dimension = index.dimension
        self.searches: list[frozenset[int]] = []

    @property
    def size(self) -> int:
        return self.inner.size

    @property
    def chunk_ids(self) -> frozenset[int]:
        return self.inner.chunk_ids

    def add(self, chunk_ids: Sequence[int], vectors: Vectors) -> None:
        self.inner.add(chunk_ids, vectors)

    def search(self, query: Vectors, limit: int, allowed_ids: Collection[int]) -> list[Hit]:
        self.searches.append(frozenset(allowed_ids))
        return self.inner.search(query, limit, allowed_ids)


class LeakyIndex(SpyIndex):
    """An index with a fault: it adds a chunk from outside the allowed list."""

    def __init__(self, index: FaissVectorIndex, extra_chunk_id: int) -> None:
        super().__init__(index)
        self.extra_chunk_id = extra_chunk_id

    def search(self, query: Vectors, limit: int, allowed_ids: Collection[int]) -> list[Hit]:
        return [*super().search(query, limit, allowed_ids), Hit(self.extra_chunk_id, 0.99)]


class Company:
    def __init__(self, db: Session, settings: Settings) -> None:
        self.db = db
        self.settings = settings
        self.embedder = FakeEmbedder()
        self.index = SpyIndex(build_index(db, self.embedder))

    def user(self, name: str) -> User:
        return self.db.scalars(select(User).where(User.email == f"{name}@acmecorp.example")).one()

    def document(self, title: str) -> Document:
        return self.db.scalars(select(Document).where(Document.title == title)).one()

    def ask(self, text: str) -> ProcessedQuestion:
        return process_question(text, self.settings, self.embedder)

    def search(self, name: str, text: str, limit: int = 10) -> list[Hit]:
        scope = access_scope(self.db, self.user(name))
        return search_in_scope(self.index, scope, self.ask(text), limit)

    def document_of(self, hit: Hit) -> int:
        return self.db.get_one(Chunk, hit.chunk_id).document_id


@pytest.fixture
def acme(db: Session, settings: Settings) -> Company:
    seed(db, settings, "search-test-password")
    return Company(db, settings)


# --- results stay inside the scope ------------------------------------------------------


@pytest.mark.parametrize("name", PEOPLE)
def test_every_hit_is_in_the_persons_scope(acme: Company, name: str) -> None:
    scope = access_scope(acme.db, acme.user(name))

    for text in QUESTIONS:
        hits = search_in_scope(acme.index, scope, acme.ask(text), 10)
        assert hits
        assert all(scope.allows_chunk(hit.chunk_id) for hit in hits)


@pytest.mark.parametrize("name", PEOPLE)
def test_the_index_is_only_given_the_scope(acme: Company, name: str) -> None:
    scope = access_scope(acme.db, acme.user(name))

    search_in_scope(acme.index, scope, acme.ask(QUESTIONS[0]), 10)

    assert acme.index.searches == [scope.chunk_ids]


def test_hits_come_best_first(acme: Company) -> None:
    hits = acme.search("grace", "What acquisitions is the company considering?", 20)

    scores = [hit.score for hit in hits]
    assert scores == sorted(scores, reverse=True)


def test_chunks_outside_the_scope_do_not_take_places(acme: Company) -> None:
    # David reads 4 documents. The question matches the executive paper best,
    # and he still gets five results, all his own.
    hits = acme.search("david", "How are executive bonuses calculated?", 5)

    assert len(hits) == 5


def test_the_limit_is_still_checked(acme: Company) -> None:
    with pytest.raises(VectorIndexError):
        acme.search("alice", QUESTIONS[0], 0)


# --- an empty scope ---------------------------------------------------------------------


@pytest.mark.parametrize("name", ["henry", "irene"])
def test_an_empty_scope_finds_nothing_without_asking_the_index(acme: Company, name: str) -> None:
    assert acme.search(name, QUESTIONS[1]) == []
    assert acme.index.searches == []


# --- the scope must be fresh ------------------------------------------------------------


def test_a_scope_at_the_age_limit_can_still_be_used(acme: Company) -> None:
    scope = access_scope(acme.db, acme.user("alice"))

    hits = search_in_scope(
        acme.index, scope, acme.ask(QUESTIONS[0]), 5, now=scope.decided_at + MAX_SCOPE_AGE
    )

    assert hits


def test_an_old_scope_is_refused(acme: Company) -> None:
    scope = access_scope(acme.db, acme.user("alice"))
    later = scope.decided_at + MAX_SCOPE_AGE + timedelta(seconds=1)

    with pytest.raises(ScopeError, match="too old"):
        search_in_scope(acme.index, scope, acme.ask(QUESTIONS[0]), 5, now=later)
    assert acme.index.searches == []


def test_an_old_empty_scope_is_refused_too(acme: Company) -> None:
    scope = access_scope(acme.db, acme.user("henry"))
    later = scope.decided_at + MAX_SCOPE_AGE + timedelta(seconds=1)

    with pytest.raises(ScopeError):
        search_in_scope(acme.index, scope, acme.ask(QUESTIONS[0]), 5, now=later)


def test_a_scope_dated_in_the_future_is_refused(acme: Company) -> None:
    scope = access_scope(acme.db, acme.user("alice"))
    earlier = scope.decided_at - timedelta(seconds=1)

    with pytest.raises(ScopeError, match="future"):
        search_in_scope(acme.index, scope, acme.ask(QUESTIONS[0]), 5, now=earlier)


def test_a_revoked_grant_is_not_found_with_a_new_scope(acme: Company) -> None:
    question = "Which employees are on a performance improvement plan?"
    case_file = acme.document("HR Case File 2026-014")
    before = {acme.document_of(hit) for hit in acme.search("carol", question, 50)}

    revoke_access(acme.db, acme.user("henry"), case_file.id, "carol@acmecorp.example")
    after = {acme.document_of(hit) for hit in acme.search("carol", question, 50)}

    assert case_file.id in before
    assert case_file.id not in after


# --- a fault in the index ---------------------------------------------------------------


def test_a_hit_outside_the_scope_is_refused(acme: Company) -> None:
    scope = access_scope(acme.db, acme.user("david"))
    case_file = acme.document("HR Case File 2026-014")
    forbidden = case_file.chunks[0].id
    assert not scope.allows_chunk(forbidden)
    leaky = LeakyIndex(acme.index.inner, forbidden)

    with pytest.raises(ScopeError, match="outside the access scope"):
        search_in_scope(leaky, scope, acme.ask(QUESTIONS[2]), 5)


def test_a_refused_hit_is_not_logged(acme: Company, caplog: pytest.LogCaptureFixture) -> None:
    scope = access_scope(acme.db, acme.user("david"))
    forbidden = acme.document("HR Case File 2026-014").chunks[0]
    leaky = LeakyIndex(acme.index.inner, forbidden.id)

    with caplog.at_level(logging.INFO), pytest.raises(ScopeError):
        search_in_scope(leaky, scope, acme.ask(QUESTIONS[2]), 5)

    logged = everything_logged(caplog)
    assert "search returned chunks outside the scope" in logged
    assert forbidden.text[:40] not in logged


# --- the index and the database out of step ---------------------------------------------


def test_a_deleted_documents_chunks_are_not_found(acme: Company) -> None:
    runbook = acme.document("Deployment Runbook")
    delete_document(acme.db, acme.user("henry"), runbook.id)

    hits = acme.search("alice", "How does our deployment pipeline handle rollbacks?", 50)

    # Still in the index until it is rebuilt, but in nobody's scope.
    assert {chunk.id for chunk in runbook.chunks} <= acme.index.chunk_ids
    assert runbook.id not in {acme.document_of(hit) for hit in hits}


def test_a_document_not_yet_indexed_is_counted_but_not_searched(
    acme: Company, caplog: pytest.LogCaptureFixture
) -> None:
    upload = Upload(
        filename="oncall.txt",
        data=b"Who is on call this week?\n\nThe on-call rota is in the shared calendar.\n",
        title="On-call Rota",
        department_code="engineering",
        classification="departmental",
    )
    added = ingest(acme.db, acme.user("henry"), upload, acme.settings)
    new_chunks = {chunk.id for chunk in added.chunks}
    scope = access_scope(acme.db, acme.user("alice"))
    assert new_chunks <= scope.chunk_ids

    with caplog.at_level(logging.INFO):
        hits = search_in_scope(acme.index, scope, acme.ask("Who is on call this week?"), 50)

    assert new_chunks.isdisjoint(hit.chunk_id for hit in hits)
    assert acme.index.searches == [scope.chunk_ids - new_chunks]
    record = next(r for r in caplog.records if r.message == "search done")
    assert record.not_indexed == len(new_chunks)  # type: ignore[attr-defined]


# --- logging ----------------------------------------------------------------------------


def test_the_search_logs_counts_only(acme: Company, caplog: pytest.LogCaptureFixture) -> None:
    carol = acme.user("carol")
    question = "Which employees are on a performance improvement plan?"

    with caplog.at_level(logging.INFO):
        hits = acme.search("carol", question, 5)

    record = next(r for r in caplog.records if r.message == "search done")
    assert record.user_id == carol.id  # type: ignore[attr-defined]
    assert record.hits == len(hits) == 5  # type: ignore[attr-defined]
    assert record.not_indexed == 0  # type: ignore[attr-defined]
    logged = everything_logged(caplog)
    assert "performance improvement" not in logged.lower()
    assert "Case File" not in logged


def test_an_empty_scope_is_logged(acme: Company, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO):
        acme.search("irene", QUESTIONS[0])

    assert any(r.message == "search skipped, empty scope" for r in caplog.records)


def test_the_scope_type_is_what_the_search_takes() -> None:
    # The allowed list can only arrive as an AccessScope.
    annotations = search_in_scope.__annotations__

    assert annotations["scope"] is AccessScope
    assert "allowed_ids" not in annotations

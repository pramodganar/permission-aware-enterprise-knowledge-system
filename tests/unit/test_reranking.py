"""Reranking reorders the chunks a person may read, and does nothing else."""

import dataclasses
import logging
from datetime import datetime

import pytest
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.models import Chunk, Document, User
from app.retrieval import reranking
from app.retrieval import retriever as retriever_module
from app.retrieval.index import FaissVectorIndex, build_index
from app.retrieval.query import process_question
from app.retrieval.reranking import RerankError, checked_scores, load_reranker
from app.retrieval.retriever import Retrieval, retrieve
from app.retrieval.scope import AccessScope, access_scope
from fakes import FakeEmbedder, FakeReranker
from logcheck import everything_logged
from scripts.seed import seed

NO_GATE = -1.0
QUESTION = "What are the salary bands for senior engineers?"


class Company:
    def __init__(self, db: Session, settings: Settings) -> None:
        self.db = db
        self.settings = settings
        self.embedder = FakeEmbedder()
        self.index: FaissVectorIndex = build_index(db, self.embedder)

    def user(self, name: str) -> User:
        return self.db.scalars(select(User).where(User.email == f"{name}@acmecorp.example")).one()

    def retrieve(
        self,
        name: str,
        question: str = QUESTION,
        limit: int = 5,
        min_score: float = NO_GATE,
        reranker: FakeReranker | None = None,
        candidates: int | None = None,
    ) -> Retrieval:
        processed = process_question(question, self.settings, self.embedder)
        return retrieve(
            self.db,
            self.index,
            self.user(name),
            processed,
            limit,
            min_score,
            reranker=reranker,
            candidates=candidates,
        )


@pytest.fixture
def acme(db: Session, settings: Settings) -> Company:
    seed(db, settings, "reranking-test-password")
    return Company(db, settings)


def ids(retrieval: Retrieval) -> list[int]:
    return [chunk.chunk_id for chunk in retrieval.chunks]


# --- what reranking changes -------------------------------------------------------------


def test_the_reranker_decides_the_order(acme: Company) -> None:
    plain = acme.retrieve("carol")
    # Reverse the search's order.
    reranker = FakeReranker(scores=[1.0, 2.0, 3.0, 4.0, 5.0])

    reranked = acme.retrieve("carol", reranker=reranker)

    assert ids(reranked) == list(reversed(ids(plain)))
    assert [c.rerank_score for c in reranked.chunks] == [5.0, 4.0, 3.0, 2.0, 1.0]


def test_reranking_keeps_the_same_chunks_when_it_chooses_from_the_same_number(
    acme: Company,
) -> None:
    plain = acme.retrieve("grace", "What acquisitions is the company considering?")

    reranked = acme.retrieve(
        "grace", "What acquisitions is the company considering?", reranker=FakeReranker()
    )

    assert set(ids(reranked)) == set(ids(plain))


def test_with_more_candidates_the_best_by_the_reranker_are_returned(acme: Company) -> None:
    wide = acme.retrieve("grace", limit=12)
    # The chunk the search ranked last is the reranker's favourite.
    reranker = FakeReranker(scores=[float(i) for i in range(12)])

    reranked = acme.retrieve("grace", limit=3, reranker=reranker, candidates=12)

    assert ids(reranked) == list(reversed(ids(wide)))[:3]
    assert len(reranker.calls[0][1]) == 12


def test_equal_scores_keep_the_searchs_order(acme: Company) -> None:
    plain = acme.retrieve("frank")

    reranked = acme.retrieve("frank", reranker=FakeReranker(scores=[0.0] * 5))

    assert ids(reranked) == ids(plain)


def test_reranking_does_not_change_the_cosine_scores(acme: Company) -> None:
    plain = {c.chunk_id: c.score for c in acme.retrieve("bob").chunks}

    reranked = acme.retrieve("bob", reranker=FakeReranker())

    assert {c.chunk_id: c.score for c in reranked.chunks} == plain


def test_the_reranker_reads_the_cleaned_question_and_each_chunk_with_its_title(
    acme: Company,
) -> None:
    reranker = FakeReranker()

    reranked = acme.retrieve(
        "carol", "  What are the   salary bands\nfor senior engineers?", reranker=reranker
    )

    question, passages = reranker.calls[0]
    assert question == QUESTION
    by_id = {c.chunk_id: c for c in reranked.chunks}
    for passage in passages:
        assert any(passage == f"{c.document_title}\n{c.text}" for c in by_id.values())


def test_candidates_below_the_limit_are_refused(acme: Company) -> None:
    with pytest.raises(ValueError, match="candidates"):
        acme.retrieve("alice", limit=5, reranker=FakeReranker(), candidates=3)


def test_without_a_reranker_candidates_are_ignored(acme: Company) -> None:
    retrieval = acme.retrieve("grace", limit=3, candidates=20)

    assert len(retrieval.chunks) == 3
    assert all(c.rerank_score is None for c in retrieval.chunks)


# --- what the reranker may see ----------------------------------------------------------


def test_the_reranker_never_sees_a_chunk_the_person_may_not_read(
    acme: Company, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A scope query with a bug lets in the whole company. The second check
    # removes the rest before anything reaches the reranker.
    every_chunk = frozenset(acme.db.scalars(select(Chunk.id)))
    every_document = frozenset(acme.db.scalars(select(Document.id)))

    def too_wide(db: Session, user: User, now: datetime | None = None) -> AccessScope:
        scope = access_scope(db, user, now)
        return dataclasses.replace(scope, chunk_ids=every_chunk, document_ids=every_document)

    monkeypatch.setattr(retriever_module, "access_scope", too_wide)
    reranker = FakeReranker()
    case_file = acme.db.scalars(
        select(Document).where(Document.title == "HR Case File 2026-014")
    ).one()

    retrieval = acme.retrieve(
        "david",
        "Which employees are on a performance improvement plan?",
        limit=5,
        reranker=reranker,
        candidates=40,
    )

    assert retrieval.dropped > 0
    seen = "\n".join(p for _, passages in reranker.calls for p in passages)
    assert "HR Case File" not in seen
    for chunk in case_file.chunks:
        assert chunk.text not in seen


@pytest.mark.parametrize("name", ["henry", "irene"])
def test_the_reranker_is_not_called_for_an_empty_scope(acme: Company, name: str) -> None:
    reranker = FakeReranker()

    assert acme.retrieve(name, reranker=reranker).is_empty
    assert reranker.calls == []


def test_the_reranker_is_not_called_below_the_threshold(acme: Company) -> None:
    reranker = FakeReranker()

    retrieval = acme.retrieve("alice", min_score=0.99, reranker=reranker)

    assert retrieval.below_threshold
    assert reranker.calls == []


def test_the_gate_still_uses_the_search_score_not_the_rerank_score(acme: Company) -> None:
    plain = acme.retrieve("alice")
    best = plain.chunks[0].score
    # The reranker hates everything; the threshold is judged on the search.
    reranker = FakeReranker(scores=[-100.0] * 5)

    retrieval = acme.retrieve("alice", min_score=best, reranker=reranker)

    assert not retrieval.below_threshold
    assert retrieval.best_score == best


# --- a reranker that misbehaves ---------------------------------------------------------


@pytest.mark.parametrize(
    "scores",
    [[1.0, 2.0], [1.0] * 6, [1.0, float("nan"), 2.0, 3.0, 4.0], [1.0, float("inf"), 2.0, 3.0, 4.0]],
)
def test_unusable_scores_are_refused(acme: Company, scores: list[float]) -> None:
    with pytest.raises(RerankError):
        acme.retrieve("carol", reranker=FakeReranker(scores=scores))


def test_checked_scores_passes_good_scores_through() -> None:
    assert checked_scores([3.0, -1.5], 2) == [3.0, -1.5]


def test_a_chunk_too_long_for_the_reranker_keeps_the_search_order(
    acme: Company, caplog: pytest.LogCaptureFixture
) -> None:
    plain = acme.retrieve("carol")
    reranker = FakeReranker(scores=[1.0, 2.0, 3.0, 4.0, 5.0], max_tokens=10)

    with caplog.at_level(logging.INFO):
        retrieval = acme.retrieve("carol", reranker=reranker)

    assert ids(retrieval) == ids(plain)
    assert reranker.calls == []
    assert any("too long for the reranker" in r.message for r in caplog.records)


# --- settings and logging ---------------------------------------------------------------


def test_reranking_is_off_by_default() -> None:
    settings = Settings(_env_file=None, jwt_secret_key=SecretStr("x" * 64))

    assert settings.reranker_model is None
    assert load_reranker(settings) is None


def test_reranking_is_logged_without_any_text(
    acme: Company, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO):
        retrieval = acme.retrieve("carol", reranker=FakeReranker())

    record = next(r for r in caplog.records if r.message == "chunks retrieved")
    assert record.reranked is True  # type: ignore[attr-defined]
    logged = everything_logged(caplog)
    for chunk in retrieval.chunks:
        assert chunk.text[:40] not in logged
    assert "salary" not in logged.lower()


def test_a_named_reranker_is_loaded_with_the_offline_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loaded: list[tuple[str, bool]] = []

    class Recorder:
        def __init__(self, model_name: str, offline: bool = False) -> None:
            loaded.append((model_name, offline))

    monkeypatch.setattr(reranking, "CrossEncoderReranker", Recorder)
    settings = Settings(
        _env_file=None,
        jwt_secret_key=SecretStr("x" * 64),
        reranker_model="some/cross-encoder",
        embedding_offline=True,
    )

    assert isinstance(load_reranker(settings), Recorder)
    assert loaded == [("some/cross-encoder", True)]

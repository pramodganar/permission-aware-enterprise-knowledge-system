"""Measuring retrieval on the evaluation set, end to end.

With the fake embedder the numbers mean nothing, but everything around them
can be checked: every question finds its relevant chunks, every chunk
retrieved is one the asker may read, and the report is the same twice.
The real numbers are checked against the committed baseline with the model
tests.
"""

import dataclasses
import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from sqlalchemy import Engine, select

from app.authorization.service import can_read
from app.core.config import Settings
from app.models import Chunk
from evaluation import retrieval
from evaluation.company import Company, loaded_company, measuring_settings
from evaluation.dataset import Kind, default_set
from fakes import FakeEmbedder, FakeReranker


@pytest.fixture(scope="module")
def company() -> Iterator[Company]:
    with loaded_company(measuring_settings(), FakeEmbedder()) as loaded:
        yield loaded


def test_every_answerable_question_is_ranked_with_its_relevant_chunks(company: Company) -> None:
    results = retrieval.evaluate(company, default_set())

    assert [r.question_id for r in results] == [
        q.id for q in default_set().of_kind(Kind.ANSWERABLE)
    ]
    assert all(r.relevant for r in results)
    assert all(len(r.ranked) == retrieval.DEPTH for r in results)


def test_every_chunk_ranked_is_one_the_asker_may_read(company: Company) -> None:
    for result in retrieval.evaluate(company, default_set()):
        asker = company.person(result.asker)
        for chunk_id in result.ranked:
            chunk = company.db.get(Chunk, chunk_id)
            assert chunk is not None
            assert can_read(asker, chunk.document), (result.question_id, chunk_id)


def test_the_relevant_chunks_hold_the_evidence_and_belong_to_the_document(
    company: Company,
) -> None:
    for question in default_set().of_kind(Kind.ANSWERABLE):
        for chunk_id in retrieval.relevant_chunks(company, question):
            chunk = company.db.get(Chunk, chunk_id)
            assert chunk is not None
            assert chunk.document.title == question.document


def test_a_quote_found_in_other_documents_too_counts_only_its_own(company: Company) -> None:
    # "EUR" is in most of the company's documents.
    travel = next(q for q in default_set().of_kind(Kind.ANSWERABLE) if q.id == "frank-hotel")
    loose = travel.model_copy(update={"evidence": ("EUR",)})

    relevant = retrieval.relevant_chunks(company, loose)

    titles = {chunk.document.title for chunk in (company.db.get_one(Chunk, i) for i in relevant)}
    assert titles == {"Travel and Expense Policy"}


def test_a_question_whose_evidence_is_in_no_chunk_is_refused(company: Company) -> None:
    question = (
        default_set()
        .of_kind(Kind.ANSWERABLE)[0]
        .model_copy(update={"evidence": ("words in no document at all",)})
    )

    with pytest.raises(ValueError, match="no chunk holds"):
        retrieval.relevant_chunks(company, question)


def test_the_report_says_what_was_measured_and_is_the_same_twice(company: Company) -> None:
    questions = default_set()

    def measured() -> dict[str, object]:
        return retrieval.report(
            retrieval.evaluate(company, questions),
            questions=questions,
            company=company,
            reranked=False,
        )

    first = measured()

    assert first == measured()
    assert first["set_version"] == questions.version
    assert first["settings"] == {
        "embedding_model": company.settings.embedding_model,
        "chunk_max_characters": 800,
        "chunk_overlap_characters": 120,
        "relevance_threshold": 0.35,
        "reranker": None,
    }
    by_person = first["by_person"]
    assert isinstance(by_person, dict)
    assert sorted(by_person) == ["alice", "bob", "carol", "david", "emma", "frank", "grace"]
    assert sum(p["questions"] for p in by_person.values()) == 34


def test_the_command_writes_the_report(
    company: Company,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fake_company(settings: Settings) -> Iterator[Company]:
        yield company

    monkeypatch.setattr(retrieval, "loaded_company", contextmanager(fake_company))
    out = tmp_path / "retrieval.json"

    assert retrieval.main(["--output", str(out)]) == 0

    numbers = json.loads(out.read_text(encoding="utf-8"))
    assert numbers["overall"]["questions"] == 34
    printed = capsys.readouterr().out
    assert "overall" in printed
    assert f"Written to {out}" in printed


def test_the_command_reranks_when_asked(
    company: Company, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    used: list[str | None] = []
    reranker = FakeReranker()

    def fake_company(settings: Settings) -> Iterator[Company]:
        used.append(settings.reranker_model)
        # The company as loading it with those settings would give.
        yield dataclasses.replace(company, settings=settings)

    monkeypatch.setattr(retrieval, "loaded_company", contextmanager(fake_company))
    monkeypatch.setattr(retrieval, "load_reranker", lambda settings: reranker)
    out = tmp_path / "reranked.json"

    assert retrieval.main(["--rerank", "--output", str(out)]) == 0

    assert used == ["cross-encoder/ms-marco-MiniLM-L6-v2"]
    # Every answerable question went through the reranker.
    assert len(reranker.calls) == 34
    assert json.loads(out.read_text(encoding="utf-8"))["settings"]["reranker"] == used[0]


def test_the_measured_settings_ignore_this_machines_env() -> None:
    settings = measuring_settings()

    assert settings.llm_provider == "none"
    assert settings.reranker_model is None
    assert measuring_settings("a-reranker").reranker_model == "a-reranker"


def test_a_run_never_touches_a_database_on_disk(company: Company) -> None:
    bind = company.db.get_bind()

    assert isinstance(bind, Engine)
    assert str(bind.url) == "sqlite://"
    assert company.db.scalars(select(Chunk.id)).first() is not None

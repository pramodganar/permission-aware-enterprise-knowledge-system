"""The committed answer results are what the code makes of the committed answers.

A real model's answers vary, so the Groq answers aren't rerun. What can be
checked without a network is everything computed from them: the scores in
the answers file, the hand labels being for exactly these answers, and the
judge's summary being the count of the verdicts and labels it holds. And
that the file holds nothing beyond what each asker could have been shown.
"""

import json
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import select

from app.authorization.service import can_read
from app.models import Chunk, Document
from evaluation import judge
from evaluation.answers import Answers, load, score
from evaluation.company import Company, loaded_company, measuring_settings
from evaluation.dataset import default_set
from fakes import FakeEmbedder


@pytest.fixture(scope="module")
def answers() -> Answers:
    return load(judge.ANSWERS)


@pytest.fixture(scope="module")
def judged() -> dict[str, Any]:
    found: dict[str, Any] = json.loads(judge.JUDGED.read_text(encoding="utf-8"))
    return found


@pytest.fixture(scope="module")
def company() -> Iterator[Company]:
    # Chunking doesn't depend on the embedder, so the chunk IDs are the ones
    # the answers were given.
    with loaded_company(measuring_settings(), FakeEmbedder()) as c:
        yield c


def test_the_scores_in_the_file_are_the_scores_of_its_answers(answers: Answers) -> None:
    assert answers.summary == score(answers.answers, default_set())
    assert answers.summary["failed"] == []


def test_every_source_in_the_file_is_one_its_asker_may_read(
    answers: Answers, company: Company
) -> None:
    for answer in answers.answers:
        asker = company.person(answer.asker)
        for source in answer.sources:
            chunks = company.db.scalars(select(Chunk).where(Chunk.id.in_(source.chunk_ids))).all()
            assert len(chunks) == len(source.chunk_ids)
            document = company.db.get_one(Document, chunks[0].document_id)
            assert {c.document_id for c in chunks} == {document.id}
            assert document.title == source.document
            assert can_read(asker, document), (answer.id, source.document)
            for chunk in chunks:
                assert chunk.text in source.text


def test_the_hand_labels_are_for_exactly_these_answers(answers: Answers) -> None:
    labels = judge.load_labels()
    answered = {a.id: a.digest for a in judge.judged_answers(answers)}
    assert {label.id: label.digest for label in labels.labels} == answered


def test_the_judges_summary_is_the_count_of_its_verdicts_and_the_labels(
    answers: Answers, judged: dict[str, Any]
) -> None:
    recounted = judge.summarise(
        answers,
        judge.stored_verdicts(judged),
        judge.load_labels(),
        judge_model=judged["judge_model"],
    )
    assert recounted == judged
    assert judged["stale_labels"] == judged["unlabelled"] == []
    assert {v["id"] for v in judged["verdicts"]} == {a.id for a in judge.judged_answers(answers)}

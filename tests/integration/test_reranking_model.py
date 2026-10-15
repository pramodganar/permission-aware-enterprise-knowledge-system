"""Reranking with the real cross-encoder. Run with: pytest -m model

These are the measurements behind leaving reranking off by default
(docs/retrieval.md). If the documents or models change, they say whether
that decision still holds.
"""

from collections.abc import Iterator

import pytest
from pydantic import SecretStr
from sqlalchemy import select

from app.core.config import Settings
from app.models import Chunk
from app.retrieval.embeddings import embedding_text
from app.retrieval.reranking import CrossEncoderReranker, Reranker
from app.retrieval.retriever import RetrievedChunk
from questions import ANSWERABLE, OFF_TOPIC, Company, real_company

pytestmark = pytest.mark.model

RERANKER = "cross-encoder/ms-marco-MiniLM-L6-v2"
NO_GATE = -1.0


@pytest.fixture(scope="module")
def acme() -> Iterator[Company]:
    with real_company() as company:
        yield company


@pytest.fixture(scope="module")
def reranker() -> Reranker:
    return CrossEncoderReranker(RERANKER)


def rank_of(title: str, chunks: tuple[RetrievedChunk, ...]) -> int:
    return [chunk.document_title for chunk in chunks].index(title) + 1


def test_reranking_puts_the_answer_first_no_more_often_than_the_search(
    acme: Company, reranker: Reranker
) -> None:
    plain = reranked = 0
    for question, (asker, answer) in ANSWERABLE.items():
        plain += acme.ask(asker, question).chunks[0].document_title == answer
        reranked += acme.ask(asker, question, reranker=reranker).chunks[0].document_title == answer

    assert (plain, reranked) == (21, 21)


def test_reranking_moves_carols_case_file_up_one_place(acme: Company, reranker: Reranker) -> None:
    question = "Which employees are on a performance improvement plan?"
    case_file = "HR Case File 2026-014"

    assert rank_of(case_file, acme.ask("carol", question).chunks) == 5
    assert rank_of(case_file, acme.ask("carol", question, reranker=reranker).chunks) == 4


def test_reranking_does_not_change_who_gets_an_answer(acme: Company, reranker: Reranker) -> None:
    # The gate is judged on the search score, so turning reranking on can't
    # let an off-topic question through or stop an answerable one.
    for question in OFF_TOPIC:
        assert acme.ask("grace", question, reranker=reranker).is_empty
    for question, (asker, _) in ANSWERABLE.items():
        assert not acme.ask(asker, question, reranker=reranker).is_empty
    assert acme.ask("david", "How are executive bonuses calculated?", reranker=reranker).is_empty


def test_its_scores_separate_the_questions_far_less_well(acme: Company, reranker: Reranker) -> None:
    # Why the gate stays on the search score. For each question, the best of
    # its 20 candidates by each measure; then the gap between the worst
    # answerable question and the best off-topic one, as a share of the range.
    def best_scores(name: str, question: str) -> tuple[float, float]:
        chunks = acme.ask(name, question, limit=20, min_score=NO_GATE).chunks
        passages = [embedding_text(c.document_title, c.text) for c in chunks]
        return chunks[0].score, max(reranker.score(question, passages))

    answerable = [best_scores(asker, q) for q, (asker, _) in ANSWERABLE.items()]
    off_topic = [best_scores("grace", q) for q in OFF_TOPIC]

    def margin(which: int) -> float:
        worst_answer = min(scores[which] for scores in answerable)
        best_off_topic = max(scores[which] for scores in off_topic)
        top = max(scores[which] for scores in answerable)
        bottom = min(scores[which] for scores in off_topic)
        return (worst_answer - best_off_topic) / (top - bottom)

    search, rerank = margin(0), margin(1)

    # Measured: about 16% of the range for the search, under 2% for the reranker.
    assert search > 0.15
    assert 0 < rerank < 0.02


def test_the_longest_question_and_chunk_fit_in_the_reranker(
    acme: Company, reranker: Reranker
) -> None:
    # The embedder refuses questions over its 256 tokens, and chunks with
    # their title are at most 256, so together they fit in the reranker's 512.
    words = ["budget"] * 300
    while acme.embedder.count_tokens(" ".join(words)) > acme.embedder.max_tokens:
        words.pop()
    longest_question = " ".join(words)
    texts = [
        embedding_text(chunk.document.title, chunk.text)
        for chunk in acme.db.scalars(select(Chunk).where(Chunk.id.in_(acme.index.chunk_ids)))
    ]

    longest = max(reranker.count_tokens(longest_question, text) for text in texts)

    assert reranker.max_tokens == 512
    assert longest <= reranker.max_tokens


def test_reranking_is_off_unless_named() -> None:
    settings = Settings(_env_file=None, jwt_secret_key=SecretStr("x" * 64))

    assert settings.reranker_model is None

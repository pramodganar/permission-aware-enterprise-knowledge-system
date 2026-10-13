"""The real embedding model. Slow, so left out of a normal run: pytest -m model"""

from pathlib import Path

import numpy as np
import pytest
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.authorization.service import readable_chunks
from app.core.config import Settings
from app.core.exceptions import InvalidInputError
from app.models import Chunk, User
from app.retrieval.embeddings import (
    Embedder,
    EmbeddingError,
    embed_chunks,
    embed_question,
    embedding_text,
    load_embedder,
)
from app.retrieval.index import build_index
from app.retrieval.index_store import load_index, save_index
from app.retrieval.query import process_question
from scripts.seed import seed

pytestmark = pytest.mark.model

# question: the document that answers it
QUESTIONS = {
    "How does our deployment pipeline handle rollbacks?": "Deployment Runbook",
    "How many days of annual leave do I get?": "Leave Policy",
    "What is the approval limit for vendor payments?": "Vendor Payment Approval Limits",
    "What was the root cause of the last production outage?": "Payments Outage Postmortem",
    "What is the process for a performance improvement plan?": (
        "Performance Improvement Plan Process"
    ),
    "What is next year's headcount budget?": "Headcount Budget 2027",
    "What discount can I offer without approval?": "Discount Approval Policy",
    "What is the status of the dispute with our supplier?": "Supplier Dispute Status",
    "How are executive bonuses calculated?": "Executive Compensation Strategy 2027",
    "What is our data retention obligation for contracts?": "Contract Data Retention Obligations",
    "What is the SOP for a warehouse incident?": "Warehouse Incident SOP",
    "What is the company's three-year strategy?": "Three-Year Strategy 2027 to 2029",
    "What are the salary bands for senior engineers?": "Salary Bands 2026",
    "What acquisitions is the company considering?": "Project Falcon Acquisition Paper",
    "What is the executive compensation strategy?": "Executive Compensation Strategy 2027",
    "What is the hotel limit when travelling?": "Travel and Expense Policy",
    "Can I accept a gift from a supplier?": "Code of Conduct",
}


@pytest.fixture(scope="module")
def embedder() -> Embedder:
    settings = Settings(_env_file=None, jwt_secret_key=SecretStr("x" * 64))
    return load_embedder(settings)


@pytest.fixture
def chunks(db: Session, settings: Settings) -> list[Chunk]:
    seed(db, settings, "embedding-test-password")
    return list(db.scalars(select(Chunk).order_by(Chunk.id)))


def test_the_model_reports_its_sizes(embedder: Embedder) -> None:
    assert embedder.name == "sentence-transformers/all-MiniLM-L6-v2"
    assert (embedder.dimension, embedder.max_tokens) == (384, 256)


def test_token_counts_include_the_two_special_tokens(embedder: Embedder) -> None:
    assert embedder.count_tokens("") == 2
    assert embedder.count_tokens("leave") == 3


def test_vectors_are_unit_length_and_repeatable(embedder: Embedder) -> None:
    texts = [
        "Full-time employees receive 25 days of paid annual leave.",
        "Rollbacks are automatic.",
    ]

    first, second = embedder.embed_documents(texts), embedder.embed_documents(texts)

    assert first.shape == (2, 384)
    assert first.dtype == np.float32
    assert np.allclose(np.linalg.norm(first, axis=1), 1.0, atol=1e-4)
    assert np.allclose(first, second, atol=1e-6)


def test_a_question_is_closer_to_its_answer_than_to_something_else(embedder: Embedder) -> None:
    question = embed_question("How many days of annual leave do I get?", embedder)
    answer, other = embedder.embed_documents(
        [
            "Full-time employees receive 25 days of paid annual leave per calendar year.",
            "The pipeline rolls back automatically during the canary stage.",
        ]
    )

    assert question @ answer > 0.5
    assert question @ other < 0.2


def test_the_model_silently_ignores_text_past_its_limit(embedder: Embedder) -> None:
    # This is the reason embed_chunks counts tokens before embedding.
    filler = "The supervisor files an incident report in the operations system. " * 40
    assert embedder.count_tokens(filler) > embedder.max_tokens

    without_fact, with_fact = embedder.embed_documents(
        [filler, filler + "The approval limit for vendor payments is 5,000 EUR."]
    )

    assert np.allclose(without_fact, with_fact, atol=1e-6)


def test_every_company_chunk_fits_in_the_model(embedder: Embedder, chunks: list[Chunk]) -> None:
    counts = [
        embedder.count_tokens(embedding_text(chunk.document.title, chunk.text)) for chunk in chunks
    ]

    assert len(counts) == 57
    assert max(counts) <= embedder.max_tokens


def test_all_company_chunks_are_embedded(embedder: Embedder, chunks: list[Chunk]) -> None:
    vectors = embed_chunks(chunks, embedder)

    assert vectors.shape == (57, 384)
    assert np.allclose(np.linalg.norm(vectors, axis=1), 1.0, atol=1e-3)


def test_a_chunk_that_is_too_long_is_refused_by_the_real_model(
    embedder: Embedder, chunks: list[Chunk]
) -> None:
    chunks[0].text = "incident report " * 400

    with pytest.raises(EmbeddingError, match="more than the 256"):
        embed_chunks(chunks[:3], embedder)


def test_each_question_finds_its_document_in_the_top_three(
    embedder: Embedder, chunks: list[Chunk]
) -> None:
    # A smoke check on the model with no permissions involved. It is not the
    # retrieval evaluation: 17 questions over 57 chunks is far too easy.
    vectors = embed_chunks(chunks, embedder)

    top_one = 0
    for question, expected in QUESTIONS.items():
        scores = vectors @ embed_question(question, embedder)
        best = [chunks[int(index)].document.title for index in np.argsort(-scores)[:3]]
        assert expected in best, question
        top_one += best[0] == expected

    assert top_one >= 15


def test_the_bonus_question_with_the_real_model_and_a_restricted_search(
    embedder: Embedder, db: Session, chunks: list[Chunk]
) -> None:
    # The incident the project starts from, end to end except for the LLM:
    # the real model, the real index, and each person's own list of chunks.
    index = build_index(db, embedder)
    by_id = {chunk.id: chunk for chunk in chunks}
    question = embed_question("How are executive bonuses calculated?", embedder)

    def titles(name: str) -> list[str]:
        user = db.scalars(select(User).where(User.email == f"{name}@acmecorp.example")).one()
        allowed = set(db.scalars(readable_chunks(user).with_only_columns(Chunk.id)))
        return [by_id[hit.chunk_id].document.title for hit in index.search(question, 5, allowed)]

    david, grace = titles("david"), titles("grace")

    assert grace[0] == "Executive Compensation Strategy 2027"
    assert "Executive Compensation Strategy 2027" not in david
    assert len(david) == 5
    assert set(david) <= {
        "Leave Policy",
        "Code of Conduct",
        "Travel and Expense Policy",
        "Discount Approval Policy",
    }


def test_a_saved_index_loads_and_answers_like_the_one_in_memory(
    embedder: Embedder, db: Session, chunks: list[Chunk], tmp_path: Path
) -> None:
    settings = Settings(_env_file=None, jwt_secret_key=SecretStr("x" * 64))
    built = build_index(db, embedder)
    manifest = save_index(built, embedder, settings, tmp_path)

    loaded, loaded_manifest = load_index(settings, tmp_path)

    assert loaded_manifest == manifest
    assert manifest.dimension == 384
    assert manifest.chunk_count == 57
    for question in QUESTIONS:
        vector = embed_question(question, embedder)
        assert loaded.search(vector, 5, built.chunk_ids) == built.search(vector, 5, built.chunk_ids)


def test_questions_are_processed_with_the_real_model(embedder: Embedder) -> None:
    settings = Settings(_env_file=None, jwt_secret_key=SecretStr("x" * 64))

    question = process_question("  How many days of annual\t leave do I get? ", settings, embedder)

    assert question.text == "How many days of annual leave do I get?"
    assert question.tokens == 12
    assert question.vector.shape == (384,)


def test_a_question_within_the_character_limit_can_still_be_too_long_for_the_model(
    embedder: Embedder,
) -> None:
    # 1,000 characters of numbers is far more than 256 tokens.
    settings = Settings(_env_file=None, jwt_secret_key=SecretStr("x" * 64))
    question = ("68,000 92,000 " * 80)[:1000]

    with pytest.raises(InvalidInputError, match="too long"):
        process_question(question, settings, embedder)

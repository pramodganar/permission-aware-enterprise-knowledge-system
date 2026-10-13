"""Embedding chunks and questions, tested with a fake model."""

import logging
from collections.abc import Sequence

import numpy as np
import pytest
from sqlalchemy import select

from acmecorp import World
from app.core.config import Settings
from app.ingestion.pipeline import Upload, ingest
from app.models import Chunk, Document
from app.retrieval import embeddings
from app.retrieval.embeddings import (
    Embedder,
    EmbeddingError,
    Vectors,
    embed_chunks,
    embed_question,
    embedding_text,
    load_embedder,
)
from fakes import FakeEmbedder
from logcheck import everything_logged

GUIDE = "\n".join(
    f"Step {number}. The on-call engineer checks item {number} before the release goes out."
    for number in range(1, 31)
)
POLICY = "\n".join(
    f"Rule {number}. Annual leave request {number} is approved by the line manager."
    for number in range(1, 31)
)


@pytest.fixture
def small(settings: Settings) -> Settings:
    return settings.model_copy(update={"chunk_max_characters": 300, "chunk_overlap_characters": 80})


def store(world: World, settings: Settings, title: str, text: str, department: str) -> Document:
    upload = Upload(
        filename=f"{title}.txt",
        data=text.encode(),
        title=title,
        department_code=department,
        classification="departmental",
    )
    return ingest(world.db, world.users["admin"], upload, settings)


@pytest.fixture
def guide(world: World, small: Settings) -> Document:
    return store(world, small, "Release Guide", GUIDE, "engineering")


class BrokenEmbedder(FakeEmbedder):
    """Returns whatever the test tells it to."""

    def __init__(self, result: Vectors) -> None:
        super().__init__()
        self.result = result

    def embed_documents(self, texts: Sequence[str]) -> Vectors:
        return self.result

    def embed_query(self, text: str) -> Vectors:
        return self.result


def unit_rows(count: int, dimension: int = 64) -> Vectors:
    rows = np.zeros((count, dimension), dtype=np.float32)
    rows[:, 0] = 1.0
    return rows


# --- what is embedded ----------------------------------------------------------------


def test_the_title_goes_in_front_of_the_chunk_text() -> None:
    text = embedding_text("Vendor Payment Approval Limits", "Up to 5,000 EUR: Finance Analyst")

    assert text == "Vendor Payment Approval Limits\nUp to 5,000 EUR: Finance Analyst"


def test_one_vector_per_chunk_in_the_same_order(guide: Document) -> None:
    embedder = FakeEmbedder()

    vectors = embed_chunks(guide.chunks, embedder)

    assert vectors.shape == (len(guide.chunks), embedder.dimension)
    assert vectors.dtype == np.float32
    for row, chunk in zip(vectors, guide.chunks, strict=True):
        expected = embedder.embed_query(embedding_text(guide.title, chunk.text))
        assert np.array_equal(row, expected)


def test_vectors_are_unit_length(guide: Document) -> None:
    vectors = embed_chunks(guide.chunks, FakeEmbedder())

    assert np.allclose(np.linalg.norm(vectors, axis=1), 1.0)


def test_each_chunk_is_embedded_with_the_title_of_its_own_document(
    world: World, small: Settings, guide: Document
) -> None:
    policy = store(world, small, "Leave Rules", POLICY, "hr")
    embedder = FakeEmbedder()

    # Mixed on purpose: the order of the batch must not decide which title is used.
    mixed = [policy.chunks[0], guide.chunks[0], policy.chunks[1], guide.chunks[1]]
    embed_chunks(mixed, embedder)

    assert [text.split("\n")[0] for text in embedder.embedded] == [
        "Leave Rules",
        "Release Guide",
        "Leave Rules",
        "Release Guide",
    ]
    for text in embedder.embedded:
        if text.startswith("Release Guide"):
            assert "Annual leave" not in text
        else:
            assert "on-call engineer" not in text


def test_the_stored_chunk_text_is_not_changed(guide: Document) -> None:
    before = [chunk.text for chunk in guide.chunks]

    embed_chunks(guide.chunks, FakeEmbedder())

    assert [chunk.text for chunk in guide.chunks] == before
    assert not any(chunk.text.startswith("Release Guide") for chunk in guide.chunks)


def test_no_chunks_gives_an_empty_result_of_the_right_width() -> None:
    embedder = FakeEmbedder(dimension=64)

    vectors = embed_chunks([], embedder)

    assert vectors.shape == (0, 64)
    assert embedder.embedded == []


def test_the_same_chunks_give_the_same_vectors(guide: Document) -> None:
    first = embed_chunks(guide.chunks, FakeEmbedder())
    second = embed_chunks(guide.chunks, FakeEmbedder())

    assert np.array_equal(first, second)


# --- text the model cannot read in full --------------------------------------------


def test_a_chunk_longer_than_the_model_reads_is_an_error(guide: Document) -> None:
    embedder = FakeEmbedder(max_tokens=20)

    with pytest.raises(EmbeddingError, match="more than the 20") as error:
        embed_chunks(guide.chunks, embedder)

    assert f"chunk {guide.chunks[0].id} of document {guide.id}" in str(error.value)


def test_the_error_does_not_contain_the_text(guide: Document) -> None:
    with pytest.raises(EmbeddingError) as error:
        embed_chunks(guide.chunks, FakeEmbedder(max_tokens=20))

    assert "on-call" not in str(error.value)
    assert "Release Guide" not in str(error.value)


def test_nothing_is_embedded_when_one_chunk_is_too_long(guide: Document) -> None:
    longest = max(len(embedding_text(guide.title, chunk.text).split()) for chunk in guide.chunks)
    embedder = FakeEmbedder(max_tokens=longest - 1)

    with pytest.raises(EmbeddingError):
        embed_chunks(guide.chunks, embedder)

    assert embedder.embedded == []


def test_the_title_counts_towards_the_limit(world: World, settings: Settings) -> None:
    document = store(world, settings, "One two three four five six", "seven eight nine", "hr")

    assert embed_chunks(document.chunks, FakeEmbedder(max_tokens=9)).shape[0] == 1
    with pytest.raises(EmbeddingError, match="is 9 tokens"):
        embed_chunks(document.chunks, FakeEmbedder(max_tokens=8))


def test_a_chunk_exactly_at_the_limit_is_accepted(guide: Document) -> None:
    longest = max(len(embedding_text(guide.title, chunk.text).split()) for chunk in guide.chunks)

    vectors = embed_chunks(guide.chunks, FakeEmbedder(max_tokens=longest))

    assert vectors.shape[0] == len(guide.chunks)


# --- vectors a search could not use ---------------------------------------------------


def test_the_wrong_number_of_vectors_is_refused(guide: Document) -> None:
    with pytest.raises(EmbeddingError, match="expected"):
        embed_chunks(guide.chunks, BrokenEmbedder(unit_rows(len(guide.chunks) - 1)))


def test_vectors_of_the_wrong_size_are_refused(guide: Document) -> None:
    with pytest.raises(EmbeddingError, match="expected"):
        embed_chunks(guide.chunks, BrokenEmbedder(unit_rows(len(guide.chunks), dimension=32)))


@pytest.mark.parametrize("bad", [np.nan, np.inf, -np.inf])
def test_a_vector_with_a_value_that_is_not_a_number_is_refused(guide: Document, bad: float) -> None:
    rows = unit_rows(len(guide.chunks))
    rows[1, 5] = bad

    with pytest.raises(EmbeddingError, match="not a number"):
        embed_chunks(guide.chunks, BrokenEmbedder(rows))


@pytest.mark.parametrize("scale", [0.0, 0.5, 3.0])
def test_a_vector_that_is_not_unit_length_is_refused(guide: Document, scale: float) -> None:
    rows = unit_rows(len(guide.chunks))
    rows[2] *= scale

    with pytest.raises(EmbeddingError, match="unit length"):
        embed_chunks(guide.chunks, BrokenEmbedder(rows))


# --- questions ---------------------------------------------------------------------


def test_a_question_becomes_one_unit_length_vector() -> None:
    embedder = FakeEmbedder()

    vector = embed_question("How do rollbacks work?", embedder)

    assert vector.shape == (embedder.dimension,)
    assert vector.dtype == np.float32
    assert np.isclose(np.linalg.norm(vector), 1.0)


def test_a_question_is_closest_to_the_chunk_that_shares_its_words(
    world: World, small: Settings, guide: Document
) -> None:
    policy = store(world, small, "Leave Rules", POLICY, "hr")
    embedder = FakeEmbedder()
    chunks = [*guide.chunks, *policy.chunks]

    scores = embed_chunks(chunks, embedder) @ embed_question("annual leave request", embedder)

    assert chunks[int(np.argmax(scores))].document_id == policy.id


@pytest.mark.parametrize(
    "result",
    [unit_rows(1, dimension=32)[0], unit_rows(2), np.full(64, np.nan, dtype=np.float32)],
)
def test_an_unusable_question_vector_is_refused(result: Vectors) -> None:
    with pytest.raises(EmbeddingError):
        embed_question("How do rollbacks work?", BrokenEmbedder(result))


# --- logging and loading ------------------------------------------------------------


def test_embedding_is_logged_with_counts_only(
    guide: Document, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO):
        embed_chunks(guide.chunks, FakeEmbedder())

    record = next(r for r in caplog.records if r.message == "chunks embedded")
    assert record.chunks == len(guide.chunks)  # type: ignore[attr-defined]
    assert record.model == "fake-embedder"  # type: ignore[attr-defined]
    assert "on-call" not in everything_logged(caplog)


def test_the_fake_has_the_shape_of_a_real_embedder() -> None:
    embedder: Embedder = FakeEmbedder()

    assert (embedder.name, embedder.dimension, embedder.max_tokens) == ("fake-embedder", 64, 256)


def test_the_model_is_loaded_from_the_settings(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, object] = {}

    class Recorder(FakeEmbedder):
        def __init__(self, model_name: str, batch_size: int, offline: bool) -> None:
            super().__init__()
            seen.update(model_name=model_name, batch_size=batch_size, offline=offline)

    monkeypatch.setattr(embeddings, "SentenceTransformerEmbedder", Recorder)
    chosen = settings.model_copy(
        update={
            "embedding_model": "some/model",
            "embedding_batch_size": 8,
            "embedding_offline": True,
        }
    )

    load_embedder(chosen)

    assert seen == {"model_name": "some/model", "batch_size": 8, "offline": True}


def test_chunks_can_be_embedded_straight_from_a_query(world: World, guide: Document) -> None:
    chunks = world.db.scalars(select(Chunk).order_by(Chunk.id)).all()

    assert embed_chunks(chunks, FakeEmbedder()).shape[0] == len(guide.chunks)

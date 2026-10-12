"""The vector index, and above all that a search stays inside the allowed list."""

import inspect
import logging

import numpy as np
import pytest

from acmecorp import World
from app.core.config import Settings
from app.ingestion.pipeline import Upload, delete_document, ingest, replace_document
from app.models import Document
from app.retrieval.embeddings import Vectors, embed_question
from app.retrieval.index import (
    MAX_SEARCH_LIMIT,
    FaissVectorIndex,
    VectorIndexError,
    build_index,
)
from fakes import FakeEmbedder

DIMENSION = 16


def unit_vectors(count: int, seed: int = 0) -> Vectors:
    rng = np.random.default_rng(seed)
    vectors = rng.normal(size=(count, DIMENSION)).astype(np.float32)
    normalised: Vectors = vectors / np.linalg.norm(vectors, axis=1, keepdims=True)
    return normalised


def filled(count: int = 50, seed: int = 0) -> tuple[FaissVectorIndex, list[int], Vectors]:
    """An index of ``count`` random vectors under the IDs 101, 102, ..."""
    ids = list(range(101, 101 + count))
    vectors = unit_vectors(count, seed)
    index = FaissVectorIndex(DIMENSION)
    index.add(ids, vectors)
    return index, ids, vectors


def by_hand(
    ids: list[int], vectors: Vectors, query: Vectors, limit: int, allowed: set[int]
) -> list[tuple[int, float]]:
    """The expected result, worked out without the index."""
    scored = [
        (chunk_id, float(vector @ query))
        for chunk_id, vector in zip(ids, vectors, strict=True)
        if chunk_id in allowed
    ]
    return sorted(scored, key=lambda item: -item[1])[:limit]


# --- staying inside the allowed list -------------------------------------------------


def test_the_allowed_list_is_required_and_has_no_default() -> None:
    parameter = inspect.signature(FaissVectorIndex.search).parameters["allowed_ids"]

    assert parameter.default is inspect.Parameter.empty
    with pytest.raises(TypeError):
        filled()[0].search(unit_vectors(1)[0], 5)  # type: ignore[call-arg]


def test_a_search_never_returns_a_chunk_outside_the_allowed_list() -> None:
    index, ids, _ = filled(200)
    rng = np.random.default_rng(1)

    for trial in range(300):
        allowed = set(rng.choice(ids, size=int(rng.integers(0, 60)), replace=False).tolist())
        query = unit_vectors(1, seed=1000 + trial)[0]

        hits = index.search(query, int(rng.integers(1, 30)), allowed)

        assert {hit.chunk_id for hit in hits} <= allowed


def test_a_search_matches_a_calculation_done_by_hand() -> None:
    index, ids, vectors = filled(200)
    rng = np.random.default_rng(2)

    for trial in range(200):
        allowed = set(rng.choice(ids, size=int(rng.integers(1, 80)), replace=False).tolist())
        query = unit_vectors(1, seed=2000 + trial)[0]
        limit = int(rng.integers(1, 20))

        hits = index.search(query, limit, allowed)
        expected = by_hand(ids, vectors, query, limit, allowed)

        assert [hit.chunk_id for hit in hits] == [chunk_id for chunk_id, _ in expected]
        assert np.allclose([hit.score for hit in hits], [score for _, score in expected], atol=1e-5)


def test_an_empty_allowed_list_finds_nothing() -> None:
    index, _, vectors = filled()

    assert index.search(vectors[0], 5, []) == []
    assert index.search(vectors[0], 5, set()) == []


def test_the_closest_vector_is_not_found_when_it_is_not_allowed() -> None:
    index, ids, vectors = filled()

    everything = index.search(vectors[7], 1, ids)
    without_it = index.search(vectors[7], 1, [chunk_id for chunk_id in ids if chunk_id != ids[7]])

    assert [hit.chunk_id for hit in everything] == [ids[7]]
    assert everything[0].score == pytest.approx(1.0, abs=1e-5)
    assert without_it[0].chunk_id != ids[7]
    assert without_it[0].score < 1.0


def test_closer_vectors_outside_the_list_do_not_use_up_places() -> None:
    # The weakness of filtering after the search: if the forty closest vectors
    # are ones the user may not read, a top five with them removed is empty.
    index, ids, vectors = filled(50)
    query = vectors[0]
    ranked = [hit.chunk_id for hit in index.search(query, 50, ids)]
    allowed = ranked[40:]

    hits = index.search(query, 5, allowed)

    assert [hit.chunk_id for hit in hits] == ranked[40:45]


def test_allowed_ids_that_are_not_in_the_index_are_ignored() -> None:
    index, ids, vectors = filled()

    hits = index.search(vectors[0], 10, [ids[3], 999_999, -4])

    assert [hit.chunk_id for hit in hits] == [ids[3]]


def test_fewer_results_come_back_when_fewer_are_allowed() -> None:
    index, ids, vectors = filled()

    hits = index.search(vectors[0], 10, ids[:3])

    assert len(hits) == 3
    assert {hit.chunk_id for hit in hits} == set(ids[:3])


def test_results_are_ordered_best_first() -> None:
    index, ids, vectors = filled()

    scores = [hit.score for hit in index.search(vectors[0], 20, ids)]

    assert scores == sorted(scores, reverse=True)
    assert scores[0] == pytest.approx(1.0, abs=1e-5)


def test_the_score_is_the_cosine_similarity() -> None:
    index = FaissVectorIndex(2)
    index.add([1, 2, 3], np.array([[1, 0], [0, 1], [-1, 0]], dtype=np.float32))

    hits = index.search(np.array([1, 0], dtype=np.float32), 3, [1, 2, 3])

    assert [(hit.chunk_id, round(hit.score, 5)) for hit in hits] == [(1, 1.0), (2, 0.0), (3, -1.0)]


def test_an_empty_index_finds_nothing() -> None:
    assert FaissVectorIndex(DIMENSION).search(unit_vectors(1)[0], 5, [1, 2, 3]) == []


# --- what a search refuses ------------------------------------------------------------


@pytest.mark.parametrize("limit", [0, -1, MAX_SEARCH_LIMIT + 1])
def test_a_limit_out_of_range_is_refused(limit: int) -> None:
    index, ids, vectors = filled()

    with pytest.raises(VectorIndexError, match="limit"):
        index.search(vectors[0], limit, ids)


def test_the_largest_limit_is_accepted() -> None:
    index, ids, vectors = filled(150)

    assert len(index.search(vectors[0], MAX_SEARCH_LIMIT, ids)) == MAX_SEARCH_LIMIT


@pytest.mark.parametrize(
    "query",
    [
        np.zeros(DIMENSION + 1, dtype=np.float32),
        np.zeros((1, DIMENSION), dtype=np.float32),
        np.full(DIMENSION, np.nan, dtype=np.float32),
        np.full(DIMENSION, np.inf, dtype=np.float32),
    ],
)
def test_a_query_of_the_wrong_shape_or_with_bad_values_is_refused(query: Vectors) -> None:
    index, ids, _ = filled()

    with pytest.raises(VectorIndexError, match="query"):
        index.search(query, 5, ids)


# --- adding ------------------------------------------------------------------------


def test_added_vectors_are_counted_and_listed() -> None:
    index, ids, _ = filled(50)

    assert index.size == 50
    assert index.chunk_ids == frozenset(ids)
    assert index.dimension == DIMENSION


def test_vectors_can_be_added_in_several_steps() -> None:
    index, ids, vectors = filled(10)
    index.add([501, 502], unit_vectors(2, seed=9))

    assert index.size == 12
    assert index.search(vectors[0], 1, [*ids, 501, 502])[0].chunk_id == ids[0]


def test_adding_nothing_changes_nothing() -> None:
    index, _, _ = filled(10)

    index.add([], np.empty((0, DIMENSION), dtype=np.float32))

    assert index.size == 10


@pytest.mark.parametrize(
    ("ids", "problem"),
    [
        ([101, 900], "already in the index"),
        ([900, 900], "already in the index"),
        ([0, 900], "positive"),
        ([-3, 900], "positive"),
        ([900], "expected 1 vectors"),
    ],
)
def test_ids_that_cannot_be_added_are_refused(ids: list[int], problem: str) -> None:
    index, _, _ = filled(10)

    with pytest.raises(VectorIndexError, match=problem):
        index.add(ids, unit_vectors(2, seed=5))

    # Nothing from the refused call was added.
    assert index.size == 10
    assert 900 not in index.chunk_ids


def test_vectors_of_the_wrong_size_are_refused() -> None:
    index, _, _ = filled(10)

    with pytest.raises(VectorIndexError, match="expected"):
        index.add([900], np.ones((1, DIMENSION + 1), dtype=np.float32))


def test_a_vector_with_a_value_that_is_not_a_number_is_refused() -> None:
    index, _, _ = filled(10)
    vectors = unit_vectors(2, seed=5)
    vectors[1, 3] = np.nan

    with pytest.raises(VectorIndexError, match="not a number"):
        index.add([900, 901], vectors)
    assert index.size == 10


def test_an_index_needs_a_vector_size() -> None:
    with pytest.raises(VectorIndexError):
        FaissVectorIndex(0)


def test_a_result_outside_the_allowed_list_would_be_an_error(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # The library is not expected to do this. If it ever did, the index must
    # fail and not hand the chunk on.
    index, ids, vectors = filled(10)

    class Leaky:
        def search(self, query: Vectors, k: int, params: object) -> tuple[Vectors, Vectors]:
            return np.array([[0.9]], dtype=np.float32), np.array([[ids[5]]], dtype=np.int64)

    monkeypatch.setattr(index, "_index", Leaky())

    with caplog.at_level(logging.ERROR), pytest.raises(VectorIndexError, match="outside"):
        index.search(vectors[0], 1, [ids[0]])
    assert "outside the allowed list" in caplog.text


# --- building from the database ---------------------------------------------------------

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


def test_the_index_holds_every_chunk_of_every_active_document(
    world: World, small: Settings
) -> None:
    guide = store(world, small, "Release Guide", GUIDE, "engineering")
    policy = store(world, small, "Leave Rules", POLICY, "hr")
    embedder = FakeEmbedder()

    index = build_index(world.db, embedder)

    assert index.dimension == embedder.dimension
    assert index.chunk_ids == {chunk.id for chunk in [*guide.chunks, *policy.chunks]}


def test_the_index_is_keyed_by_chunk_id(world: World, small: Settings) -> None:
    guide = store(world, small, "Release Guide", GUIDE, "engineering")
    policy = store(world, small, "Leave Rules", POLICY, "hr")
    embedder = FakeEmbedder()
    index = build_index(world.db, embedder)
    everything = index.chunk_ids

    hits = index.search(embed_question("annual leave request", embedder), 3, everything)

    assert {hit.chunk_id for hit in hits} <= {chunk.id for chunk in policy.chunks}
    assert not {hit.chunk_id for hit in hits} & {chunk.id for chunk in guide.chunks}


def test_chunks_of_replaced_and_deleted_documents_are_left_out(
    world: World, small: Settings
) -> None:
    guide = store(world, small, "Release Guide", GUIDE, "engineering")
    policy = store(world, small, "Leave Rules", POLICY, "hr")
    old_guide_ids = {chunk.id for chunk in guide.chunks}
    policy_ids = {chunk.id for chunk in policy.chunks}
    new_guide = replace_document(
        world.db,
        world.users["admin"],
        guide.id,
        filename="guide.txt",
        data=(GUIDE + "\nStep 31. Tell the release channel.").encode(),
        settings=small,
    )
    delete_document(world.db, world.users["admin"], policy.id)

    index = build_index(world.db, FakeEmbedder())

    assert index.chunk_ids == {chunk.id for chunk in new_guide.chunks}
    assert index.chunk_ids.isdisjoint(old_guide_ids | policy_ids)


def test_an_empty_database_gives_an_empty_index(world: World) -> None:
    embedder = FakeEmbedder()

    index = build_index(world.db, embedder)

    assert index.size == 0
    assert index.search(embed_question("anything", embedder), 5, [1, 2, 3]) == []


def test_building_is_logged_with_counts_only(
    world: World, small: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    guide = store(world, small, "Release Guide", GUIDE, "engineering")

    with caplog.at_level(logging.INFO):
        build_index(world.db, FakeEmbedder())

    record = next(r for r in caplog.records if r.message == "vector index built")
    assert record.chunks == len(guide.chunks)  # type: ignore[attr-defined]
    assert "on-call" not in caplog.text

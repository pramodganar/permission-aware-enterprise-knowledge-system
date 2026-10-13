"""Saving the vector index and loading it back, and every reason to refuse it."""

import hashlib
import json
import logging
from pathlib import Path
from typing import Any

import faiss
import numpy as np
import pytest
from pydantic import SecretStr

from app.core.config import Settings
from app.retrieval.embeddings import Vectors
from app.retrieval.index import FaissVectorIndex, VectorIndexError
from app.retrieval.index_store import (
    INDEX_FILE,
    MANIFEST_FILE,
    IndexLoadError,
    IndexManifest,
    load_index,
    save_index,
)
from fakes import FakeEmbedder

DIMENSION = 64  # the fake embedder's


def unit_vectors(count: int, seed: int = 0) -> Vectors:
    rng = np.random.default_rng(seed)
    vectors = rng.normal(size=(count, DIMENSION)).astype(np.float32)
    normalised: Vectors = vectors / np.linalg.norm(vectors, axis=1, keepdims=True)
    return normalised


@pytest.fixture
def fake_settings(settings: Settings) -> Settings:
    """Settings that name the fake embedder, so a saved index loads."""
    return settings.model_copy(update={"embedding_model": "fake-embedder"})


@pytest.fixture
def index() -> FaissVectorIndex:
    index = FaissVectorIndex(DIMENSION)
    index.add(list(range(1, 41)), unit_vectors(40))
    return index


@pytest.fixture
def saved(index: FaissVectorIndex, fake_settings: Settings, tmp_path: Path) -> Path:
    directory = tmp_path / "index"
    save_index(index, FakeEmbedder(), fake_settings, directory)
    return directory


def edit_manifest(directory: Path, **changes: Any) -> None:
    path = directory / MANIFEST_FILE
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest.update(changes)
    path.write_text(json.dumps(manifest), encoding="utf-8")


# --- a round trip -----------------------------------------------------------------


def test_saving_writes_the_index_and_its_manifest(saved: Path) -> None:
    assert sorted(path.name for path in saved.iterdir()) == [MANIFEST_FILE, INDEX_FILE]


def test_the_manifest_records_what_built_the_index(
    index: FaissVectorIndex, fake_settings: Settings, tmp_path: Path
) -> None:
    manifest = save_index(index, FakeEmbedder(), fake_settings, tmp_path)

    assert manifest.embedding_model == "fake-embedder"
    assert (manifest.dimension, manifest.chunk_count) == (DIMENSION, 40)
    assert (manifest.chunk_max_characters, manifest.chunk_overlap_characters) == (800, 120)
    assert (manifest.format_version, manifest.version) == (2, 1)
    assert manifest.content_sha256 is None
    assert manifest.built_at.tzinfo is not None
    stored = IndexManifest.model_validate_json((tmp_path / MANIFEST_FILE).read_text("utf-8"))
    assert stored == manifest


def test_a_loaded_index_gives_the_same_results(
    index: FaissVectorIndex, saved: Path, fake_settings: Settings
) -> None:
    loaded, _ = load_index(fake_settings, saved)
    rng = np.random.default_rng(3)

    assert loaded.chunk_ids == index.chunk_ids
    assert loaded.dimension == index.dimension
    for trial in range(50):
        query = unit_vectors(1, seed=100 + trial)[0]
        allowed = set(rng.choice(range(1, 41), size=15, replace=False).tolist())
        assert loaded.search(query, 5, allowed) == index.search(query, 5, allowed)


def test_a_loaded_index_still_keeps_searches_inside_the_allowed_list(
    saved: Path, fake_settings: Settings
) -> None:
    loaded, _ = load_index(fake_settings, saved)

    hits = loaded.search(unit_vectors(1, seed=7)[0], 10, [3, 4, 5])

    assert {hit.chunk_id for hit in hits} == {3, 4, 5}
    assert loaded.search(unit_vectors(1, seed=7)[0], 10, []) == []


def test_a_loaded_index_refuses_ids_it_already_has(saved: Path, fake_settings: Settings) -> None:
    loaded, _ = load_index(fake_settings, saved)

    with pytest.raises(VectorIndexError, match="already in the index"):
        loaded.add([5], unit_vectors(1, seed=9))


def test_an_empty_index_can_be_saved_and_loaded(fake_settings: Settings, tmp_path: Path) -> None:
    save_index(FaissVectorIndex(DIMENSION), FakeEmbedder(), fake_settings, tmp_path)

    loaded, manifest = load_index(fake_settings, tmp_path)

    assert (loaded.size, manifest.chunk_count) == (0, 0)


def test_saving_again_replaces_the_previous_index(
    index: FaissVectorIndex, saved: Path, fake_settings: Settings
) -> None:
    smaller = FaissVectorIndex(DIMENSION)
    smaller.add([7, 8], unit_vectors(2, seed=4))

    save_index(smaller, FakeEmbedder(), fake_settings, saved)
    loaded, _ = load_index(fake_settings, saved)

    assert loaded.chunk_ids == {7, 8}
    assert sorted(path.name for path in saved.iterdir()) == [MANIFEST_FILE, INDEX_FILE]


def test_saving_creates_the_folder(
    index: FaissVectorIndex, fake_settings: Settings, tmp_path: Path
) -> None:
    directory = tmp_path / "a" / "b"

    save_index(index, FakeEmbedder(), fake_settings, directory)

    assert (directory / MANIFEST_FILE).is_file()


def test_an_index_and_a_model_with_different_vector_sizes_are_not_saved(
    fake_settings: Settings, tmp_path: Path
) -> None:
    with pytest.raises(VectorIndexError, match="different vector sizes"):
        save_index(FaissVectorIndex(8), FakeEmbedder(), fake_settings, tmp_path)

    assert not (tmp_path / MANIFEST_FILE).exists()


def test_save_and_load_are_logged_without_content(
    index: FaissVectorIndex,
    fake_settings: Settings,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO):
        save_index(index, FakeEmbedder(), fake_settings, tmp_path)
        load_index(fake_settings, tmp_path)

    messages = [record.message for record in caplog.records]
    assert "vector index saved" in messages
    assert "vector index loaded" in messages


# --- reasons to refuse ------------------------------------------------------------------


def test_no_index_yet(fake_settings: Settings, tmp_path: Path) -> None:
    with pytest.raises(IndexLoadError, match="no index has been built"):
        load_index(fake_settings, tmp_path / "missing")


def test_an_index_built_with_another_model_is_refused(saved: Path, settings: Settings) -> None:
    # The service is set to the real model; the folder holds the fake one's vectors.
    with pytest.raises(IndexLoadError, match="built with fake-embedder"):
        load_index(settings, saved)


@pytest.mark.parametrize(
    "changes",
    [{"chunk_max_characters": 400}, {"chunk_overlap_characters": 60}],
)
def test_an_index_built_with_other_chunk_settings_is_refused(
    saved: Path, fake_settings: Settings, changes: dict[str, int]
) -> None:
    with pytest.raises(IndexLoadError, match="chunk settings"):
        load_index(fake_settings.model_copy(update=changes), saved)


def test_a_changed_index_file_is_refused(saved: Path, fake_settings: Settings) -> None:
    path = saved / INDEX_FILE
    data = bytearray(path.read_bytes())
    data[-10] ^= 0xFF
    path.write_bytes(bytes(data))

    with pytest.raises(IndexLoadError, match="does not match its manifest"):
        load_index(fake_settings, saved)


def test_an_index_file_from_another_build_is_refused(
    saved: Path, fake_settings: Settings, tmp_path: Path
) -> None:
    # What a crash between replacing the two files would leave behind.
    other = FaissVectorIndex(DIMENSION)
    other.add([1], unit_vectors(1, seed=8))
    (saved / INDEX_FILE).write_bytes(other.to_bytes())

    with pytest.raises(IndexLoadError, match="does not match its manifest"):
        load_index(fake_settings, saved)


@pytest.mark.parametrize("stop_before", [INDEX_FILE, MANIFEST_FILE])
def test_a_save_that_stops_halfway_leaves_a_folder_that_is_refused(
    saved: Path, fake_settings: Settings, monkeypatch: pytest.MonkeyPatch, stop_before: str
) -> None:
    import app.retrieval.index_store as store

    real_write = store.write_atomically

    def write_until(path: Path, data: bytes) -> None:
        if path.name == stop_before:
            raise OSError("the process stopped")
        real_write(path, data)

    monkeypatch.setattr(store, "write_atomically", write_until)
    other = FaissVectorIndex(DIMENSION)
    other.add([1], unit_vectors(1, seed=8))

    with pytest.raises(OSError):
        save_index(other, FakeEmbedder(), fake_settings, saved)

    if stop_before == INDEX_FILE:
        # Nothing was replaced: the previous index still loads.
        assert load_index(fake_settings, saved)[0].size == 40
    else:
        with pytest.raises(IndexLoadError, match="does not match its manifest"):
            load_index(fake_settings, saved)


def test_a_missing_index_file_is_refused(saved: Path, fake_settings: Settings) -> None:
    (saved / INDEX_FILE).unlink()

    with pytest.raises(IndexLoadError, match="index file is missing"):
        load_index(fake_settings, saved)


@pytest.mark.parametrize("content", ["", "not json", "[]", '{"format_version": 1}', "\x00\x01"])
def test_a_manifest_that_cannot_be_read_is_refused(
    saved: Path, fake_settings: Settings, content: str
) -> None:
    (saved / MANIFEST_FILE).write_text(content, encoding="utf-8")

    with pytest.raises(IndexLoadError, match="manifest can't be read"):
        load_index(fake_settings, saved)


def test_a_manifest_that_is_not_text_is_refused(saved: Path, fake_settings: Settings) -> None:
    (saved / MANIFEST_FILE).write_bytes(b"\xff\xfe\xfa")

    with pytest.raises(IndexLoadError, match="manifest can't be read"):
        load_index(fake_settings, saved)


def test_a_manifest_with_an_unknown_field_is_refused(saved: Path, fake_settings: Settings) -> None:
    edit_manifest(saved, notes="hand edited")

    with pytest.raises(IndexLoadError, match="manifest can't be read"):
        load_index(fake_settings, saved)


def test_an_unknown_format_version_is_refused(saved: Path, fake_settings: Settings) -> None:
    edit_manifest(saved, format_version=1)

    with pytest.raises(IndexLoadError, match="format 1 is not supported"):
        load_index(fake_settings, saved)


@pytest.mark.parametrize("changes", [{"chunk_count": 41}, {"dimension": 32}])
def test_a_manifest_that_disagrees_with_the_index_is_refused(
    saved: Path, fake_settings: Settings, changes: dict[str, int]
) -> None:
    edit_manifest(saved, **changes)

    with pytest.raises(IndexLoadError, match="does not match its manifest"):
        load_index(fake_settings, saved)


def write_raw(directory: Path, raw: Any, fake_settings: Settings) -> None:
    """Put an arbitrary FAISS index in the folder with a manifest that matches it."""
    data = bytes(faiss.serialize_index(raw))
    (directory / INDEX_FILE).write_bytes(data)
    edit_manifest(
        directory,
        index_sha256=hashlib.sha256(data).hexdigest(),
        chunk_count=int(raw.ntotal),
        dimension=int(raw.d),
    )


def test_an_index_that_is_not_keyed_by_chunk_id_is_refused(
    saved: Path, fake_settings: Settings
) -> None:
    raw = faiss.IndexFlatIP(DIMENSION)
    raw.add(unit_vectors(3))
    write_raw(saved, raw, fake_settings)

    with pytest.raises(IndexLoadError, match="not keyed by chunk ID"):
        load_index(fake_settings, saved)


def test_an_index_that_measures_distance_instead_of_similarity_is_refused(
    saved: Path, fake_settings: Settings
) -> None:
    raw = faiss.IndexIDMap2(faiss.IndexFlatL2(DIMENSION))
    raw.add_with_ids(unit_vectors(3), np.array([1, 2, 3], dtype=np.int64))
    write_raw(saved, raw, fake_settings)

    with pytest.raises(IndexLoadError, match="exact inner-product"):
        load_index(fake_settings, saved)


def test_an_index_with_repeated_ids_is_refused(saved: Path, fake_settings: Settings) -> None:
    raw = faiss.IndexIDMap2(faiss.IndexFlatIP(DIMENSION))
    raw.add_with_ids(unit_vectors(2), np.array([4, 4], dtype=np.int64))
    write_raw(saved, raw, fake_settings)

    with pytest.raises(IndexLoadError, match="repeated or invalid"):
        load_index(fake_settings, saved)


def test_index_data_that_is_not_an_index_is_refused(saved: Path, fake_settings: Settings) -> None:
    data = b"this is not a FAISS index"
    (saved / INDEX_FILE).write_bytes(data)
    edit_manifest(saved, index_sha256=hashlib.sha256(data).hexdigest())

    with pytest.raises(IndexLoadError, match="could not be read"):
        load_index(fake_settings, saved)


def test_a_setting_for_the_index_folder_exists() -> None:
    settings = Settings(_env_file=None, jwt_secret_key=SecretStr("x" * 64))

    assert settings.index_directory == Path("data/index")

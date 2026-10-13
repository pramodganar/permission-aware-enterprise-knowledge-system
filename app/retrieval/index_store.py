"""Saving the vector index to disk and loading it back.

An index is two files in one folder:

    vectors.faiss   the index itself
    manifest.json   what built it: the embedding model, the vector size, the
                    chunk settings, the number of chunks and a checksum of
                    vectors.faiss

Loading checks the files against each other and against the current settings,
and refuses on any mismatch. An index built by another model would still
return results, just meaningless ones, and nothing downstream could tell.
Refusing is the only safe answer (NFR-6).

The checksum is checked before the index file is parsed, so a damaged or
half-written file is never handed to the FAISS reader. It protects against
accidents, not against someone who can rewrite both files.
"""

import hashlib
import json
import logging
import os
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.core.config import Settings
from app.core.database import utc_now
from app.retrieval.embeddings import Embedder
from app.retrieval.index import FaissVectorIndex, VectorIndexError

logger = logging.getLogger(__name__)

INDEX_FILE = "vectors.faiss"
MANIFEST_FILE = "manifest.json"
FORMAT_VERSION = 1


class IndexLoadError(VectorIndexError):
    """There is no usable index in the folder."""


class IndexManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    format_version: int
    embedding_model: str
    dimension: int = Field(gt=0)
    chunk_max_characters: int
    chunk_overlap_characters: int
    chunk_count: int = Field(ge=0)
    index_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    built_at: datetime


def save_index(
    index: FaissVectorIndex, embedder: Embedder, settings: Settings, directory: Path
) -> IndexManifest:
    """Write the index and its manifest to ``directory``.

    Each file is replaced with an atomic rename, so neither is ever half
    written. The two renames are not one step: if the process stops between
    them, the folder holds a new file and an old one, the checksum in the
    manifest no longer matches, and loading refuses the folder. It is never
    left looking valid with files from two builds.
    """
    if index.dimension != embedder.dimension:
        raise VectorIndexError("the index and the embedding model have different vector sizes")

    data = index.to_bytes()
    manifest = IndexManifest(
        format_version=FORMAT_VERSION,
        embedding_model=embedder.name,
        dimension=index.dimension,
        chunk_max_characters=settings.chunk_max_characters,
        chunk_overlap_characters=settings.chunk_overlap_characters,
        chunk_count=index.size,
        index_sha256=hashlib.sha256(data).hexdigest(),
        built_at=utc_now(),
    )

    directory.mkdir(parents=True, exist_ok=True)
    _write_atomically(directory / INDEX_FILE, data)
    _write_atomically(
        directory / MANIFEST_FILE, (manifest.model_dump_json(indent=2) + "\n").encode()
    )
    logger.info(
        "vector index saved",
        extra={"chunks": manifest.chunk_count, "model": manifest.embedding_model},
    )
    return manifest


def load_index(settings: Settings, directory: Path) -> tuple[FaissVectorIndex, IndexManifest]:
    """Load the index in ``directory`` and check it can be used with these settings."""
    manifest = _read_manifest(directory)

    if manifest.format_version != FORMAT_VERSION:
        raise IndexLoadError(f"the index format {manifest.format_version} is not supported")
    if manifest.embedding_model != settings.embedding_model:
        raise IndexLoadError(
            f"the index was built with {manifest.embedding_model}, "
            f"but the service is set to use {settings.embedding_model}"
        )
    built_with = (manifest.chunk_max_characters, manifest.chunk_overlap_characters)
    configured = (settings.chunk_max_characters, settings.chunk_overlap_characters)
    if built_with != configured:
        raise IndexLoadError(
            f"the index was built with chunk settings {built_with}, not {configured}"
        )

    try:
        data = (directory / INDEX_FILE).read_bytes()
    except FileNotFoundError:
        raise IndexLoadError("the index file is missing") from None
    if hashlib.sha256(data).hexdigest() != manifest.index_sha256:
        raise IndexLoadError("the index file does not match its manifest")

    try:
        index = FaissVectorIndex.from_bytes(data)
    except VectorIndexError as error:
        raise IndexLoadError(str(error)) from None
    if index.dimension != manifest.dimension or index.size != manifest.chunk_count:
        raise IndexLoadError("the index does not match its manifest")

    logger.info(
        "vector index loaded",
        extra={
            "chunks": index.size,
            "model": manifest.embedding_model,
            "built_at": manifest.built_at.isoformat(),
        },
    )
    return index, manifest


def _read_manifest(directory: Path) -> IndexManifest:
    try:
        data = (directory / MANIFEST_FILE).read_bytes()
    except FileNotFoundError:
        raise IndexLoadError("no index has been built yet") from None
    try:
        return IndexManifest.model_validate(json.loads(data.decode("utf-8")))
    except (UnicodeDecodeError, json.JSONDecodeError, ValidationError):
        raise IndexLoadError("the index manifest can't be read") from None


def _write_atomically(path: Path, data: bytes) -> None:
    """Write to a temporary file next to ``path``, then rename it into place."""
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("wb") as file:
        file.write(data)
        file.flush()
        os.fsync(file.fileno())
    os.replace(temporary, path)

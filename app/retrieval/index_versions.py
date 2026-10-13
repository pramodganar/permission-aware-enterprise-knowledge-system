"""Versions of the vector index: publishing, rolling back, and noticing when one is out of date.

The index folder holds every kept version and a pointer to the one in use:

    data/index/
      current            the number of the version in use, e.g. "000003"
      versions/
        000002/          vectors.faiss, manifest.json
        000003/

A new build never touches a version that exists. It is written to a new
folder, loaded back to check it, and only then made current by replacing the
pointer, which is one file and one atomic rename. Until that moment the
previous version stays in use, so a failed or interrupted build changes
nothing. Rolling back is moving the pointer to an older version.

An index is out of date when the chunks in the database are not the ones it
was built from: a document was added, replaced or deleted, or re-chunked. That
is a matter of completeness, not of access. A chunk that is in the index but
no longer readable is still never found, because nobody's allowed list
contains it.
"""

import hashlib
import logging
import re
import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.models import Chunk
from app.retrieval.embeddings import Embedder, embedding_text
from app.retrieval.index import (
    FaissVectorIndex,
    VectorIndexError,
    active_chunks,
    index_from_chunks,
)
from app.retrieval.index_store import (
    IndexLoadError,
    IndexManifest,
    load_index,
    save_index,
    write_atomically,
)

logger = logging.getLogger(__name__)

POINTER_FILE = "current"
VERSIONS_FOLDER = "versions"
# The pointer's content is used to build a path, so it must be exactly this.
_VERSION_NAME = re.compile(r"^\d{6}$")


@dataclass(frozen=True)
class IndexVersion:
    number: int
    manifest: IndexManifest | None  # None when the version can't be loaded
    problem: str | None
    current: bool


@dataclass(frozen=True)
class Freshness:
    up_to_date: bool
    indexed_chunks: int
    active_chunks: int
    # Chunks in the database the index doesn't have: not searchable yet.
    missing: int
    # Chunks in the index that are no longer active: never found, but stale.
    retired: int


def content_fingerprint(chunks: Sequence[Chunk]) -> str:
    """A SHA-256 over each chunk ID and the exact text embedded for it.

    Any change that would make a vector wrong changes it: a chunk added or
    removed, its text changed, or its document's title changed.
    """
    digest = hashlib.sha256()
    for chunk in sorted(chunks, key=lambda chunk: chunk.id):
        text = embedding_text(chunk.document.title, chunk.text).encode("utf-8")
        digest.update(f"{chunk.id}:{hashlib.sha256(text).hexdigest()}\n".encode())
    return digest.hexdigest()


def publish_index(db: Session, embedder: Embedder, settings: Settings, root: Path) -> IndexManifest:
    """Build a new version from the database, check it, and make it current."""
    chunks = active_chunks(db)
    index = index_from_chunks(chunks, embedder)

    versions = root / VERSIONS_FOLDER
    versions.mkdir(parents=True, exist_ok=True)
    number = max(_version_numbers(root), default=0) + 1
    folder = versions / _name(number)
    # exist_ok=False: two builds running at once can't write to the same folder.
    folder.mkdir()

    save_index(
        index,
        embedder,
        settings,
        folder,
        version=number,
        content_sha256=content_fingerprint(chunks),
    )
    # Read it back before anyone is pointed at it.
    _, manifest = load_index(settings, folder)
    _point_to(root, number)
    logger.info("index version published", extra={"version": number, "chunks": index.size})

    prune_versions(root, settings.index_keep_versions)
    return manifest


def load_current_index(settings: Settings, root: Path) -> tuple[FaissVectorIndex, IndexManifest]:
    """Load the version the pointer names."""
    return load_index(settings, _folder(root, current_version(root)))


def current_version(root: Path) -> int:
    try:
        name = (root / POINTER_FILE).read_text(encoding="ascii").strip()
    except FileNotFoundError:
        raise IndexLoadError("no index has been published yet") from None
    except UnicodeDecodeError:
        raise IndexLoadError("the index pointer can't be read") from None
    if not _VERSION_NAME.match(name):
        raise IndexLoadError("the index pointer can't be read")
    return int(name)


def list_versions(settings: Settings, root: Path) -> list[IndexVersion]:
    """Every kept version, newest first, and whether it loads with these settings."""
    try:
        current: int | None = current_version(root)
    except IndexLoadError:
        current = None

    found = []
    for number in sorted(_version_numbers(root), reverse=True):
        try:
            _, manifest = load_index(settings, _folder(root, number))
            found.append(IndexVersion(number, manifest, None, number == current))
        except VectorIndexError as error:
            found.append(IndexVersion(number, None, str(error), number == current))
    return found


def rollback_index(settings: Settings, root: Path, to_version: int | None = None) -> IndexManifest:
    """Make an older version current.

    Without a version number, goes to the newest version below the current one
    that loads with the current settings. The target is always loaded and
    checked before the pointer moves.
    """
    current = current_version(root)
    if to_version is None:
        candidates = [
            version.number
            for version in list_versions(settings, root)
            if version.number < current and version.manifest is not None
        ]
        if not candidates:
            raise IndexLoadError("there is no earlier version that can be used")
        to_version = candidates[0]
    if to_version == current:
        raise IndexLoadError(f"version {to_version} is already current")
    if to_version not in _version_numbers(root):
        raise IndexLoadError(f"there is no version {to_version}")

    _, manifest = load_index(settings, _folder(root, to_version))
    _point_to(root, to_version)
    logger.warning("index rolled back", extra={"from_version": current, "to_version": to_version})
    return manifest


def prune_versions(root: Path, keep: int) -> list[int]:
    """Delete all but the newest ``keep`` versions. Never deletes the current one."""
    if keep < 1:
        raise ValueError("at least one version must be kept")
    current = current_version(root)
    newest = sorted(_version_numbers(root), reverse=True)
    removed = [number for number in newest[keep:] if number != current]
    for number in removed:
        shutil.rmtree(_folder(root, number))
    if removed:
        logger.info("old index versions removed", extra={"versions": removed})
    return removed


def check_freshness(db: Session, index: FaissVectorIndex, manifest: IndexManifest) -> Freshness:
    """Compare an index with the chunks that are in the database now."""
    chunks = active_chunks(db)
    in_database = {chunk.id for chunk in chunks}
    up_to_date = (
        manifest.content_sha256 is not None
        and manifest.content_sha256 == content_fingerprint(chunks)
    )
    return Freshness(
        up_to_date=up_to_date,
        indexed_chunks=index.size,
        active_chunks=len(in_database),
        missing=len(in_database - index.chunk_ids),
        retired=len(index.chunk_ids - in_database),
    )


def _version_numbers(root: Path) -> set[int]:
    versions = root / VERSIONS_FOLDER
    if not versions.is_dir():
        return set()
    return {
        int(path.name)
        for path in versions.iterdir()
        if path.is_dir() and _VERSION_NAME.match(path.name)
    }


def _name(number: int) -> str:
    return f"{number:06d}"


def _folder(root: Path, number: int) -> Path:
    return root / VERSIONS_FOLDER / _name(number)


def _point_to(root: Path, number: int) -> None:
    write_atomically(root / POINTER_FILE, f"{_name(number)}\n".encode("ascii"))

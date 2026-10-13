"""The vector index: chunk IDs and their vectors, and nothing else.

The index knows no departments, classifications or users. It holds every
active chunk, the restricted ones included. What keeps a search inside a
user's access is the list of chunk IDs passed to ``search``: the search only
looks at those vectors (decision record 0003).

That list is a required argument with no default. There is no way to search
"everything" by leaving it out, and an empty list finds nothing.

Vectors are unit length, so the inner product used here is the cosine
similarity. The search is exact: every allowed vector is compared.
"""

import logging
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from typing import Protocol

import faiss
import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Chunk, Document, DocumentStatus
from app.retrieval.embeddings import Embedder, Vectors, embed_chunks

logger = logging.getLogger(__name__)

# The most results one search may ask for. A search is for a handful of chunks
# to put in front of the LLM, not for exporting the index.
MAX_SEARCH_LIMIT = 100


class VectorIndexError(Exception):
    """The index was given something it can't use, or returned something it must not."""


@dataclass(frozen=True)
class Hit:
    chunk_id: int
    # Cosine similarity, from -1 to 1. Higher is closer.
    score: float


class VectorIndex(Protocol):
    dimension: int

    @property
    def size(self) -> int: ...

    @property
    def chunk_ids(self) -> frozenset[int]: ...

    def add(self, chunk_ids: Sequence[int], vectors: Vectors) -> None: ...

    def search(self, query: Vectors, limit: int, allowed_ids: Collection[int]) -> list[Hit]: ...


class FaissVectorIndex:
    """An exact inner-product index in memory, keyed by chunk ID."""

    def __init__(self, dimension: int) -> None:
        if dimension < 1:
            raise VectorIndexError("the vector size must be at least 1")
        self.dimension = dimension
        self._index = faiss.IndexIDMap2(faiss.IndexFlatIP(dimension))
        self._ids: set[int] = set()

    @classmethod
    def from_bytes(cls, data: bytes) -> "FaissVectorIndex":
        """Rebuild an index from ``to_bytes``. Refuses anything but this kind of index."""
        try:
            raw = faiss.deserialize_index(np.frombuffer(data, dtype=np.uint8))
        except Exception:
            raise VectorIndexError("the index data could not be read") from None

        # Another kind of index could search approximately, or by distance
        # instead of similarity, and still look as if it worked.
        if not isinstance(raw, faiss.IndexIDMap2):
            raise VectorIndexError("the index is not keyed by chunk ID")
        if not isinstance(faiss.downcast_index(raw.index), faiss.IndexFlatIP):
            raise VectorIndexError("the index is not an exact inner-product index")

        ids = [int(chunk_id) for chunk_id in faiss.vector_to_array(raw.id_map)]
        if len(set(ids)) != len(ids) or any(chunk_id < 1 for chunk_id in ids):
            raise VectorIndexError("the index has repeated or invalid chunk IDs")

        index = cls(int(raw.d))
        index._index = raw
        index._ids = set(ids)
        return index

    def to_bytes(self) -> bytes:
        return bytes(faiss.serialize_index(self._index))

    @property
    def size(self) -> int:
        return len(self._ids)

    @property
    def chunk_ids(self) -> frozenset[int]:
        return frozenset(self._ids)

    def add(self, chunk_ids: Sequence[int], vectors: Vectors) -> None:
        """Add vectors under their chunk IDs. Either all are added or none."""
        ids = list(chunk_ids)
        if vectors.shape != (len(ids), self.dimension):
            raise VectorIndexError(
                f"expected {len(ids)} vectors of size {self.dimension}, got shape {vectors.shape}"
            )
        if not np.isfinite(vectors).all():
            raise VectorIndexError("a vector has a value that is not a number")
        if any(chunk_id < 1 for chunk_id in ids):
            raise VectorIndexError("chunk IDs must be positive")
        # FAISS would accept the same ID twice and return it twice.
        if len(set(ids)) != len(ids) or not self._ids.isdisjoint(ids):
            raise VectorIndexError("a chunk ID is already in the index")
        if not ids:
            return

        self._index.add_with_ids(
            np.ascontiguousarray(vectors, dtype=np.float32), np.asarray(ids, dtype=np.int64)
        )
        self._ids.update(ids)

    def search(self, query: Vectors, limit: int, allowed_ids: Collection[int]) -> list[Hit]:
        """Return the closest chunks among ``allowed_ids``, best first.

        Only the allowed vectors are compared. A closer vector outside the
        list is not found and does not use up a place in the result.
        """
        if not 1 <= limit <= MAX_SEARCH_LIMIT:
            raise VectorIndexError(f"the limit must be between 1 and {MAX_SEARCH_LIMIT}")
        if query.shape != (self.dimension,) or not np.isfinite(query).all():
            raise VectorIndexError(f"the query must be one vector of size {self.dimension}")

        # An ID that isn't in the index can't be found, so it is dropped here.
        allowed = self._ids.intersection(allowed_ids)
        if not allowed:
            return []

        # The selector and the parameters stay in scope until the search returns.
        selector = faiss.IDSelectorBatch(np.fromiter(allowed, dtype=np.int64, count=len(allowed)))
        parameters = faiss.SearchParameters()
        parameters.sel = selector
        scores, ids = self._index.search(
            np.ascontiguousarray(query.reshape(1, -1), dtype=np.float32),
            min(limit, len(allowed)),
            params=parameters,
        )

        hits = [
            Hit(chunk_id=int(chunk_id), score=float(score))
            for chunk_id, score in zip(ids[0], scores[0], strict=True)
            if chunk_id != -1  # FAISS pads a short result with -1
        ]
        # Expected never to fire. If the library ever returned an ID outside the
        # list, that is a leak, and failing is better than returning it.
        outside = [hit.chunk_id for hit in hits if hit.chunk_id not in allowed]
        if outside:
            logger.error("vector search returned chunks outside the allowed list")
            raise VectorIndexError("the search returned a chunk outside the allowed list")
        return hits


def active_chunks(db: Session) -> list[Chunk]:
    """Every chunk of every active document, in ID order.

    Chunks of replaced and deleted documents are left out. They could not be
    found anyway, since nobody's allowed list contains them, but there is no
    reason to keep their vectors.
    """
    return list(
        db.scalars(
            select(Chunk)
            .join(Document, Chunk.document_id == Document.id)
            .where(Document.status == DocumentStatus.ACTIVE)
            .order_by(Chunk.id)
        )
    )


def build_index(db: Session, embedder: Embedder) -> FaissVectorIndex:
    """Embed the chunks of every active document and return an index of them."""
    return index_from_chunks(active_chunks(db), embedder)


def index_from_chunks(chunks: Sequence[Chunk], embedder: Embedder) -> FaissVectorIndex:
    index = FaissVectorIndex(embedder.dimension)
    index.add([chunk.id for chunk in chunks], embed_chunks(chunks, embedder))
    logger.info(
        "vector index built",
        extra={"chunks": index.size, "dimension": index.dimension, "model": embedder.name},
    )
    return index

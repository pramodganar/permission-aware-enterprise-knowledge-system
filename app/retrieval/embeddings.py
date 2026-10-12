"""Turning text into vectors.

One model embeds both the document chunks and the questions asked about them.
It runs inside the service, so no document text is sent anywhere to be
embedded (decision record 0009).

Everything else depends on the ``Embedder`` interface and not on a particular
library, so tests can use a small fake and the model can be changed by
configuration. Changing it means embedding every chunk again: vectors from two
models can't be compared.

A vector is derived from the text and is as sensitive as the text. This
module has no idea who is asking. Deciding which vectors a search may look at
is the retriever's job.
"""

import logging
from collections.abc import Sequence
from typing import Protocol

import numpy as np
from numpy.typing import NDArray

from app.core.config import Settings
from app.models import Chunk

logger = logging.getLogger(__name__)

Vectors = NDArray[np.float32]

# How far from length 1 a vector may be and still count as normalised.
_UNIT_TOLERANCE = 1e-3


class EmbeddingError(Exception):
    """Text could not be embedded, or the model returned something unusable."""


class Embedder(Protocol):
    """A model that turns text into unit-length vectors."""

    # Recorded with an index, so a query is never compared with vectors from another model.
    name: str
    dimension: int
    # The model reads this many tokens of a text and ignores the rest without an error.
    max_tokens: int

    def count_tokens(self, text: str) -> int:
        """How many tokens the model sees for this text, special tokens included."""
        ...

    def embed_documents(self, texts: Sequence[str]) -> Vectors:
        """One row per text."""
        ...

    def embed_query(self, text: str) -> Vectors:
        """A single vector for a question."""
        ...


class SentenceTransformerEmbedder:
    """A sentence-transformers model, run locally on the CPU."""

    def __init__(self, model_name: str, batch_size: int = 32, offline: bool = False) -> None:
        # Imported here because it takes many seconds and most code never needs it.
        from sentence_transformers import SentenceTransformer

        # offline=True loads only from the local cache and makes no network call.
        self._model = SentenceTransformer(model_name, device="cpu", local_files_only=offline)
        self._batch_size = batch_size
        dimension = self._model.get_embedding_dimension()
        max_tokens = self._model.max_seq_length
        if dimension is None or max_tokens is None:
            raise EmbeddingError(f"the model {model_name} does not report its sizes")

        self.name = model_name
        self.dimension = dimension
        self.max_tokens = int(max_tokens)
        logger.info(
            "embedding model loaded",
            extra={"model": self.name, "dimension": self.dimension, "max_tokens": self.max_tokens},
        )

    def count_tokens(self, text: str) -> int:
        return len(self._model.tokenizer(text)["input_ids"])

    def embed_documents(self, texts: Sequence[str]) -> Vectors:
        vectors = self._model.encode_document(
            list(texts),
            batch_size=self._batch_size,
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=False,
        )
        return np.asarray(vectors, dtype=np.float32)

    def embed_query(self, text: str) -> Vectors:
        vector = self._model.encode_query(
            text, normalize_embeddings=True, convert_to_numpy=True, show_progress_bar=False
        )
        return np.asarray(vector, dtype=np.float32)


def load_embedder(settings: Settings) -> Embedder:
    """Load the embedding model named in the settings. Slow: do it once."""
    return SentenceTransformerEmbedder(
        settings.embedding_model,
        batch_size=settings.embedding_batch_size,
        offline=settings.embedding_offline,
    )


def embedding_text(title: str, chunk_text: str) -> str:
    """The text that is embedded for a chunk: its document's title, then the chunk.

    A chunk such as "Up to 5,000 EUR: Finance Analyst" does not say what the
    limit is for. The title does. The stored chunk text is left as it is.
    """
    return f"{title}\n{chunk_text}"


def embed_chunks(chunks: Sequence[Chunk], embedder: Embedder) -> Vectors:
    """Return one unit-length vector per chunk, in the same order.

    Raises EmbeddingError if a chunk is longer than the model can read. The
    model would not complain: it would embed the start and drop the rest, and
    the dropped text could then be cited but never found.
    """
    texts = [embedding_text(chunk.document.title, chunk.text) for chunk in chunks]
    if not texts:
        return np.empty((0, embedder.dimension), dtype=np.float32)

    for chunk, text in zip(chunks, texts, strict=True):
        tokens = embedder.count_tokens(text)
        if tokens > embedder.max_tokens:
            # The message names the chunk, not its text.
            raise EmbeddingError(
                f"chunk {chunk.id} of document {chunk.document_id} is {tokens} tokens, "
                f"more than the {embedder.max_tokens} that {embedder.name} reads"
            )

    vectors = _checked(embedder.embed_documents(texts), len(texts), embedder)
    logger.info("chunks embedded", extra={"chunks": len(texts), "model": embedder.name})
    return vectors


def embed_question(question: str, embedder: Embedder) -> Vectors:
    """Return the unit-length vector for one question."""
    vector = np.asarray(embedder.embed_query(question), dtype=np.float32)
    checked: Vectors = _checked(vector.reshape(1, -1), 1, embedder)[0]
    return checked


def _checked(vectors: Vectors, count: int, embedder: Embedder) -> Vectors:
    """Refuse vectors that a search could not use.

    A wrong shape, a not-a-number or a vector that isn't unit length would not
    stop a similarity search. It would quietly return wrong neighbours.
    """
    if vectors.shape != (count, embedder.dimension):
        raise EmbeddingError(
            f"expected {count} vectors of size {embedder.dimension}, got shape {vectors.shape}"
        )
    if not np.isfinite(vectors).all():
        raise EmbeddingError("the model returned a vector with a value that is not a number")
    lengths = np.linalg.norm(vectors, axis=1)
    if not np.allclose(lengths, 1.0, atol=_UNIT_TOLERANCE):
        raise EmbeddingError("the model returned a vector that is not unit length")
    return vectors.astype(np.float32, copy=False)

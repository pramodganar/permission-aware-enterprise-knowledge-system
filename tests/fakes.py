"""Stand-ins for the slow or external parts of the system."""

import hashlib
import re
from collections.abc import Sequence

import numpy as np

from app.retrieval.embeddings import Vectors

_WORDS = re.compile(r"[a-z0-9]+")


class FakeEmbedder:
    """A deterministic embedder that needs no model.

    Each word adds to one of ``dimension`` slots, chosen by a hash of the word,
    and the result is scaled to length 1. Texts that share words get similar
    vectors, which is enough to test everything around the model.
    """

    def __init__(self, dimension: int = 64, max_tokens: int = 256) -> None:
        self.name = "fake-embedder"
        self.dimension = dimension
        self.max_tokens = max_tokens
        self.embedded: list[str] = []

    def count_tokens(self, text: str) -> int:
        return len(text.split())

    def embed_documents(self, texts: Sequence[str]) -> Vectors:
        self.embedded.extend(texts)
        return np.stack([self._vector(text) for text in texts])

    def embed_query(self, text: str) -> Vectors:
        return self._vector(text)

    def _vector(self, text: str) -> Vectors:
        vector = np.zeros(self.dimension, dtype=np.float32)
        for word in _WORDS.findall(text.lower()):
            slot = int.from_bytes(hashlib.sha256(word.encode()).digest()[:4], "big")
            vector[slot % self.dimension] += 1.0
        length = float(np.linalg.norm(vector))
        if length == 0.0:
            vector[0] = 1.0
            return vector
        return vector / length


class FakeReranker:
    """A reranker that needs no model.

    By default it scores a passage by how many of the question's words it
    contains. ``scores`` replaces that with fixed values, in order. Every call
    is recorded, so tests can see exactly what text reached it.
    """

    def __init__(self, scores: Sequence[float] | None = None, max_tokens: int = 512) -> None:
        self.name = "fake-reranker"
        self.max_tokens = max_tokens
        self.fixed = list(scores) if scores is not None else None
        self.calls: list[tuple[str, list[str]]] = []

    def count_tokens(self, question: str, passage: str) -> int:
        return len(question.split()) + len(passage.split())

    def score(self, question: str, passages: Sequence[str]) -> list[float]:
        self.calls.append((question, list(passages)))
        if self.fixed is not None:
            # As given, even if the count is wrong: tests use that on purpose.
            return list(self.fixed)
        asked = set(_WORDS.findall(question.lower()))
        return [float(len(asked & set(_WORDS.findall(p.lower())))) for p in passages]

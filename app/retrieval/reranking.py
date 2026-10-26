"""Reordering retrieved chunks with a model that reads question and chunk together.

The vector search compares a question vector with chunk vectors that were
made separately. A cross-encoder reads the question and one chunk side by
side, which is slower but can judge better which chunk answers the question.

It is off by default. Measured on the company documents, it put the right
document first no more often than the vector search did (21 of 22 questions
either way) and added about 600 ms per question on a CPU (docs/retrieval.md).
It is here so that it can be switched on, and measured again, when there are
enough documents for the order of the results to matter.

A reranker only ever sees chunks that have already passed both access
checks. It changes their order, never which chunks there are.
"""

import logging
import math
from collections.abc import Sequence
from typing import Protocol

from app.core.config import Settings
from app.retrieval.embeddings import no_progress_bars

logger = logging.getLogger(__name__)


class RerankError(Exception):
    """The reranker returned something that can't be used to order chunks."""


class Reranker(Protocol):
    name: str
    # Question and passage together, special tokens included.
    max_tokens: int

    def count_tokens(self, question: str, passage: str) -> int: ...

    def score(self, question: str, passages: Sequence[str]) -> list[float]:
        """One score per passage, higher for a better answer. Not a probability."""
        ...


class CrossEncoderReranker:
    """A sentence-transformers cross-encoder, run locally on the CPU."""

    def __init__(self, model_name: str, offline: bool = False) -> None:
        # Imported here for the same reason as in embeddings.py: it is slow.
        from sentence_transformers import CrossEncoder

        no_progress_bars()
        self._model = CrossEncoder(model_name, device="cpu", local_files_only=offline)
        self.name = model_name
        self.max_tokens = int(self._model.max_seq_length)
        logger.info("reranker loaded", extra={"model": self.name, "max_tokens": self.max_tokens})

    def count_tokens(self, question: str, passage: str) -> int:
        return len(self._model.tokenizer(question, passage)["input_ids"])

    def score(self, question: str, passages: Sequence[str]) -> list[float]:
        if not passages:
            return []
        scores = self._model.predict(
            [(question, passage) for passage in passages],
            show_progress_bar=False,
            convert_to_numpy=True,
        )
        return [float(value) for value in scores]


def load_reranker(settings: Settings) -> Reranker | None:
    """The reranker named in the settings, or None if reranking is off."""
    if settings.reranker_model is None:
        return None
    return CrossEncoderReranker(settings.reranker_model, offline=settings.embedding_offline)


def checked_scores(scores: Sequence[float], count: int) -> list[float]:
    """Refuse scores that would order the chunks wrongly without an error.

    A missing score would shift every chunk after it; a NaN compares false
    with everything, so a sort would leave it anywhere.
    """
    if len(scores) != count:
        raise RerankError(f"expected {count} scores, got {len(scores)}")
    if not all(math.isfinite(value) for value in scores):
        raise RerankError("the reranker returned a score that is not a number")
    return list(scores)

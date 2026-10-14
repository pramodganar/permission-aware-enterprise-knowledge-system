"""From a person and a question to the chunks they may read that answer it.

This is the one function the rest of the application calls to retrieve. It
works out the person's access scope, searches inside it, loads the chunks
that were found, and checks each chunk's document again with ``can_read``
before it is returned (decision record 0003).

The two checks are written differently on purpose. The scope comes from a
SQL query (``readable_chunks``); the second check is the same rule written in
Python, applied to the loaded document. A mistake in one of them is unlikely
to be the same mistake in the other, so a chunk only gets through if both
agree. The second check should never remove anything. When it does, the
chunk is dropped, an error is logged, and the rest of the answer goes on: the
chunks left have passed both checks.

Both checks use the moment the scope was decided, so a grant that expires
during the request is judged the same way by both.

Last, the relevance gate. If the best chunk that passed both checks scores
below ``min_score``, nothing here is about the question and nothing is
returned, so the answer is "no information available" and the LLM is not
called. The gate looks at the best chunk only, not at each one: measured on
the company data, a per-chunk cut-off removes real answers that rank lower
(docs/retrieval.md).
"""

import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.authorization.service import can_read
from app.models import Chunk, Document, User
from app.retrieval.index import VectorIndex
from app.retrieval.query import ProcessedQuestion
from app.retrieval.scope import access_scope
from app.retrieval.search import search_in_scope

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RetrievedChunk:
    """A chunk the person may read, with what is needed to cite it."""

    chunk_id: int
    document_id: int
    document_title: str
    document_version: int
    position: int
    text: str
    # Cosine similarity to the question, from -1 to 1.
    score: float


@dataclass(frozen=True)
class Retrieval:
    user_id: int
    chunks: tuple[RetrievedChunk, ...]
    # Hits removed by the second check. Expected to be 0 every time.
    dropped: int
    # The best score among the chunks that passed both checks, None if there
    # were none. Kept when the gate empties the result, to tune the threshold.
    best_score: float | None = None
    # True when chunks were found but the best was below the threshold.
    below_threshold: bool = False

    @property
    def is_empty(self) -> bool:
        return not self.chunks


def retrieve(
    db: Session,
    index: VectorIndex,
    user: User,
    question: ProcessedQuestion,
    limit: int,
    min_score: float,
) -> Retrieval:
    """Return the chunks closest to the question that this person may read, best first.

    Returns no chunks if the best of them scores below ``min_score``.
    """
    scope = access_scope(db, user)
    hits = search_in_scope(index, scope, question, limit, now=scope.decided_at)
    if not hits:
        return Retrieval(user_id=user.id, chunks=(), dropped=0)

    # The text comes from the database, not from the index, and the document
    # is loaded with its grants so that can_read sees the current rows.
    loaded = db.scalars(
        select(Chunk)
        .where(Chunk.id.in_([hit.chunk_id for hit in hits]))
        .options(selectinload(Chunk.document).selectinload(Document.grants))
    )
    chunks = {chunk.id: chunk for chunk in loaded}

    kept: list[RetrievedChunk] = []
    for hit in hits:
        chunk = chunks.get(hit.chunk_id)
        if chunk is None or not can_read(user, chunk.document, scope.decided_at):
            continue
        kept.append(
            RetrievedChunk(
                chunk_id=chunk.id,
                document_id=chunk.document_id,
                document_title=chunk.document.title,
                document_version=chunk.document.version,
                position=chunk.position,
                text=chunk.text,
                score=hit.score,
            )
        )

    dropped = len(hits) - len(kept)
    if dropped:
        # The scope and the second check disagree, so one of them is wrong.
        # Counts only: which chunk it was would say what the person nearly saw.
        logger.error(
            "second check removed chunks the scope allowed",
            extra={"user_id": user.id, "dropped": dropped},
        )
    # Judged only on chunks the person may read: a forbidden chunk's score
    # must not decide whether they get an answer.
    best_score = kept[0].score if kept else None
    below_threshold = best_score is not None and best_score < min_score
    if below_threshold:
        kept = []

    # The best score is logged so the threshold can be checked against real
    # questions. It says how close the person's own documents came, nothing more.
    logger.info(
        "chunks retrieved",
        extra={
            "user_id": user.id,
            "chunks": len(kept),
            "dropped": dropped,
            "best_score": None if best_score is None else round(best_score, 3),
            "below_threshold": below_threshold,
        },
    )
    return Retrieval(
        user_id=user.id,
        chunks=tuple(kept),
        dropped=dropped,
        best_score=best_score,
        below_threshold=below_threshold,
    )

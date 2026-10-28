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

Then the relevance gate. If the best chunk the person may read scores below
``min_score``, nothing here is about the question and nothing is returned, so
the answer is "no information available" and the LLM is not called. The gate
looks at the best chunk only, not at each one: measured on the company data,
a per-chunk cut-off removes real answers that rank lower. With filters, it
looks at the best chunk in everything the person may read, not just in what
the filter left: a filter can remove the strong chunk and leave the one the
person asked for just under the threshold (docs/retrieval.md).

Last, if a reranker is given, it reorders the chunks that are left. It never
adds or removes one.
"""

import dataclasses
import logging
import time
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.authorization.service import can_read
from app.core.logging_setup import milliseconds_since
from app.models import Chunk, Document, User
from app.retrieval.embeddings import embedding_text
from app.retrieval.filters import SearchFilters, narrow_scope
from app.retrieval.index import Hit, VectorIndex
from app.retrieval.query import ProcessedQuestion
from app.retrieval.reranking import Reranker, checked_scores
from app.retrieval.scope import AccessScope, access_scope
from app.retrieval.search import search_in_scope

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RetrievedChunk:
    """A chunk the person may read, with what is needed to cite it."""

    chunk_id: int
    document_id: int
    # What the document is called outside the service.
    document_public_id: UUID
    document_title: str
    document_version: int
    position: int
    # Where the text sits in the document: text == content[start_offset:end_offset].
    start_offset: int
    end_offset: int
    text: str
    # Cosine similarity to the question, from -1 to 1.
    score: float
    # The reranker's score, if there was one. Its scale is the model's own.
    rerank_score: float | None = None


@dataclass(frozen=True)
class Retrieval:
    user_id: int
    chunks: tuple[RetrievedChunk, ...]
    # Hits removed by the second check. Expected to be 0 every time.
    dropped: int
    # The score the relevance gate judged: the best among the chunks the
    # person may read, before any filter. None if there were none. Kept when
    # the gate empties the result, to tune the threshold.
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
    *,
    filters: SearchFilters | None = None,
    reranker: Reranker | None = None,
    candidates: int | None = None,
) -> Retrieval:
    """Return up to ``limit`` chunks this person may read that answer the question, best first.

    Returns no chunks if the best chunk they may read scores below
    ``min_score``. ``filters`` can narrow the search within what the person
    may read. With a ``reranker``, the closest ``candidates`` chunks are
    reordered by it and the best ``limit`` of them returned.
    """
    searched = limit if reranker is None else (candidates or limit)
    if searched < limit:
        raise ValueError("candidates must be at least the limit")
    started = time.perf_counter()

    readable = access_scope(db, user)
    scope = narrow_scope(db, readable, filters)
    # No clock is passed: the search checks the scope's age against the real
    # time, so a scope kept from an earlier question is refused.
    hits = search_in_scope(index, scope, question, searched)
    kept, dropped = _load_and_check(db, user, scope, hits)

    # Judged only on chunks the person may read: a forbidden chunk's score
    # must not decide whether they get an answer.
    best_score = kept[0].score if kept else None
    if kept and scope is not readable:
        # A filter can remove the chunk that shows the question is relevant.
        # The best chunk in everything the person may read decides instead.
        top = search_in_scope(index, readable, question, 1)
        best_of_readable, dropped_here = _load_and_check(db, user, readable, top)
        dropped += dropped_here
        if best_of_readable:
            best_score = max(best_score or -1.0, best_of_readable[0].score)

    if dropped:
        # The scope and the second check disagree, so one of them is wrong.
        # Counts only: which chunk it was would say what the person nearly saw.
        logger.error(
            "second check removed chunks the scope allowed",
            extra={"user_id": user.id, "dropped": dropped},
        )

    below_threshold = best_score is not None and best_score < min_score
    # The reranker is the slowest step when there is one: a model run on
    # every candidate.
    rerank_milliseconds = None
    if below_threshold:
        kept = []
    elif reranker is not None and kept:
        reranking = time.perf_counter()
        kept = _reranked(question, kept, reranker)
        rerank_milliseconds = milliseconds_since(reranking)
    kept = kept[:limit]

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
            "reranked": reranker is not None and bool(kept),
            # The whole retrieval: scope, search, second check and reranking.
            "milliseconds": milliseconds_since(started),
            "rerank_milliseconds": rerank_milliseconds,
        },
    )
    return Retrieval(
        user_id=user.id,
        chunks=tuple(kept),
        dropped=dropped,
        best_score=best_score,
        below_threshold=below_threshold,
    )


def _load_and_check(
    db: Session, user: User, scope: AccessScope, hits: list[Hit]
) -> tuple[list[RetrievedChunk], int]:
    """Load the chunks found and keep those whose document passes ``can_read``.

    Returns them in the order of the hits, and how many were dropped.
    """
    if not hits:
        return [], 0

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
                document_public_id=chunk.document.public_id,
                document_title=chunk.document.title,
                document_version=chunk.document.version,
                position=chunk.position,
                start_offset=chunk.start_offset,
                end_offset=chunk.end_offset,
                text=chunk.text,
                score=hit.score,
            )
        )
    return kept, len(hits) - len(kept)


def _reranked(
    question: ProcessedQuestion, chunks: list[RetrievedChunk], reranker: Reranker
) -> list[RetrievedChunk]:
    """The same chunks, ordered by the reranker. Ties keep the search's order."""
    # The reranker reads the chunk the way it was embedded: title, then text.
    passages = [embedding_text(chunk.document_title, chunk.text) for chunk in chunks]

    # A cross-encoder cuts what doesn't fit without saying so, and would judge
    # a chunk by its start. Reranking is an improvement, not a requirement, so
    # the search's order is kept rather than failing the question.
    if any(reranker.count_tokens(question.text, p) > reranker.max_tokens for p in passages):
        logger.warning(
            "not reranked, a chunk and the question are too long for the reranker",
            extra={"model": reranker.name},
        )
        return chunks

    scores = checked_scores(reranker.score(question.text, passages), len(chunks))
    # sorted() is stable, so equal scores stay in the search's order.
    order = sorted(range(len(chunks)), key=lambda i: -scores[i])
    return [dataclasses.replace(chunks[i], rerank_score=scores[i]) for i in order]

"""From a person's question to the answer they get.

This puts the steps together in the only order they can run in: the
question is checked and embedded, the chunks the person may read are
retrieved, the context is built from those alone, and only then is the
model asked. There is no other way to reach the model from here, so nothing
the person may not read can be in what it was given.

Two answers come without the model:

- Nothing the person may read is relevant. The model is never called, and
  they are told there is no information available (FR-12).
- The model replies that its sources don't answer the question
  (NOT_IN_SOURCES). They are told exactly the same thing. The relevance gate
  can tell "about nothing here" from "about something here", not "about
  something here" from "answered here", so this is the second half of FR-12.

The two look the same to the person on purpose: either way, there is nothing
to show them. Which one it was is kept on the answer for the audit log and
for evaluation, never in the response.

The marker is matched anywhere in the reply, not only as the whole reply. A
model that says the sources don't contain the answer and then answers
anyway is answering from somewhere else, which is the one thing it mustn't
do.

An answer cut off at the length cap is returned, marked as cut off, rather
than passed on as if it were whole. What it says up to there came from the
sources; the person is told the rest is missing.

An answer's citations are resolved against its own context only
(``app/rag/citations.py``), so a cited source is always one the person may
read. A label the model made up is removed from the text. Before that, any
link in the answer that its sources don't contain is taken out
(``app/rag/links.py``), so an instruction planted in a document can't make
the answer carry the reader's content to someone else's server.

Every question that reaches retrieval is audited: the question, the chunks
the person was given, and what came of it (FR-13). That includes a question
the model failed on, because its sources were sent to the model all the
same. If the record can't be stored, the answer isn't given
(decision record 0006).

Before the model is called, a record of what it is about to be sent is
stored. With a hosted model, that is company text leaving the machine, and
it must not leave unrecorded: if this record can't be stored, the model is
never called. The record of the outcome follows once there is one.
"""

import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy.orm import Session

from app.audit.trail import write
from app.core.config import Settings
from app.models import AuditAction, User
from app.rag.citations import resolve_citations
from app.rag.context import Context, Source, build_context
from app.rag.links import remove_foreign_links
from app.rag.llm import LLMError, LLMProvider, StopReason, complete
from app.rag.prompt import NOT_IN_SOURCES, Prompt, build_prompt
from app.retrieval.embeddings import Embedder
from app.retrieval.filters import SearchFilters
from app.retrieval.index import VectorIndex
from app.retrieval.query import process_question
from app.retrieval.reranking import Reranker
from app.retrieval.retriever import retrieve

logger = logging.getLogger(__name__)

# What the person is told when there is no answer, whatever the reason.
NO_INFORMATION_MESSAGE = "No information available to answer this question."

# The marker as the model may write it: any case, with spaces for underscores.
_NOT_IN_SOURCES = re.compile(NOT_IN_SOURCES.replace("_", "[_ ]"), re.IGNORECASE)


class Outcome(StrEnum):
    ANSWERED = "answered"
    NO_INFORMATION = "no_information"


class NoAnswerReason(StrEnum):
    # Nothing the person may read was relevant. The model wasn't asked.
    NOTHING_RELEVANT = "nothing_relevant"
    # The model said its sources don't answer the question.
    NOT_IN_SOURCES = "not_in_sources"
    # The provider's own safety system declined to answer.
    REFUSED_BY_PROVIDER = "refused_by_provider"


@dataclass(frozen=True)
class Answer:
    user_id: int
    # The question as it was searched with.
    question: str
    outcome: Outcome
    # The model's answer with its citations as [S1], or NO_INFORMATION_MESSAGE.
    text: str
    # What the model was given. Empty when it wasn't asked. Kept for the
    # audit log even when there is no answer: the model still read it.
    context: Context
    reason: NoAnswerReason | None = None
    # The sources the answer cites, in the order it first cites them. Always
    # taken from the context.
    citations: tuple[Source, ...] = ()
    # Labels the model cited that weren't in the context, removed from the text.
    unknown_citations: int = 0
    # Links the sources don't contain, removed from the text.
    removed_links: int = 0
    # True when the answer stopped at the length cap.
    cut_off: bool = False
    model: str | None = None
    prompt_version: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None

    @property
    def answered(self) -> bool:
        return self.outcome is Outcome.ANSWERED

    @property
    def uncited(self) -> bool:
        """An answer that cites none of its sources."""
        return self.answered and not self.citations


def answer_question(
    db: Session,
    index: VectorIndex,
    embedder: Embedder,
    provider: LLMProvider,
    user: User,
    raw_question: str,
    settings: Settings,
    *,
    filters: SearchFilters | None = None,
    reranker: Reranker | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> Answer:
    """Answer the question from what this person may read, or say there is nothing to answer it.

    Raises InvalidInputError for a question that can't be searched with,
    LLMError when the model can't be reached or gives no usable reply, and
    AuditWriteError when the answer can't be audited.
    """
    started = time.perf_counter()
    question = process_question(raw_question, settings, embedder)
    retrieval = retrieve(
        db,
        index,
        user,
        question,
        settings.retrieval_top_k,
        settings.retrieval_min_score,
        filters=filters,
        reranker=reranker,
        candidates=settings.rerank_candidates,
    )
    context = build_context(retrieval, settings.context_max_characters)
    retrieved = time.perf_counter()

    if context.is_empty:
        answer = Answer(
            user_id=user.id,
            question=question.text,
            outcome=Outcome.NO_INFORMATION,
            text=NO_INFORMATION_MESSAGE,
            context=context,
            reason=NoAnswerReason.NOTHING_RELEVANT,
        )
        _log(answer, started, retrieved, None)
        _audit(db, answer, filters)
        return answer

    prompt = build_prompt(question, context)
    write(
        db,
        AuditAction.QUESTION_SENT,
        "sent",
        actor_id=user.id,
        question=question.text,
        chunk_ids=_chunks_given(context),
        details={"provider": provider.name, "filters": _filters(filters)},
    )
    try:
        answer = _generate(provider, prompt, user, question.text, context, settings, sleep)
    except LLMError as error:
        write(
            db,
            AuditAction.QUESTION_ASKED,
            "failed",
            actor_id=user.id,
            question=question.text,
            chunk_ids=_chunks_given(context),
            details={"error": type(error).__name__, "filters": _filters(filters)},
        )
        raise
    _log(answer, started, retrieved, time.perf_counter())
    _audit(db, answer, filters)
    return answer


def _generate(
    provider: LLMProvider,
    prompt: Prompt,
    user: User,
    question: str,
    context: Context,
    settings: Settings,
    sleep: Callable[[float], None],
) -> Answer:
    completion = complete(provider, prompt, settings, sleep=sleep)

    reason = None
    if completion.stop_reason is StopReason.REFUSED:
        reason = NoAnswerReason.REFUSED_BY_PROVIDER
    elif _NOT_IN_SOURCES.search(completion.text):
        reason = NoAnswerReason.NOT_IN_SOURCES

    if reason is None:
        # Links first: a citation written as a link stays a citation.
        links = remove_foreign_links(completion.text, context)
        cited = resolve_citations(links.text, context)
        if not cited.text:
            # Nothing but citations the model made up.
            raise LLMError("the model's answer was only citations")
        text, citations, unknown = cited.text, cited.sources, cited.unknown
        removed_links = links.removed
    else:
        text, citations, unknown, removed_links = NO_INFORMATION_MESSAGE, (), 0, 0

    return Answer(
        user_id=user.id,
        question=question,
        outcome=Outcome.ANSWERED if reason is None else Outcome.NO_INFORMATION,
        text=text,
        context=context,
        reason=reason,
        citations=citations,
        unknown_citations=unknown,
        removed_links=removed_links,
        cut_off=reason is None and completion.stop_reason is StopReason.MAX_TOKENS,
        model=completion.model,
        prompt_version=prompt.version,
        input_tokens=completion.input_tokens,
        output_tokens=completion.output_tokens,
    )


def _chunks_given(context: Context) -> list[int]:
    return [chunk_id for source in context.sources for chunk_id in source.chunk_ids]


def _filters(filters: SearchFilters | None) -> dict[str, object] | None:
    if filters is None:
        return None
    return {
        name: sorted(value)
        for name, value in filters.model_dump(mode="json", exclude_none=True).items()
    }


def _audit(db: Session, answer: Answer, filters: SearchFilters | None) -> None:
    # The question and the chunks belong here, not in the application log.
    write(
        db,
        AuditAction.QUESTION_ASKED,
        answer.outcome.value,
        actor_id=answer.user_id,
        question=answer.question,
        chunk_ids=_chunks_given(answer.context),
        details={
            "reason": None if answer.reason is None else answer.reason.value,
            "cited_documents": [source.document_id for source in answer.citations],
            "unknown_citations": answer.unknown_citations,
            "removed_links": answer.removed_links,
            "cut_off": answer.cut_off,
            "model": answer.model,
            "prompt_version": answer.prompt_version,
            "input_tokens": answer.input_tokens,
            "output_tokens": answer.output_tokens,
            "filters": _filters(filters),
        },
    )


def _log(answer: Answer, started: float, retrieved: float, generated: float | None) -> None:
    # Counts and timings only, never the question or the answer (NFR-9).
    logger.info(
        "question answered",
        extra={
            "user_id": answer.user_id,
            "outcome": answer.outcome.value,
            "reason": None if answer.reason is None else answer.reason.value,
            "sources": len(answer.context.sources),
            "citations": len(answer.citations),
            "unknown_citations": answer.unknown_citations,
            "removed_links": answer.removed_links,
            "cut_off": answer.cut_off,
            "retrieval_milliseconds": round((retrieved - started) * 1000),
            "generation_milliseconds": (
                None if generated is None else round((generated - retrieved) * 1000)
            ),
        },
    )

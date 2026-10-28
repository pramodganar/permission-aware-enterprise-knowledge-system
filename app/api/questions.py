"""Searching and asking (FR-8, FR-10 to FR-12).

    POST /search   {"question": ..., "filters": {...}}  ->  the passages the caller may read
    POST /ask      {"question": ..., "filters": {...}}  ->  an answer from those passages

Both are POST so the question travels in the body. A question in the URL
would end up in proxy logs, browser history and the service's own request
log.

The routes add no rules of their own. ``/search`` calls ``retrieve`` and
``/ask`` calls ``answer_question``, the single path to the model, both with
the user from the token. Nothing in the body can say who is asking.

When there is nothing to show, both say "No information available to answer
this question." and nothing else, whatever the reason: nothing the caller
may read is relevant, the model found nothing in its sources, or the
provider refused. Which one it was stays in the log and, later, the audit
log. Neither response carries the relevance score the decision was made on.

``/ask`` returns only the sources the answer cites, and every one of them
comes from the answer's own context, so a citation is always to something
the caller may read. Without a model configured (APP_LLM_PROVIDER=none),
``/ask`` is refused with 503 and ``/search`` keeps working (NFR-5). A model
that is down or too slow gives the same 503 after its retries.

Responses must not be stored: they are different for every person.

Each person may ask and search only so often (threat T24): a call over
the limit gets 429 with Retry-After, before the body is looked at. Every
call with a valid token counts, whatever it asks and however it ends, so
the count says nothing about what the person may or may not read. A
limited call is logged, not audited: it was given nothing, and a flood of
them would otherwise be a flood of records.

Both are audited with the question and the chunks the caller was given:
``/ask`` inside ``answer_question``, ``/search`` here. If the record can't be
stored, the caller gets 503 instead of the results (decision record 0006).
"""

import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from app.api.components import Loaded
from app.audit.trail import write
from app.auth.dependencies import CurrentUser, DbSession
from app.core.config import Settings, get_settings
from app.core.exceptions import AnswersSwitchedOffError, TooManyRequestsError
from app.core.rate_limit import RateLimiter
from app.models import AuditAction
from app.rag.answer import NO_INFORMATION_MESSAGE, answer_question
from app.retrieval.filters import SearchFilters
from app.retrieval.query import process_question
from app.retrieval.retriever import retrieve

router = APIRouter(tags=["questions"])
logger = logging.getLogger(__name__)

# A cap on the body before anything is done with it. The real limit,
# APP_MAX_QUESTION_CHARACTERS, is checked with the question's other rules and
# gives a clearer message.
MAX_BODY_QUESTION = 10_000


class QuestionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=MAX_BODY_QUESTION)
    filters: SearchFilters | None = None


class DocumentRef(BaseModel):
    id: UUID
    title: str
    version: int


class Passage(BaseModel):
    document: DocumentRef
    # Where the passage sits in the document's text.
    start_offset: int
    end_offset: int
    text: str
    # Cosine similarity to the question, -1 to 1.
    score: float


class SearchResponse(BaseModel):
    # Best first. Empty when nothing the caller may read is relevant.
    results: list[Passage]
    # Set when there are no results.
    message: str | None = None


class Citation(BaseModel):
    # As it appears in the answer: S1, S2, ...
    label: str
    document: DocumentRef
    text: str


class AskResponse(BaseModel):
    answered: bool
    answer: str
    # The sources the answer cites, in the order of their labels.
    citations: list[Citation]
    # The answer reached the length cap and stops part way.
    cut_off: bool
    # The answer cites none of its sources.
    uncited: bool


AppSettings = Annotated[Settings, Depends(get_settings)]


def limit_searching(request: Request, user: CurrentUser) -> None:
    _take(request.app.state.search_limiter, user.id, "/search")


def limit_asking(request: Request, user: CurrentUser) -> None:
    _take(request.app.state.ask_limiter, user.id, "/ask")


def _take(limiter: RateLimiter, user_id: int, route: str) -> None:
    try:
        limiter.take(user_id)
    except TooManyRequestsError:
        logger.warning("request limited", extra={"user_id": user_id, "route": route})
        raise


@router.post("/search", dependencies=[Depends(limit_searching)])
def search(
    body: QuestionRequest,
    user: CurrentUser,
    db: DbSession,
    components: Loaded,
    settings: AppSettings,
    response: Response,
) -> SearchResponse:
    question = process_question(body.question, settings, components.embedder)
    retrieval = retrieve(
        db,
        components.index,
        user,
        question,
        settings.retrieval_top_k,
        settings.retrieval_min_score,
        filters=body.filters,
        reranker=components.reranker,
        candidates=settings.rerank_candidates,
    )
    write(
        db,
        AuditAction.SEARCH,
        "no_information" if retrieval.is_empty else "found",
        actor_id=user.id,
        question=question.text,
        chunk_ids=[chunk.chunk_id for chunk in retrieval.chunks],
        details={
            "filters": None
            if body.filters is None
            else {
                name: sorted(value)
                for name, value in body.filters.model_dump(mode="json", exclude_none=True).items()
            }
        },
    )

    response.headers["Cache-Control"] = "no-store"
    return SearchResponse(
        results=[
            Passage(
                document=DocumentRef(
                    id=chunk.document_public_id,
                    title=chunk.document_title,
                    version=chunk.document_version,
                ),
                start_offset=chunk.start_offset,
                end_offset=chunk.end_offset,
                text=chunk.text,
                score=round(chunk.score, 3),
            )
            for chunk in retrieval.chunks
        ],
        message=NO_INFORMATION_MESSAGE if retrieval.is_empty else None,
    )


@router.post("/ask", dependencies=[Depends(limit_asking)])
def ask(
    body: QuestionRequest,
    user: CurrentUser,
    db: DbSession,
    components: Loaded,
    settings: AppSettings,
    response: Response,
) -> AskResponse:
    if components.provider is None:
        raise AnswersSwitchedOffError("no language model is configured")

    answer = answer_question(
        db,
        components.index,
        components.embedder,
        components.provider,
        user,
        body.question,
        settings,
        filters=body.filters,
        reranker=components.reranker,
    )

    response.headers["Cache-Control"] = "no-store"
    return AskResponse(
        answered=answer.answered,
        answer=answer.text,
        citations=[
            Citation(
                label=source.label,
                document=DocumentRef(
                    id=source.document_public_id,
                    title=source.document_title,
                    version=source.document_version,
                ),
                text=source.text,
            )
            for source in answer.citations
        ],
        cut_off=answer.cut_off,
        uncited=answer.uncited,
    )

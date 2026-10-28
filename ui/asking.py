"""Asking and searching from the UI: the parts that aren't drawing.

What a person may narrow a question to is offered from the documents the
service says they may read, so the choices never name anything else. The
choices only narrow: the service applies them inside the person's access,
and a stale choice, a document since withdrawn, simply matches nothing.

Nothing here is kept between interactions. An answer is drawn in the run
that asked for it and not stored, so there is nothing left for whoever uses
the browser next, and nothing stale on screen after someone's access changes.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from uuid import UUID

from ui.client import (
    Api,
    ApiError,
    DocumentSummary,
    Filters,
    InvalidInput,
    NotAllowed,
    TooManyAttempts,
    Unavailable,
)
from ui.session import UNREACHABLE

# The service's own limits on a filter (app/retrieval/filters.py). Going
# over them is refused, so the UI says so before sending.
MAX_DEPARTMENTS = 20
MAX_DOCUMENTS = 50
# The most the service takes in a body; its configured limit, usually lower,
# is checked there and comes back with the reason.
MAX_QUESTION = 10_000
# How many readable documents are fetched to offer as choices.
PAGE_SIZE = 100
MAX_PAGES = 10

NARROW = "Only look in…"
EMPTY_QUESTION = "Type a question first."
TOO_MANY_DOCUMENTS = f"Choose at most {MAX_DOCUMENTS} documents."
TOO_MANY_DEPARTMENTS = f"Choose at most {MAX_DEPARTMENTS} departments."
CUT_OFF = "This answer reached the length limit and stops part way; the rest is missing."
UNCITED = (
    "This answer cites none of its sources, so nothing here shows where it came from. "
    "Check the documents before relying on it."
)


@dataclass(frozen=True)
class Choices:
    """What a question can be narrowed to, all taken from readable documents."""

    departments: dict[str, str]  # code -> name
    classifications: tuple[str, ...]
    documents: dict[UUID, str]  # id -> how it is shown

    @property
    def empty(self) -> bool:
        return not self.documents


def readable_documents(api: Api, token: str) -> list[DocumentSummary]:
    """Every document the service says this person may read, up to a limit."""
    found: list[DocumentSummary] = []
    for page in range(MAX_PAGES):
        listed = api.documents(token, limit=PAGE_SIZE, offset=page * PAGE_SIZE)
        found += listed.documents
        if len(listed.documents) < PAGE_SIZE or len(found) >= listed.total:
            break
    return found


def choices(documents: Iterable[DocumentSummary]) -> Choices:
    documents = list(documents)
    return Choices(
        departments={
            d.department.code: d.department.name
            for d in sorted(documents, key=lambda d: d.department.name)
        },
        classifications=tuple(sorted({d.classification for d in documents})),
        documents={
            d.id: f"{d.title} (v{d.version})" for d in sorted(documents, key=lambda d: d.title)
        },
    )


def chosen(
    departments: Iterable[str], classifications: Iterable[str], documents: Iterable[UUID]
) -> Filters:
    """The filters for what was picked. Nothing picked is no filter."""
    return Filters(
        departments=tuple(sorted(departments)),
        classifications=tuple(sorted(classifications)),
        document_ids=tuple(sorted(documents)),
    )


def problem_with(question: str, filters: Filters) -> str | None:
    """What is wrong with the request before it is sent, if anything."""
    if not question.strip():
        return EMPTY_QUESTION
    if len(filters.departments) > MAX_DEPARTMENTS:
        return TOO_MANY_DEPARTMENTS
    if len(filters.document_ids) > MAX_DOCUMENTS:
        return TOO_MANY_DOCUMENTS
    return None


def explain(error: ApiError) -> str:
    """What to tell the person when a question fails.

    A 401 isn't handled here: it means signing in again, which the page does.
    """
    if isinstance(error, InvalidInput):
        # The service's own reason, such as the question being too long.
        return error.message
    if isinstance(error, TooManyAttempts):
        seconds = error.retry_after or 60
        return f"Too many requests. Try again in {seconds} seconds."
    if isinstance(error, NotAllowed):
        return "Your account may not ask questions."
    if isinstance(error, Unavailable):
        # Otherwise the reason is worth showing: answers switched off, the
        # model down, or the service taking too long.
        return UNREACHABLE if error.code == "unreachable" else error.message
    reference = f" (reference {error.request_id})" if error.request_id else ""
    return f"Something went wrong{reference}. Try again."

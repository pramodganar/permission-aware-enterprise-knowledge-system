"""The audit page: the parts that aren't drawing.

The trail holds the questions people asked and what they were given, so
only a security reviewer may read it, and the service records every read of
it, this page's included: each interaction with the page is a read.

Times are shown and chosen in UTC, as the service stores them.
"""

import json
from datetime import UTC, date, datetime, time, timedelta
from uuid import UUID

from ui import asking
from ui.client import Api, ApiError, AuditEntry, AuditPage, InvalidInput, NotAllowed

# The same as app.models.AuditAction; a test keeps them in step.
ACTIONS = (
    "question.sent",
    "question.asked",
    "search",
    "document.read",
    "document.ingested",
    "document.replaced",
    "document.deleted",
    "document.relabelled",
    "documents.rechunked",
    "grant.given",
    "grant.revoked",
    "login.succeeded",
    "login.failed",
    "action.denied",
    "audit.read",
)
PAGE_SIZE = 50
BACKWARDS = "The first day is after the last."
NOT_AN_ID = "A document ID looks like 3f0c9a52-8d1e-4b7a-9c61-2e5d8f4a7b10."


def window(first: date | None, last: date | None) -> tuple[datetime | None, datetime | None]:
    """From the start of the first day to the end of the last, in UTC."""
    since = None if first is None else datetime.combine(first, time.min, tzinfo=UTC)
    until = (
        None if last is None else datetime.combine(last + timedelta(days=1), time.min, tzinfo=UTC)
    )
    return since, until


def window_problem(first: date | None, last: date | None) -> str | None:
    if first is not None and last is not None and first > last:
        return BACKWARDS
    return None


def document_id_from(text: str) -> UUID | None:
    """The document ID typed, or None for none. Raises ValueError if it isn't one."""
    return UUID(text.strip()) if text.strip() else None


def document_id_problem(text: str) -> str | None:
    try:
        document_id_from(text)
    except ValueError:
        return NOT_AN_ID
    return None


def pages_for(total: int) -> int:
    return max(1, -(-total // PAGE_SIZE))


def fetch(
    api: Api,
    token: str,
    *,
    actor: str | None,
    action: str | None,
    document_id: UUID | None,
    since: datetime | None,
    until: datetime | None,
    page: int,
) -> tuple[AuditPage, int]:
    """One page of the trail, and which page it is.

    A page past the end, as when a filter leaves fewer records, gives the
    last page instead of an empty one.
    """

    def one(number: int) -> AuditPage:
        return api.audit(
            token,
            actor=actor,
            action=action,
            document_id=document_id,
            since=since,
            until=until,
            limit=PAGE_SIZE,
            offset=(number - 1) * PAGE_SIZE,
        )

    page = max(1, page)
    found = one(page)
    if page > pages_for(found.total):
        page = pages_for(found.total)
        if found.total:
            found = one(page)
    return found, page


def rows(records: list[AuditEntry]) -> list[dict[str, object]]:
    """The table of a page of records, newest first as the service sends them."""
    return [
        {
            "When (UTC)": record.created_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S"),
            "Who": record.actor.email if record.actor else "(no such account)",
            "Action": record.action,
            "Outcome": record.outcome,
            "Document": "" if record.document_id is None else str(record.document_id),
            "Question": record.question or "",
            "Chunks": ", ".join(str(n) for n in record.chunk_ids),
            "Details": json.dumps(record.details, sort_keys=True) if record.details else "",
            "Request": record.request_id or "",
        }
        for record in records
    ]


def explain(error: ApiError) -> str:
    if isinstance(error, NotAllowed):
        return "Your account may not read the audit trail."
    if isinstance(error, InvalidInput):
        return error.message
    return asking.explain(error)

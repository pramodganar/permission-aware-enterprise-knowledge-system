"""Writing the audit trail, and reading it back (FR-13, FR-14, SR-15, SR-16).

Two ways to write, for two kinds of work:

- ``record`` adds a record to the session without committing. Code that
  changes something calls it just before its own commit, so the change and
  its record are stored together or not at all: there is never a change
  without a record, nor a record of a change that didn't happen.
- ``write`` adds a record and commits it at once, for things that change
  nothing: a question answered, a search, a document opened, a failed login.
  They are recorded before the response goes out.

If the record can't be stored, the request fails (decision record 0006). An
answer that can't be audited isn't given: ``write`` raises
``AuditWriteError``, a 503, and the caller doesn't return what it had.

Reading needs the audit:read permission, and every read is recorded too.
"""

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.authorization.checks import ensure_permission
from app.authorization.permissions import Permission
from app.core.exceptions import ServiceUnavailableError
from app.core.logging_setup import current_request_id
from app.models import AuditAction, AuditRecord, User

logger = logging.getLogger(__name__)


class AuditWriteError(ServiceUnavailableError):
    """The audit record couldn't be stored, so the request is refused."""


def record(
    db: Session,
    action: AuditAction,
    outcome: str,
    *,
    actor_id: int | None,
    document_id: int | None = None,
    question: str | None = None,
    chunk_ids: Iterable[int] = (),
    details: dict[str, Any] | None = None,
) -> AuditRecord:
    """Add a record to the session. It is stored with the caller's next commit."""
    entry = AuditRecord(
        request_id=current_request_id(),
        actor_id=actor_id,
        action=action,
        outcome=outcome,
        document_id=document_id,
        question=question,
        chunk_ids=list(chunk_ids),
        details=details or {},
    )
    db.add(entry)
    return entry


def write(
    db: Session,
    action: AuditAction,
    outcome: str,
    *,
    actor_id: int | None,
    document_id: int | None = None,
    question: str | None = None,
    chunk_ids: Iterable[int] = (),
    details: dict[str, Any] | None = None,
) -> AuditRecord:
    """Store a record now. Raises AuditWriteError if it can't be stored."""
    entry = record(
        db,
        action,
        outcome,
        actor_id=actor_id,
        document_id=document_id,
        question=question,
        chunk_ids=chunk_ids,
        details=details,
    )
    try:
        db.commit()
    except SQLAlchemyError as error:
        db.rollback()
        # The type only: a database error message can quote the values.
        logger.error(
            "audit record not written",
            extra={"action": action.value, "error": type(error).__name__},
        )
        raise AuditWriteError(f"the audit record for {action.value} could not be stored") from None
    return entry


@dataclass(frozen=True)
class AuditQuery:
    """What a reviewer asked for. None means any."""

    actor_email: str | None = None
    action: AuditAction | None = None
    document_id: int | None = None
    since: datetime | None = None
    until: datetime | None = None
    limit: int = 100
    offset: int = 0


@dataclass(frozen=True)
class AuditPage:
    total: int
    records: Sequence[AuditRecord]
    # Each actor's email, for the records on this page.
    emails: dict[int, str]


def read_trail(db: Session, reviewer: User, query: AuditQuery) -> AuditPage:
    """Return the records that match, newest first. Needs audit:read."""
    ensure_permission(reviewer, Permission.READ_AUDIT_LOG)

    selected = select(AuditRecord)
    if query.actor_email is not None:
        actor = select(User.id).where(User.email == query.actor_email.strip().lower())
        selected = selected.where(AuditRecord.actor_id.in_(actor))
    if query.action is not None:
        selected = selected.where(AuditRecord.action == query.action)
    if query.document_id is not None:
        selected = selected.where(AuditRecord.document_id == query.document_id)
    if query.since is not None:
        selected = selected.where(AuditRecord.created_at >= query.since)
    if query.until is not None:
        selected = selected.where(AuditRecord.created_at < query.until)

    total = db.scalar(select(func.count()).select_from(selected.subquery())) or 0
    records = db.scalars(
        selected.order_by(AuditRecord.id.desc()).limit(query.limit).offset(query.offset)
    ).all()
    actor_ids = {r.actor_id for r in records if r.actor_id is not None}
    found = db.execute(select(User.id, User.email).where(User.id.in_(actor_ids)))
    emails: dict[int, str] = dict(found.all())

    # Reading the trail is recorded in it: who looked, and for what.
    write(
        db,
        AuditAction.AUDIT_READ,
        "allowed",
        actor_id=reviewer.id,
        details={
            "filters": {
                "actor_email": query.actor_email,
                "action": None if query.action is None else query.action.value,
                "document_id": query.document_id,
                "since": None if query.since is None else query.since.isoformat(),
                "until": None if query.until is None else query.until.isoformat(),
            },
            "records": len(records),
        },
    )
    return AuditPage(total=total, records=records, emails=emails)

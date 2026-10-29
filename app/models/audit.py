import hashlib
import hmac
import json
import logging
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, Connection, ForeignKey, String, Table, Text, event, insert, text
from sqlalchemy.orm import (
    Mapped,
    Mapper,
    Session,
    UOWTransaction,
    mapped_column,
    object_session,
    validates,
)

from app.core.database import AUDIT_KEY, Base, UtcDateTime, utc_now

logger = logging.getLogger(__name__)

# Where a session keeps the records it sealed, until they are committed.
_SEALED = "audit_sealed"


class AuditAction(StrEnum):
    """What was done. Each one is recorded by the code that does it."""

    # Written before the model is called, with what it is about to be sent.
    QUESTION_SENT = "question.sent"
    QUESTION_ASKED = "question.asked"
    SEARCH = "search"
    DOCUMENT_READ = "document.read"
    DOCUMENT_INGESTED = "document.ingested"
    DOCUMENT_REPLACED = "document.replaced"
    DOCUMENT_DELETED = "document.deleted"
    DOCUMENT_RELABELLED = "document.relabelled"
    DOCUMENTS_RECHUNKED = "documents.rechunked"
    GRANT_GIVEN = "grant.given"
    GRANT_REVOKED = "grant.revoked"
    LOGIN_SUCCEEDED = "login.succeeded"
    LOGIN_FAILED = "login.failed"
    SIGNED_OUT = "signed.out"
    # Refused with 403: an action the person's roles don't allow, or giving
    # themselves access.
    ACTION_DENIED = "action.denied"
    AUDIT_READ = "audit.read"


class AuditRecord(Base):
    """One thing someone did, as it happened. Never changed once written.

    This is where the content that the application log leaves out goes: the
    question as asked, and which chunks it was answered from (NFR-9). That is
    why reading it needs its own permission.

    ``actor_id`` is who did it. It is empty only for a failed login with an
    email no account has; what was typed is not kept, because people type
    passwords into the email field.
    """

    __tablename__ = "audit_records"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now, index=True)
    # Ties the record to the application log lines of the same request.
    request_id: Mapped[str | None] = mapped_column(String(64), default=None)
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), default=None, index=True)
    action: Mapped[str] = mapped_column(String(40), index=True)
    # answered, no_information, failed, allowed, denied, not_found, done
    outcome: Mapped[str] = mapped_column(String(30))
    document_id: Mapped[int | None] = mapped_column(ForeignKey("documents.id"), default=None)
    question: Mapped[str | None] = mapped_column(Text, default=None)
    # The chunks the person was given, in the order they were given.
    chunk_ids: Mapped[list[int]] = mapped_column(JSON, default=list)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    @validates("action")
    def check_action(self, _: str, value: str) -> str:
        return AuditAction(value).value

    def __repr__(self) -> str:
        return f"AuditRecord(id={self.id!r}, action={self.action!r}, outcome={self.outcome!r})"


class AuditSeal(Base):
    """A keyed hash of one audit record, written with it (decision record 0013).

    Whoever can change the database can change a record, but without the
    service's key can't make a seal that matches. The seal covers the
    record's ID, so a record moved to another ID doesn't match either, and a
    record deleted leaves a gap in the IDs. It has a table of its own because
    a record can't be updated once written, and its ID isn't known until it is.
    """

    __tablename__ = "audit_seals"

    record_id: Mapped[int] = mapped_column(ForeignKey("audit_records.id"), primary_key=True)
    # HMAC-SHA256, as 64 hex characters.
    mac: Mapped[str] = mapped_column(String(64))


class AuditRecordChangedError(Exception):
    """Something tried to change or delete an audit record or its seal."""


def seal_of(record: AuditRecord, key: bytes) -> str:
    """The seal a record should have: HMAC-SHA256 of every field, its ID included."""
    fields = {
        "id": record.id,
        "created_at": record.created_at.astimezone(UTC).isoformat(),
        "request_id": record.request_id,
        "actor_id": record.actor_id,
        "action": record.action,
        "outcome": record.outcome,
        "document_id": record.document_id,
        "question": record.question,
        "chunk_ids": list(record.chunk_ids),
        "details": record.details,
    }
    text = json.dumps(fields, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hmac.new(key, text.encode(), hashlib.sha256).hexdigest()


@event.listens_for(Session, "before_flush")
def _refuse_changes(session: Session, _: UOWTransaction, __: object) -> None:
    # The service only ever adds records. Anything else is a bug, or worse.
    kept = (AuditRecord, AuditSeal)
    changed = [r for r in session.dirty if isinstance(r, kept) and session.is_modified(r)]
    deleted = [r for r in session.deleted if isinstance(r, kept)]
    if changed or deleted:
        raise AuditRecordChangedError("audit records are never changed or deleted")


@event.listens_for(AuditRecord, "after_insert")
def _seal(_: Mapper[AuditRecord], connection: Connection, record: AuditRecord) -> None:
    # In the same transaction as the record: stored together or not at all.
    session = object_session(record)
    key = None if session is None else session.info.get(AUDIT_KEY)
    if session is None or key is None:
        return
    connection.execute(insert(AuditSeal).values(record_id=record.id, mac=seal_of(record, key)))
    session.info.setdefault(_SEALED, []).append((record.id, record.action))


@event.listens_for(Session, "after_commit")
def _log_sealed(session: Session) -> None:
    # Logged once stored, so the log never names a record that isn't there.
    # The log is kept apart from the database, so it shows the newest
    # records even if they are deleted from the trail.
    for record_id, action in session.info.pop(_SEALED, []):
        logger.info("audit record sealed", extra={"audit_id": record_id, "action": action})


@event.listens_for(Session, "after_rollback")
def _forget_sealed(session: Session) -> None:
    session.info.pop(_SEALED, None)


# The same rule in the database, for anything that doesn't go through the
# ORM. SQLite here; PostgreSQL would use a trigger function or revoke UPDATE
# and DELETE from the service's role.
@event.listens_for(AuditRecord.__table__, "after_create")
@event.listens_for(AuditSeal.__table__, "after_create")
def _add_triggers(table: Table, connection: Connection, **__: Any) -> None:
    if connection.dialect.name != "sqlite":
        return
    for operation in ("UPDATE", "DELETE"):
        connection.execute(
            text(
                f"CREATE TRIGGER {table.name}_no_{operation.lower()} "
                f"BEFORE {operation} ON {table.name} "
                "BEGIN SELECT RAISE(ABORT, 'audit records are append-only'); END"
            )
        )

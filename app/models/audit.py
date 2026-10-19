from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, Connection, ForeignKey, String, Table, Text, event, text
from sqlalchemy.orm import Mapped, Session, UOWTransaction, mapped_column, validates

from app.core.database import Base, UtcDateTime, utc_now


class AuditAction(StrEnum):
    """What was done. Each one is recorded by the code that does it."""

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
    LOGIN_FAILED = "login.failed"
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


class AuditRecordChangedError(Exception):
    """Something tried to change or delete an audit record."""


@event.listens_for(Session, "before_flush")
def _refuse_changes(session: Session, _: UOWTransaction, __: object) -> None:
    # The service only ever adds records. Anything else is a bug, or worse.
    changed = [r for r in session.dirty if isinstance(r, AuditRecord) and session.is_modified(r)]
    deleted = [r for r in session.deleted if isinstance(r, AuditRecord)]
    if changed or deleted:
        raise AuditRecordChangedError("audit records are never changed or deleted")


# The same rule in the database, for anything that doesn't go through the
# ORM. SQLite here; PostgreSQL would use a trigger function or revoke UPDATE
# and DELETE from the service's role.
@event.listens_for(AuditRecord.__table__, "after_create")
def _add_triggers(_: Table, connection: Connection, **__: Any) -> None:
    if connection.dialect.name != "sqlite":
        return
    for operation in ("UPDATE", "DELETE"):
        connection.execute(
            text(
                f"CREATE TRIGGER audit_records_no_{operation.lower()} "
                f"BEFORE {operation} ON audit_records "
                "BEGIN SELECT RAISE(ABORT, 'audit records are append-only'); END"
            )
        )

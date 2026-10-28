import logging
import uuid
from collections.abc import Iterable
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from sqlalchemy import ForeignKey, String, Text, UniqueConstraint, Uuid, event, insert, select
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship, validates

from app.core.database import Base, UtcDateTime, utc_now

if TYPE_CHECKING:
    from app.models.chunk import Chunk
    from app.models.department import Department
    from app.models.user import User

logger = logging.getLogger(__name__)


class Classification(StrEnum):
    """Who a document is for.

    GENERAL       everyone who can use the assistant
    DEPARTMENTAL  members of the owning department, and executives
    RESTRICTED    only people named in a grant, whatever their role
    """

    GENERAL = "general"
    DEPARTMENTAL = "departmental"
    RESTRICTED = "restricted"


class DocumentStatus(StrEnum):
    """Only an ACTIVE document can be read.

    SUPERSEDED  a newer version has replaced it
    DELETED     it was withdrawn
    """

    ACTIVE = "active"
    SUPERSEDED = "superseded"
    DELETED = "deleted"


class Document(Base):
    """A document: its access labels, its text and where it came from.

    Every document has an owning department and a classification, and neither
    has a default. A document that arrives without them can't be stored, so
    nothing becomes readable by accident.

    The file details and the title are part of the document. They are shown
    only to people who may read it, like the text.

    ``id`` is numbered in the order documents are stored and never leaves the
    service: everyone outside it knows a document by its ``public_id``.
    """

    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(255))
    department_id: Mapped[int] = mapped_column(ForeignKey("departments.id"), index=True)
    classification: Mapped[str] = mapped_column(String(20), index=True)
    # The extracted text. Chunks are cut from this, so a document can be
    # re-chunked without going back to the original file.
    content: Mapped[str] = mapped_column(Text, default="")

    # Where the document came from. Recorded once at ingestion.
    source_filename: Mapped[str] = mapped_column(String(255))
    file_type: Mapped[str] = mapped_column(String(10))
    size_bytes: Mapped[int] = mapped_column()
    # SHA-256 of the uploaded file, as 64 hex characters.
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    uploaded_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    version: Mapped[int] = mapped_column(default=1)
    status: Mapped[str] = mapped_column(String(20), default=DocumentStatus.ACTIVE, index=True)
    # The version this one replaced, if any.
    supersedes_id: Mapped[int | None] = mapped_column(ForeignKey("documents.id"), default=None)

    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now, onupdate=utc_now)

    department: Mapped["Department"] = relationship()
    uploaded_by: Mapped["User"] = relationship()
    grants: Mapped[list["DocumentGrant"]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )
    chunks: Mapped[list["Chunk"]] = relationship(
        back_populates="document", cascade="all, delete-orphan", order_by="Chunk.position"
    )
    # Loaded with the document, since every response that names it needs it.
    public: Mapped["DocumentPublicId"] = relationship(lazy="joined", cascade="all, delete-orphan")

    @property
    def public_id(self) -> uuid.UUID:
        return self.public.public_id

    @validates("classification")
    def check_classification(self, _: str, value: str) -> str:
        return Classification(value.strip().lower()).value

    @validates("status")
    def check_status(self, _: str, value: str) -> str:
        return DocumentStatus(value).value

    def __repr__(self) -> str:
        return f"Document(id={self.id!r}, classification={self.classification!r})"


class DocumentGrant(Base):
    """Access to one document for one named user, optionally until a date.

    A grant only ever adds access. It is how a restricted document gets its
    readers, and how someone outside a department is let in for a project.
    """

    __tablename__ = "document_grants"
    __table_args__ = (UniqueConstraint("document_id", "user_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    granted_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now)
    expires_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)

    document: Mapped[Document] = relationship(back_populates="grants")

    def is_valid_at(self, moment: datetime) -> bool:
        return self.expires_at is None or moment < self.expires_at

    def __repr__(self) -> str:
        return f"DocumentGrant(document_id={self.document_id!r}, user_id={self.user_id!r})"


class DocumentPublicId(Base):
    """The ID a document is known by outside the service (threat T36).

    A document's own ID is numbered in the order documents are stored, so a
    reader shown IDs 3 and 7 would know three documents were stored between
    them. This one is random, so it says nothing about any other document.

    It has a table of its own so that a database made before it existed gains
    it without a table being changed: the service creates missing tables and
    never alters one, so the audit seals arrived the same way.
    """

    __tablename__ = "document_public_ids"

    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"), primary_key=True)
    public_id: Mapped[uuid.UUID] = mapped_column(Uuid, unique=True)

    def __repr__(self) -> str:
        return f"DocumentPublicId(document_id={self.document_id!r})"


def new_public_id() -> uuid.UUID:
    # 122 random bits from the operating system: nothing about the document,
    # the time or any other document goes into it.
    return uuid.uuid4()


@event.listens_for(Document, "init")
def _give_public_id(document: Document, _: Any, __: Any) -> None:
    # Every document gets one when it is made, before anything can show it.
    document.public = DocumentPublicId(public_id=new_public_id())


def give_public_ids(db: Session) -> int:
    """Give each document stored before public IDs existed one of its own. Returns how many."""
    missing = db.scalars(
        select(Document.id)
        .outerjoin(DocumentPublicId, DocumentPublicId.document_id == Document.id)
        .where(DocumentPublicId.document_id.is_(None))
    ).all()
    if missing:
        db.execute(
            insert(DocumentPublicId),
            [{"document_id": number, "public_id": new_public_id()} for number in missing],
        )
        db.commit()
        logger.info("public document IDs given", extra={"documents": len(missing)})
    return len(missing)


def document_number(db: Session, public_id: uuid.UUID) -> int | None:
    """The document's own ID, or None if no document has this public ID."""
    return db.scalar(
        select(DocumentPublicId.document_id).where(DocumentPublicId.public_id == public_id)
    )


def public_ids(db: Session, numbers: Iterable[int | None]) -> dict[int, uuid.UUID]:
    """The public ID of each of these documents, by their own ID."""
    wanted = {number for number in numbers if number is not None}
    if not wanted:
        return {}
    rows = db.execute(
        select(DocumentPublicId.document_id, DocumentPublicId.public_id).where(
            DocumentPublicId.document_id.in_(wanted)
        )
    )
    return dict(rows.all())

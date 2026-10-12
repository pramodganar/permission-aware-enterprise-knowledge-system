from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates

from app.core.database import Base, UtcDateTime, utc_now

if TYPE_CHECKING:
    from app.models.chunk import Chunk
    from app.models.department import Department
    from app.models.user import User


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

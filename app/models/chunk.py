from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, ForeignKey, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base, UtcDateTime, utc_now

if TYPE_CHECKING:
    from app.models.document import Document


class Chunk(Base):
    """A piece of one document version: the unit that is embedded, searched and cited.

    A chunk has no department, classification or grants. Who may read it is
    decided on its document, every time, so a chunk can never be more open
    than the document it came from and a permission change needs no update
    here.

    ``text`` is always ``document.content[start_offset:end_offset]``.
    """

    __tablename__ = "chunks"
    __table_args__ = (
        UniqueConstraint("document_id", "position"),
        CheckConstraint("position >= 0", name="position_not_negative"),
        CheckConstraint("start_offset >= 0", name="start_not_negative"),
        CheckConstraint("end_offset > start_offset", name="end_after_start"),
        # A chunk ID is the key of its vector in the index. If SQLite reused the
        # ID of a deleted chunk, an old vector would point at new text.
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"), index=True)
    # 0, 1, 2 ... in reading order within the document.
    position: Mapped[int] = mapped_column()
    start_offset: Mapped[int] = mapped_column()
    end_offset: Mapped[int] = mapped_column()
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now)

    document: Mapped["Document"] = relationship(back_populates="chunks")

    def __repr__(self) -> str:
        return (
            f"Chunk(id={self.id!r}, document_id={self.document_id!r}, position={self.position!r})"
        )

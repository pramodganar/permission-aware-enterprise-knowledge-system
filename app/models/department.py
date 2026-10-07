from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, String
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates

from app.core.database import Base, utc_now

if TYPE_CHECKING:
    from app.models.user import User


class Department(Base):
    """A department of the company. Every user belongs to exactly one.

    ``code`` is the stable identifier used in seed data, document metadata and
    tests, for example "engineering". ``name`` is what people see.
    """

    __tablename__ = "departments"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(50), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    # passive_deletes="all": leave the users alone when a department is deleted,
    # so the foreign key rejects deleting a department that still has members.
    users: Mapped[list["User"]] = relationship(back_populates="department", passive_deletes="all")

    @validates("code")
    def normalise_code(self, _: str, value: str) -> str:
        return value.strip().lower()

    def __repr__(self) -> str:
        return f"Department(id={self.id!r}, code={self.code!r})"

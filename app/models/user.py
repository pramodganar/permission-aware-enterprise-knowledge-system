from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column, validates

from app.core.database import Base


def utc_now() -> datetime:
    return datetime.now(UTC)


class User(Base):
    """An employee who can log in.

    ``is_active`` is checked on every request, so setting it to False cuts off
    access immediately, even for a token that hasn't expired.
    """

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(255))
    hashed_password: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )

    @validates("email")
    def normalise_email(self, _: str, value: str) -> str:
        # Stored lowercase so Alice@acme.com and alice@acme.com are one account.
        return value.strip().lower()

    def __repr__(self) -> str:
        return f"User(id={self.id!r}, email={self.email!r}, is_active={self.is_active!r})"

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates

from app.core.database import Base, UtcDateTime, utc_now
from app.models.role import Role, RoleCode, user_roles

if TYPE_CHECKING:
    from app.models.department import Department


class User(Base):
    """An employee who can log in.

    ``is_active`` is checked on every request, so setting it to False cuts off
    access immediately, even for a token that hasn't expired. So is the token
    version: signing out moves it on, and every token issued before stops
    working.

    A user starts with no roles, and a user with no roles can read nothing.
    """

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(255))
    hashed_password: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    department_id: Mapped[int] = mapped_column(ForeignKey("departments.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utc_now, onupdate=utc_now)

    department: Mapped["Department"] = relationship(back_populates="users")
    roles: Mapped[list[Role]] = relationship(secondary=user_roles)
    # Loaded with the user, since every request that checks a token needs it.
    signed_out: Mapped["TokenVersion | None"] = relationship(
        lazy="joined", cascade="all, delete-orphan"
    )

    @property
    def token_version(self) -> int:
        """Which tokens are still good: those issued at this version. 0 until the first sign-out."""
        return 0 if self.signed_out is None else self.signed_out.version

    def has_role(self, code: RoleCode) -> bool:
        return any(role.code == code for role in self.roles)

    @validates("email")
    def normalise_email(self, _: str, value: str) -> str:
        # Stored lowercase so Alice@acme.com and alice@acme.com are one account.
        return value.strip().lower()

    def __repr__(self) -> str:
        return f"User(id={self.id!r}, email={self.email!r}, is_active={self.is_active!r})"


class TokenVersion(Base):
    """How many times a user has signed out (decision record 0015).

    Every token carries the version that was current when it was issued, and
    is refused once that is no longer the user's. Signing out adds one, so
    every token the user holds stops working at once, on every device.

    A table of its own, so that a database made before it existed gains it
    without a table being changed, like the public document IDs.
    """

    __tablename__ = "token_versions"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    version: Mapped[int] = mapped_column()

    def __repr__(self) -> str:
        return f"TokenVersion(user_id={self.user_id!r}, version={self.version!r})"

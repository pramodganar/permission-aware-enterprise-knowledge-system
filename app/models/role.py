from enum import StrEnum

from sqlalchemy import Column, ForeignKey, String, Table
from sqlalchemy.orm import Mapped, mapped_column, validates

from app.core.database import Base


class RoleCode(StrEnum):
    """The roles the code knows about. A role not listed here can't be created."""

    # Reads general documents and those of the user's own department.
    EMPLOYEE = "employee"
    # Reads documents of every department.
    EXECUTIVE = "executive"
    # Ingests documents and manages their permissions. Grants no read access.
    KNOWLEDGE_ADMIN = "knowledge_admin"
    # Reads the audit log. Grants no read access to documents.
    SECURITY_REVIEWER = "security_reviewer"


user_roles = Table(
    "user_roles",
    Base.metadata,
    Column("user_id", ForeignKey("users.id"), primary_key=True),
    Column("role_id", ForeignKey("roles.id"), primary_key=True),
)


class Role(Base):
    __tablename__ = "roles"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(50), unique=True, index=True)
    description: Mapped[str] = mapped_column(String(255), default="")

    @validates("code")
    def check_code(self, _: str, value: str) -> str:
        # Raises ValueError for anything that isn't a RoleCode, so a typo can't
        # create a role that nothing in the code recognises.
        return RoleCode(value.strip().lower()).value

    def __repr__(self) -> str:
        return f"Role(id={self.id!r}, code={self.code!r})"

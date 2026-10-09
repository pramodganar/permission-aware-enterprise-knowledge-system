"""Database models. Importing this package registers every table on Base.metadata."""

from app.models.department import Department
from app.models.document import Classification, Document, DocumentGrant, DocumentStatus
from app.models.role import Role, RoleCode
from app.models.user import User

__all__ = [
    "Classification",
    "Department",
    "Document",
    "DocumentGrant",
    "DocumentStatus",
    "Role",
    "RoleCode",
    "User",
]

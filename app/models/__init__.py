"""Database models. Importing this package registers every table on Base.metadata."""

from app.models.audit import (
    AuditAction,
    AuditRecord,
    AuditRecordChangedError,
    AuditSeal,
    seal_of,
)
from app.models.chunk import Chunk
from app.models.department import Department
from app.models.document import Classification, Document, DocumentGrant, DocumentStatus
from app.models.role import Role, RoleCode
from app.models.user import User

__all__ = [
    "AuditAction",
    "AuditRecord",
    "AuditRecordChangedError",
    "AuditSeal",
    "Chunk",
    "Classification",
    "Department",
    "Document",
    "DocumentGrant",
    "DocumentStatus",
    "Role",
    "RoleCode",
    "User",
    "seal_of",
]

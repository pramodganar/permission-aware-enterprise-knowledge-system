"""Setting and changing who may read a document.

Everything here needs the manage-documents permission. That permission lets
someone decide a document's audience without being able to read the document,
so these functions look documents up directly and not through the caller's
own reading scope.

One limit is built in: nobody can grant access to themselves. Someone who is
both an admin and a reader would otherwise be able to open any restricted
document on their own.
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.authorization.checks import ensure_permission
from app.authorization.permissions import Permission, permissions_for
from app.core.database import utc_now
from app.core.exceptions import InvalidInputError, NotFoundError, PermissionDeniedError
from app.models import (
    Classification,
    Department,
    Document,
    DocumentGrant,
    DocumentStatus,
    User,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GrantRequest:
    """A request to let one user, named by email, read a document."""

    email: str
    expires_at: datetime | None = None


def parse_classification(value: str) -> Classification:
    # No fallback: a missing or unknown classification is an error.
    try:
        return Classification(value.strip().lower())
    except ValueError:
        allowed = ", ".join(level.value for level in Classification)
        raise InvalidInputError(f"classification must be one of: {allowed}") from None


def find_department(db: Session, code: str) -> Department:
    department = db.scalars(
        select(Department).where(Department.code == code.strip().lower())
    ).one_or_none()
    if department is None:
        raise InvalidInputError("unknown department")
    return department


def resolve_grants(
    db: Session, admin: User, requests: Sequence[GrantRequest], now: datetime | None = None
) -> list[tuple[User, datetime | None]]:
    """Check a list of grant requests and return the users they name.

    Writes nothing. Raises if any request can't be honoured, so a caller can
    validate every grant before storing anything.
    """
    moment = now or utc_now()
    resolved: list[tuple[User, datetime | None]] = []
    seen: set[int] = set()

    for request in requests:
        grantee = _find_grantee(db, request.email)
        if grantee.id == admin.id:
            raise PermissionDeniedError(
                f"user {admin.id} tried to grant access to themselves", user_id=admin.id
            )
        if grantee.id in seen:
            raise InvalidInputError(f"{grantee.email} is listed more than once")
        if request.expires_at is not None:
            if request.expires_at.tzinfo is None:
                raise InvalidInputError("the expiry time must include a timezone")
            if request.expires_at <= moment:
                raise InvalidInputError("the expiry time must be in the future")
        seen.add(grantee.id)
        resolved.append((grantee, request.expires_at))

    return resolved


def grant_access(
    db: Session, admin: User, document_id: int, request: GrantRequest, now: datetime | None = None
) -> DocumentGrant:
    """Let a user read a document. Granting again replaces the expiry."""
    ensure_permission(admin, Permission.MANAGE_DOCUMENTS)
    document = find_current_document(db, document_id)
    ((grantee, expires_at),) = resolve_grants(db, admin, [request], now)

    grant = next((grant for grant in document.grants if grant.user_id == grantee.id), None)
    if grant is None:
        grant = DocumentGrant(user_id=grantee.id)
        document.grants.append(grant)
    grant.granted_by_id = admin.id
    grant.expires_at = expires_at
    db.commit()

    logger.info(
        "document access granted",
        extra={
            "document_id": document.id,
            "user_id": admin.id,
            "grantee_id": grantee.id,
            "expires": expires_at is not None,
        },
    )
    return grant


def revoke_access(db: Session, admin: User, document_id: int, email: str) -> None:
    """Remove a user's grant for a document."""
    ensure_permission(admin, Permission.MANAGE_DOCUMENTS)
    document = find_current_document(db, document_id)
    grantee = db.scalars(select(User).where(User.email == email.strip().lower())).one_or_none()

    grant = next(
        (grant for grant in document.grants if grantee and grant.user_id == grantee.id), None
    )
    if grant is None:
        raise NotFoundError(f"no grant on document {document_id} for that user")

    document.grants.remove(grant)
    db.commit()
    logger.info(
        "document access revoked",
        extra={"document_id": document.id, "user_id": admin.id, "grantee_id": grant.user_id},
    )


def change_labels(
    db: Session,
    admin: User,
    document_id: int,
    *,
    classification: str | None = None,
    department_code: str | None = None,
) -> Document:
    """Change a document's classification, its owning department, or both.

    Takes effect on the next request. Nothing is re-indexed.
    """
    ensure_permission(admin, Permission.MANAGE_DOCUMENTS)
    if classification is None and department_code is None:
        raise InvalidInputError("nothing to change")

    document = find_current_document(db, document_id)
    new_classification = parse_classification(classification) if classification else None
    new_department = find_department(db, department_code) if department_code else None
    before = {"classification": document.classification, "department": document.department.code}

    if new_classification is not None:
        document.classification = new_classification
    if new_department is not None:
        document.department = new_department
    db.commit()

    logger.info(
        "document labels changed",
        extra={
            "document_id": document.id,
            "user_id": admin.id,
            "old_classification": before["classification"],
            "new_classification": document.classification,
            "old_department": before["department"],
            "new_department": document.department.code,
        },
    )
    return document


def find_current_document(db: Session, document_id: int) -> Document:
    """Return a document that can still be managed: it exists and is the current version."""
    document = db.get(Document, document_id)
    if document is None:
        raise NotFoundError(f"document {document_id} does not exist")
    if document.status != DocumentStatus.ACTIVE:
        raise InvalidInputError("the document has been replaced or deleted")
    return document


def _find_grantee(db: Session, email: str) -> User:
    """Return the user to grant to, who must be active and able to read documents."""
    address = email.strip().lower()
    user = db.scalars(select(User).where(User.email == address)).one_or_none()
    if user is None or not user.is_active:
        raise InvalidInputError(f"no active user with the email {address}")
    if Permission.READ_GRANTED_DOCUMENTS not in permissions_for(user):
        # A grant would be stored but never honoured, which only misleads.
        raise InvalidInputError(f"{address} does not have a role that can read documents")
    return user

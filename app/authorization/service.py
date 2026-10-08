"""Who can read which document.

This module is the only place that decides read access. It gives the same
answer in two forms:

- ``can_read`` answers for one document that is already loaded.
- ``readable_documents`` is a query for every document a user may read. It is
  what retrieval and the document list filter by, so unauthorized documents
  are never fetched in the first place.

The rules are the table in docs/authorization.md. Both functions start from
"no" and only add access, so anything the rules don't mention is denied.
"""

from datetime import datetime

from sqlalchemy import Select, and_, false, or_, select

from app.authorization.permissions import Permission, permissions_for
from app.core.database import utc_now
from app.models import Classification, Document, DocumentGrant, User

KNOWN_CLASSIFICATIONS = frozenset(classification.value for classification in Classification)


def can_read(user: User, document: Document, now: datetime | None = None) -> bool:
    """Return True if this user may read this document."""
    permissions = permissions_for(user)
    moment = now or utc_now()

    # A document whose label can't be trusted is readable by nobody, grant or not.
    if document.classification not in KNOWN_CLASSIFICATIONS:
        return False

    if Permission.READ_GRANTED_DOCUMENTS in permissions and any(
        grant.user_id == user.id and grant.is_valid_at(moment) for grant in document.grants
    ):
        return True

    if document.classification == Classification.GENERAL:
        return Permission.READ_GENERAL_DOCUMENTS in permissions

    if document.classification == Classification.DEPARTMENTAL:
        if Permission.READ_ALL_DEPARTMENT_DOCUMENTS in permissions:
            return True
        return (
            Permission.READ_OWN_DEPARTMENT_DOCUMENTS in permissions
            and document.department_id == user.department_id
        )

    # Restricted, and no grant.
    return False


def readable_documents(user: User, now: datetime | None = None) -> Select[Document]:
    """Return a query for every document this user may read."""
    permissions = permissions_for(user)
    moment = now or utc_now()
    allowed = []

    if Permission.READ_GENERAL_DOCUMENTS in permissions:
        allowed.append(Document.classification == Classification.GENERAL)

    if Permission.READ_ALL_DEPARTMENT_DOCUMENTS in permissions:
        allowed.append(Document.classification == Classification.DEPARTMENTAL)
    elif Permission.READ_OWN_DEPARTMENT_DOCUMENTS in permissions:
        allowed.append(
            and_(
                Document.classification == Classification.DEPARTMENTAL,
                Document.department_id == user.department_id,
            )
        )

    if Permission.READ_GRANTED_DOCUMENTS in permissions:
        granted = select(DocumentGrant.document_id).where(
            DocumentGrant.user_id == user.id,
            or_(DocumentGrant.expires_at.is_(None), DocumentGrant.expires_at > moment),
        )
        allowed.append(Document.id.in_(granted))

    if not allowed:
        return select(Document).where(false())
    return select(Document).where(Document.classification.in_(KNOWN_CLASSIFICATIONS), or_(*allowed))

"""Enforcement helpers built on the authorization service.

Two kinds of check, with deliberately different failures:

- An action the user isn't allowed to perform raises PermissionDeniedError
  (403). That an ingest endpoint exists is no secret.
- A document the user isn't allowed to read raises NotFoundError (404), the
  same as a document that doesn't exist, so the response can't be used to find
  out which documents exist.
"""

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.authorization.permissions import Permission, permissions_for
from app.authorization.service import readable_documents
from app.core.exceptions import NotFoundError, PermissionDeniedError
from app.models import Document, User

logger = logging.getLogger(__name__)


def ensure_permission(user: User, permission: Permission) -> None:
    """Raise PermissionDeniedError unless the user holds this permission."""
    if permission not in permissions_for(user):
        logger.warning("action denied", extra={"user_id": user.id, "permission": permission.value})
        raise PermissionDeniedError(
            f"user {user.id} lacks {permission.value}", user_id=user.id, permission=permission
        )


def get_readable_document(db: Session, user: User, document_id: int) -> Document:
    """Return the document if the user may read it, otherwise raise NotFoundError.

    The document is fetched through the user's scope query, so one they can't
    read is never loaded. The second lookup only tells the log and the audit
    trail whether this was a miss or a denied attempt. The caller gets the
    same error either way.

    Both ways do the same work, so the time taken doesn't tell them apart
    either: the second lookup reads the ID alone, never the document, and
    there is one log line whichever it was. Loading the document to see that
    it exists made "not found" slower for a hidden document the bigger it
    was: 3 ms at the median for one of 2 MB.
    """
    document = db.scalars(readable_documents(user).where(Document.id == document_id)).one_or_none()
    if document is not None:
        return document

    exists = db.scalar(select(Document.id).where(Document.id == document_id)) is not None
    logger.warning(
        "document not returned",
        extra={"user_id": user.id, "document_id": document_id, "exists": exists},
    )
    raise NotFoundError(
        f"document {document_id} {'not readable by' if exists else 'does not exist, asked by'} "
        f"user {user.id}",
        user_id=user.id,
        document_id=document_id,
        # For the audit trail, never for the response.
        exists=exists,
    )

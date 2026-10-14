"""What one person may search, worked out once per question.

The scope is the list of chunk IDs the search is allowed to look at. It comes
from the authorization service, from who the person is, and from nothing in
the question. It is worked out fresh for every question and never kept
between requests, so a role change, a revoked grant or a deactivated account
applies to the very next question.

Everything is decided at one moment. A grant that expires while a question is
being answered is either in the scope for the whole question or not at all.

A person who may read nothing gets an empty scope, not an error. The answer is
then the same "no information available" that any question with no match
gets (decision record 0005).
"""

import logging
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from app.authorization.permissions import Permission, permissions_for
from app.authorization.service import readable_chunks
from app.core.database import utc_now
from app.models import Chunk, User

logger = logging.getLogger(__name__)

READ_PERMISSIONS = frozenset(
    {
        Permission.READ_GENERAL_DOCUMENTS,
        Permission.READ_OWN_DEPARTMENT_DOCUMENTS,
        Permission.READ_ALL_DEPARTMENT_DOCUMENTS,
        Permission.READ_GRANTED_DOCUMENTS,
    }
)


@dataclass(frozen=True)
class AccessScope:
    """The chunks and documents one person may search, at one moment.

    Frozen, and the sets are frozensets, so nothing later in the request can
    add to it.
    """

    user_id: int
    decided_at: datetime
    permissions: frozenset[Permission]
    chunk_ids: frozenset[int]
    document_ids: frozenset[int]

    @property
    def is_empty(self) -> bool:
        return not self.chunk_ids

    def allows_chunk(self, chunk_id: int) -> bool:
        return chunk_id in self.chunk_ids


def access_scope(db: Session, user: User, now: datetime | None = None) -> AccessScope:
    """Work out what this person may search right now."""
    moment = now or utc_now()
    permissions = permissions_for(user)

    if permissions & READ_PERMISSIONS:
        query = readable_chunks(user, moment).with_only_columns(Chunk.id, Chunk.document_id)
        rows = db.execute(query).all()
    else:
        # No reading role: nothing to look up.
        rows = []

    scope = AccessScope(
        user_id=user.id,
        decided_at=moment,
        permissions=permissions,
        chunk_ids=frozenset(row[0] for row in rows),
        document_ids=frozenset(row[1] for row in rows),
    )
    # Counts only. Which documents a person can see is itself sensitive.
    logger.info(
        "access scope decided",
        extra={
            "user_id": user.id,
            "documents": len(scope.document_ids),
            "chunks": len(scope.chunk_ids),
        },
    )
    return scope

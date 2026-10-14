"""Searching the index inside one person's access scope.

This is where a question meets the index, and the allowed list it searches
with always comes from an ``AccessScope``. The function takes the scope, not a
list of IDs, so the list can't be put together anywhere else: it is whatever
``access_scope`` decided for this person at this moment.

Three checks around the search itself:

- An empty scope finds nothing, and the index is not asked.
- A scope that is too old, or dated in the future, is refused. A scope belongs
  to one question. One that is minutes old has been kept somewhere, and a
  grant revoked since then would still be in it.
- Every hit is checked against the scope again before it is returned. The
  index already checks its own results; this is the same rule held by the
  code that owns the scope, so a fault in either one is caught.
"""

import logging
from datetime import datetime, timedelta

from app.core.database import utc_now
from app.retrieval.index import Hit, VectorIndex
from app.retrieval.query import ProcessedQuestion
from app.retrieval.scope import AccessScope

logger = logging.getLogger(__name__)

# Far longer than working out a scope and searching takes, far shorter than any
# cache that would be worth having.
MAX_SCOPE_AGE = timedelta(seconds=60)


class ScopeError(Exception):
    """The scope can't be used for this search, or the search went outside it."""


def search_in_scope(
    index: VectorIndex,
    scope: AccessScope,
    question: ProcessedQuestion,
    limit: int,
    now: datetime | None = None,
) -> list[Hit]:
    """Return the chunks closest to the question among those the scope allows, best first."""
    age = (now or utc_now()) - scope.decided_at
    if age < timedelta(0):
        raise ScopeError("the access scope is dated in the future")
    if age > MAX_SCOPE_AGE:
        raise ScopeError("the access scope is too old to search with")

    if scope.is_empty:
        logger.info("search skipped, empty scope", extra={"user_id": scope.user_id})
        return []

    # Chunks the person may read but that aren't indexed yet: a document added
    # since the index was built. They can't be found until it is rebuilt.
    searchable = scope.chunk_ids & index.chunk_ids
    hits = index.search(question.vector, limit, searchable)

    outside = [hit.chunk_id for hit in hits if not scope.allows_chunk(hit.chunk_id)]
    if outside:
        logger.error("search returned chunks outside the scope", extra={"user_id": scope.user_id})
        raise ScopeError("the search returned a chunk outside the access scope")

    # Counts only, as with the scope itself.
    logger.info(
        "search done",
        extra={
            "user_id": scope.user_id,
            "in_scope": len(scope.chunk_ids),
            "not_indexed": len(scope.chunk_ids) - len(searchable),
            "hits": len(hits),
        },
    )
    return hits

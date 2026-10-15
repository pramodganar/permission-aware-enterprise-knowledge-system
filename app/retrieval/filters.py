"""Narrowing a question to some departments, classifications or documents.

A filter is something the person asks for ("only HR documents"), so it is
untrusted input, and it may only ever remove chunks from their access scope.
``narrow_scope`` returns the scope intersected with the chunks that match:
whatever is in the filter, nothing outside the scope can come back.

A filter that names something the person may not read is not an error. It
matches nothing, and the answer is the same "no information available" as for
a department or document that doesn't exist. Otherwise filtering by document
ID would be a way to find out which IDs exist.

Within one field the values are alternatives (finance or legal); across
fields all must hold (finance, and general).
"""

import dataclasses
import logging
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Chunk, Classification, Department, Document
from app.retrieval.scope import AccessScope

logger = logging.getLogger(__name__)

MAX_DEPARTMENTS = 20
MAX_DOCUMENTS = 50

DepartmentCode = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_-]{0,49}$")]
DocumentId = Annotated[int, Field(ge=1)]


class SearchFilters(BaseModel):
    """What the person asked to narrow the search to. None means no filter."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    # An empty set is refused: "no departments" could mean no filter or
    # nothing at all, and guessing wrong either way is a bug.
    departments: frozenset[DepartmentCode] | None = Field(
        default=None, min_length=1, max_length=MAX_DEPARTMENTS
    )
    classifications: frozenset[Classification] | None = Field(default=None, min_length=1)
    document_ids: frozenset[DocumentId] | None = Field(
        default=None, min_length=1, max_length=MAX_DOCUMENTS
    )

    @field_validator("departments", mode="before")
    @classmethod
    def normalise_codes(cls, value: object) -> object:
        # The same normalisation as Department.code.
        if isinstance(value, list | set | frozenset | tuple):
            return [item.strip().lower() if isinstance(item, str) else item for item in value]
        return value

    @property
    def kinds(self) -> list[str]:
        """The names of the filters that are set, for logging."""
        return [name for name in type(self).model_fields if getattr(self, name) is not None]


def narrow_scope(db: Session, scope: AccessScope, filters: SearchFilters | None) -> AccessScope:
    """Return the part of the scope that matches the filters."""
    if filters is None or not filters.kinds or scope.is_empty:
        return scope

    # Only documents already in the scope are looked at.
    query = (
        select(Chunk.id, Chunk.document_id)
        .join(Document, Chunk.document_id == Document.id)
        .where(Document.id.in_(scope.document_ids))
    )
    if filters.departments is not None:
        query = query.join(Department, Document.department_id == Department.id).where(
            Department.code.in_(filters.departments)
        )
    if filters.classifications is not None:
        query = query.where(Document.classification.in_(filters.classifications))
    if filters.document_ids is not None:
        query = query.where(Document.id.in_(filters.document_ids))

    rows = db.execute(query).all()
    # The intersection is what guarantees a filter can't widen the scope,
    # whatever the query above returns.
    chunk_ids = scope.chunk_ids.intersection(row[0] for row in rows)
    document_ids = frozenset(row[1] for row in rows if row[0] in chunk_ids)

    # Which filters, not their values: "document 16" or "hr" says what the
    # person was looking for.
    logger.info(
        "scope narrowed",
        extra={
            "user_id": scope.user_id,
            "filters": filters.kinds,
            "chunks_before": len(scope.chunk_ids),
            "chunks_after": len(chunk_ids),
        },
    )
    return dataclasses.replace(scope, chunk_ids=chunk_ids, document_ids=document_ids)

"""The documents a user may read (FR-5).

    GET /documents        the documents the caller may read, by title
    GET /documents/{id}   one of them, with its text

Both go through the authorization service and nothing else. The list is the
``readable_documents`` query, the same one retrieval filters by, so the list
and search can't disagree about what someone may read. The lookup goes
through ``get_readable_document``: a document the caller may not read gets
exactly the 404 of one that doesn't exist, so trying IDs one by one says
nothing about which documents there are.

Filters (department, classification) are added to that query, so they can
only remove documents from it. A filter for something the caller can't read
gives an empty list, the same as a filter that matches nothing.

What is never shown, to anyone: who else holds a grant, who uploaded the
file, and which earlier version a document replaced. A reader needs none of
it, and each says something about people or documents outside their own
access.

Responses say they must not be stored, because they differ per person. A
cache shared between people would otherwise hand one person's list to another.

Opening a document is audited, and so is every attempt that fails: the audit
trail says whether it was a document the caller may not read or one that
doesn't exist, which the response never does. Lists are not audited.
"""

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Path, Query, Response
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import defer, joinedload

from app.audit.trail import write
from app.auth.dependencies import CurrentUser, DbSession
from app.authorization.checks import get_readable_document
from app.authorization.service import readable_documents
from app.core.database import MAX_ID
from app.core.exceptions import NotFoundError
from app.models import AuditAction, Classification, Department, Document

router = APIRouter(prefix="/documents", tags=["documents"])

MAX_PAGE = 100
MAX_OFFSET = 1_000_000


class DepartmentOut(BaseModel):
    code: str
    name: str


class DocumentSummary(BaseModel):
    id: int
    title: str
    department: DepartmentOut
    classification: str
    file_type: str
    version: int
    updated_at: datetime


class DocumentList(BaseModel):
    # How many match, across every page.
    total: int
    documents: list[DocumentSummary]


class DocumentDetail(DocumentSummary):
    source_filename: str
    size_bytes: int
    created_at: datetime
    content: str


def _summary_fields(document: Document) -> dict[str, object]:
    return {
        "id": document.id,
        "title": document.title,
        "department": DepartmentOut(code=document.department.code, name=document.department.name),
        "classification": document.classification,
        "file_type": document.file_type,
        "version": document.version,
        "updated_at": document.updated_at,
    }


@router.get("")
def list_documents(
    user: CurrentUser,
    db: DbSession,
    response: Response,
    department: Annotated[str | None, Query(max_length=50)] = None,
    classification: Classification | None = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = MAX_PAGE,
    offset: Annotated[int, Query(ge=0, le=MAX_OFFSET)] = 0,
) -> DocumentList:
    query = readable_documents(user)
    if department is not None:
        query = query.where(
            Document.department_id.in_(
                select(Department.id).where(Department.code == department.strip().lower())
            )
        )
    if classification is not None:
        query = query.where(Document.classification == classification)

    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    documents = db.scalars(
        query.options(joinedload(Document.department), defer(Document.content))
        .order_by(Document.title, Document.id)
        .limit(limit)
        .offset(offset)
    ).all()

    response.headers["Cache-Control"] = "no-store"
    return DocumentList(
        total=total,
        documents=[DocumentSummary.model_validate(_summary_fields(d)) for d in documents],
    )


@router.get("/{document_id}")
def read_document(
    document_id: Annotated[int, Path(ge=1, le=MAX_ID)],
    user: CurrentUser,
    db: DbSession,
    response: Response,
) -> DocumentDetail:
    try:
        document = get_readable_document(db, user, document_id)
    except NotFoundError as error:
        exists = bool(error.context.get("exists"))
        write(
            db,
            AuditAction.DOCUMENT_READ,
            "denied" if exists else "not_found",
            actor_id=user.id,
            # A missing ID can't be stored as a reference to a document.
            document_id=document_id if exists else None,
            details={} if exists else {"requested_id": document_id},
        )
        raise
    write(db, AuditAction.DOCUMENT_READ, "allowed", actor_id=user.id, document_id=document.id)

    response.headers["Cache-Control"] = "no-store"
    return DocumentDetail.model_validate(
        {
            **_summary_fields(document),
            "source_filename": document.source_filename,
            "size_bytes": document.size_bytes,
            "created_at": document.created_at,
            "content": document.content,
        }
    )

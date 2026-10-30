"""Managing documents (FR-2, FR-4). Needs the manage-documents permission.

    POST   /admin/documents                          upload a file with its labels (multipart)
    GET    /admin/documents                          every document, newest first
    GET    /admin/documents/{id}                     how a document is managed
    POST   /admin/documents/{id}/versions            replace it with a new file (multipart)
    PATCH  /admin/documents/{id}                     change its classification or department
    DELETE /admin/documents/{id}                     withdraw it
    PUT    /admin/documents/{id}/grants              let one person read it
    DELETE /admin/documents/{id}/grants/{email}      take that away

Every route checks the permission before doing anything, and someone without
it gets 403: that these routes exist is no secret. The work itself is done by
the ingestion pipeline and the document access module, which check the
permission again.

Managing a document is not reading it (docs/authorization.md). A knowledge
admin decides who may read a document without being able to read it, so
these responses never hold a document's title, file name or text: an admin
works with documents by ID, picked from the list. They do show who holds a
grant, because setting that is the admin's job. The IDs are the documents'
public ones, the same a reader is given.

Every change applies to the next request: access is decided from the
database, not from the index. Search is different in one way. A new document
or version is readable at once but can only be found by search once the
index has been rebuilt and the service restarted, so every response says
whether the document is in the index the service is using.
"""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Path, Query, Response, UploadFile, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.components import Loaded
from app.api.responses import errors
from app.auth.dependencies import DbSession
from app.authorization.dependencies import require_permission
from app.authorization.document_access import (
    GrantRequest,
    change_labels,
    grant_access,
    revoke_access,
)
from app.authorization.permissions import Permission
from app.core.config import Settings, get_settings
from app.core.exceptions import NotFoundError
from app.ingestion.pipeline import Upload, delete_document, ingest, replace_document
from app.models import Document, DocumentGrant, User, document_number, public_ids

router = APIRouter(prefix="/admin/documents", tags=["admin"])

CanManage = Annotated[User, Depends(require_permission(Permission.MANAGE_DOCUMENTS))]
AppSettings = Annotated[Settings, Depends(get_settings)]
MAX_READERS = 50
MAX_PAGE = 100

# For the description of the routes (app/api/responses.py).
SELF = (
    "The caller's roles don't give documents:manage, or this would give the caller access to "
    "the document. Recorded in the audit trail."
)
NO_DOC = "No document with this ID."
MAX_OFFSET = 1_000_000


class GrantOut(BaseModel):
    email: str
    expires_at: datetime | None


class ManagedDocument(BaseModel):
    """What an admin sees of a document: how it is managed, not what it says."""

    id: UUID
    version: int
    status: str
    department: str
    classification: str
    file_type: str
    size_bytes: int
    chunks: int
    # The version this one replaced.
    supersedes_id: UUID | None
    grants: list[GrantOut]
    # Whether search can find it yet. False for a document added since the
    # index the service is using was built.
    in_index: bool


class ManagedList(BaseModel):
    # How many there are, across every page.
    total: int
    documents: list[ManagedDocument]


class LabelChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    classification: str | None = Field(default=None, max_length=20)
    department: str | None = Field(default=None, max_length=50)


class GrantIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=1, max_length=255)
    # With a timezone, and in the future. None means until it is revoked.
    expires_at: datetime | None = None


def _number(db: Session, public_id: UUID) -> int:
    """The document's own ID, which the services below take."""
    number = document_number(db, public_id)
    if number is None:
        raise NotFoundError(f"no document has the ID {public_id}")
    return number


def _managed(db: Session, document: Document, components: Loaded) -> ManagedDocument:
    db.refresh(document)
    chunk_ids = {chunk.id for chunk in document.chunks}
    replaced = public_ids(db, [document.supersedes_id])
    return ManagedDocument(
        id=document.public_id,
        version=document.version,
        status=document.status,
        department=document.department.code,
        classification=document.classification,
        file_type=document.file_type,
        size_bytes=document.size_bytes,
        chunks=len(chunk_ids),
        supersedes_id=None if document.supersedes_id is None else replaced[document.supersedes_id],
        grants=[
            GrantOut(email=email, expires_at=expires_at)
            for email, expires_at in db.execute(
                select(User.email, DocumentGrant.expires_at)
                .join(User, User.id == DocumentGrant.user_id)
                .where(DocumentGrant.document_id == document.id)
                .order_by(User.email)
            )
        ],
        in_index=bool(chunk_ids) and chunk_ids <= components.index.chunk_ids,
    )


def _file_bytes(file: UploadFile, settings: Settings) -> bytes:
    # One byte past the limit is enough for the pipeline to refuse it.
    return file.file.read(settings.max_upload_bytes + 1)


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    summary="Upload a document",
    description="A file with its department, classification and readers. Stored with its "
    "chunks and recorded in one transaction. Search finds it after the next index build.",
    responses=errors(401, 403, 422, 503, e403=SELF),
)
def upload(
    admin: CanManage,
    db: DbSession,
    settings: AppSettings,
    components: Loaded,
    file: Annotated[UploadFile, File()],
    title: Annotated[str, Form(max_length=255)],
    department: Annotated[str, Form(max_length=50)],
    classification: Annotated[str, Form(max_length=20)],
    readers: Annotated[list[str] | None, Form(max_length=MAX_READERS)] = None,
) -> ManagedDocument:
    document = ingest(
        db,
        admin,
        Upload(
            filename=file.filename or "",
            data=_file_bytes(file, settings),
            title=title,
            department_code=department,
            classification=classification,
            grants=tuple(GrantRequest(email=email) for email in readers or ()),
        ),
        settings,
    )
    return _managed(db, document, components)


@router.get(
    "",
    summary="Every document, as it is managed",
    description="Newest first, replaced and deleted versions included. Labels, version and "
    "grants, never a title, file name or text.",
    responses=errors(401, 403, 422, 503),
)
def managed_documents(
    _: CanManage,
    db: DbSession,
    components: Loaded,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = MAX_PAGE,
    offset: Annotated[int, Query(ge=0, le=MAX_OFFSET)] = 0,
) -> ManagedList:
    # Every version, replaced and deleted ones too: the list is where an
    # admin finds a document's ID, and the status says which can be managed.
    total = db.scalar(select(func.count()).select_from(Document)) or 0
    documents = db.scalars(
        select(Document).order_by(Document.id.desc()).limit(limit).offset(offset)
    ).all()
    return ManagedList(
        total=total, documents=[_managed(db, document, components) for document in documents]
    )


@router.get(
    "/{document_id}",
    summary="How one document is managed",
    description="Its labels, version, status, grants and whether search can find it yet. "
    "Never its title, file name or text.",
    responses=errors(401, 403, 404, 422, 503, e404=NO_DOC),
)
def managed_document(
    document_id: UUID, _: CanManage, db: DbSession, components: Loaded
) -> ManagedDocument:
    document = db.get_one(Document, _number(db, document_id))
    return _managed(db, document, components)


@router.post(
    "/{document_id}/versions",
    status_code=status.HTTP_201_CREATED,
    summary="Replace a document with a new version",
    description="The new version keeps the labels and grants and gets a public ID of its own. "
    "The old one stops being readable at once. Recorded in the audit trail.",
    responses=errors(401, 403, 404, 422, 503, e404=NO_DOC),
)
def new_version(
    document_id: UUID,
    admin: CanManage,
    db: DbSession,
    settings: AppSettings,
    components: Loaded,
    file: Annotated[UploadFile, File()],
    title: Annotated[str | None, Form(max_length=255)] = None,
) -> ManagedDocument:
    document = replace_document(
        db,
        admin,
        _number(db, document_id),
        filename=file.filename or "",
        data=_file_bytes(file, settings),
        settings=settings,
        title=title,
    )
    return _managed(db, document, components)


@router.patch(
    "/{document_id}",
    summary="Change its classification or department",
    description="Applies to the next request, with no re-indexing. A change that would let the "
    "caller read the document is refused. Recorded in the audit trail.",
    responses=errors(401, 403, 404, 422, 503, e403=SELF, e404=NO_DOC),
)
def relabel(
    document_id: UUID,
    body: LabelChange,
    admin: CanManage,
    db: DbSession,
    components: Loaded,
) -> ManagedDocument:
    document = change_labels(
        db,
        admin,
        _number(db, document_id),
        classification=body.classification,
        department_code=body.department,
    )
    return _managed(db, document, components)


@router.delete(
    "/{document_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Withdraw a document",
    description="Nobody can read it from the next request. It is kept, marked deleted. "
    "Recorded in the audit trail.",
    responses=errors(401, 403, 404, 422, 503, e404=NO_DOC),
)
def withdraw(document_id: UUID, admin: CanManage, db: DbSession) -> Response:
    delete_document(db, admin, _number(db, document_id))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.put(
    "/{document_id}/grants",
    summary="Let one person read it",
    description="A grant to an active person with a reading role, until an optional expiry. "
    "Nobody can grant to themselves. Recorded in the audit trail.",
    responses=errors(401, 403, 404, 422, 503, e403=SELF, e404=NO_DOC),
)
def grant(
    document_id: UUID,
    body: GrantIn,
    admin: CanManage,
    db: DbSession,
    components: Loaded,
) -> ManagedDocument:
    given = grant_access(
        db,
        admin,
        _number(db, document_id),
        GrantRequest(email=body.email, expires_at=body.expires_at),
    )
    return _managed(db, given.document, components)


@router.delete(
    "/{document_id}/grants/{email}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Take a person's access away",
    description="Applies to the next request. Recorded in the audit trail.",
    responses=errors(
        401, 403, 404, 422, 503, e404="No document with this ID, or no grant on it for that email."
    ),
)
def revoke(
    document_id: UUID,
    email: Annotated[str, Path(max_length=255)],
    admin: CanManage,
    db: DbSession,
) -> Response:
    revoke_access(db, admin, _number(db, document_id), email)
    return Response(status_code=status.HTTP_204_NO_CONTENT)

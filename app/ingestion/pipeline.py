"""The ingestion pipeline: from an uploaded file to a stored document.

    check permission -> validate -> extract text -> clean -> store with its permissions

Everything is checked before anything is written. If any step fails, the
database is left exactly as it was.

A document is never edited in place. Replacing one stores a new version as a
new row and marks the old row as superseded, so what an earlier answer was
based on can still be looked up. Deleting marks the row as deleted. In both
cases the old row stops being readable at once.

A document's chunks are written in the same transaction as the document, so a
document is never stored without them. Chunks belong to one version and are
kept when it is replaced or deleted. They stop being readable with it.
"""

import hashlib
import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.authorization.checks import ensure_permission
from app.authorization.document_access import (
    GrantRequest,
    find_current_document,
    find_department,
    parse_classification,
    resolve_grants,
)
from app.authorization.permissions import Permission
from app.core.config import Settings
from app.core.database import utc_now
from app.core.exceptions import InvalidInputError
from app.ingestion.chunking import chunk_text
from app.ingestion.cleaning import clean_text
from app.ingestion.extractors import EXTRACTORS, base_name, extension_of, extract_text
from app.models import Chunk, Classification, Document, DocumentGrant, DocumentStatus, User

logger = logging.getLogger(__name__)

MAX_TITLE_LENGTH = 255
MAX_FILENAME_LENGTH = 255

# A compressed file can hold far more text than its size suggests. This bounds
# what one document can add to the database and to every later stage.
MAX_EXTRACTED_CHARACTERS = 2_000_000


@dataclass(frozen=True)
class Upload:
    """A file, the labels its uploader gave it, and any named readers."""

    filename: str
    data: bytes
    title: str
    department_code: str
    classification: str
    grants: tuple[GrantRequest, ...] = ()


def ingest(db: Session, user: User, upload: Upload, settings: Settings) -> Document:
    """Validate an upload, extract its text and store it as a new document.

    Raises PermissionDeniedError if the user may not manage documents, and
    InvalidInputError if the file or its labels are not acceptable.
    """
    ensure_permission(user, Permission.MANAGE_DOCUMENTS)

    title = _valid_title(upload.title)
    classification = parse_classification(upload.classification)
    department = find_department(db, upload.department_code)
    readers = resolve_grants(db, user, upload.grants)
    if classification is Classification.RESTRICTED and not readers:
        # It would be stored but readable by nobody, which looks like a lost document.
        raise InvalidInputError("a restricted document needs at least one named reader")

    content, checksum = _read_file(db, upload.filename, upload.data, settings)

    document = Document(
        title=title,
        department=department,
        classification=classification,
        content=content,
        source_filename=_stored_filename(upload.filename),
        file_type=extension_of(upload.filename),
        size_bytes=len(upload.data),
        sha256=checksum,
        uploaded_by=user,
        grants=[
            DocumentGrant(user_id=reader.id, granted_by_id=user.id, expires_at=expires_at)
            for reader, expires_at in readers
        ],
        chunks=_chunks_of(content, settings),
    )
    db.add(document)
    db.commit()

    logger.info(
        "document ingested",
        extra={
            "document_id": document.id,
            "user_id": user.id,
            "department": department.code,
            "classification": classification.value,
            "characters": len(content),
            "chunks": len(document.chunks),
            "grants": len(readers),
        },
    )
    return document


def replace_document(
    db: Session,
    user: User,
    document_id: int,
    *,
    filename: str,
    data: bytes,
    settings: Settings,
    title: str | None = None,
) -> Document:
    """Store a new version of a document and retire the current one.

    The new version keeps the department, the classification and the grants
    that are still valid, so replacing the text can't change who may read it.
    Labels are changed separately, on purpose.
    """
    ensure_permission(user, Permission.MANAGE_DOCUMENTS)
    current = find_current_document(db, document_id)
    new_title = _valid_title(title) if title is not None else current.title
    content, checksum = _read_file(db, filename, data, settings)

    moment = utc_now()
    new_version = Document(
        title=new_title,
        department=current.department,
        classification=current.classification,
        content=content,
        source_filename=_stored_filename(filename),
        file_type=extension_of(filename),
        size_bytes=len(data),
        sha256=checksum,
        uploaded_by=user,
        version=current.version + 1,
        supersedes_id=current.id,
        grants=[
            DocumentGrant(
                user_id=grant.user_id,
                granted_by_id=grant.granted_by_id,
                expires_at=grant.expires_at,
            )
            for grant in current.grants
            if grant.is_valid_at(moment)
        ],
        chunks=_chunks_of(content, settings),
    )
    current.status = DocumentStatus.SUPERSEDED
    db.add(new_version)
    db.commit()

    logger.info(
        "document replaced",
        extra={
            "document_id": new_version.id,
            "replaced_document_id": current.id,
            "version": new_version.version,
            "user_id": user.id,
            "characters": len(content),
            "chunks": len(new_version.chunks),
        },
    )
    return new_version


def delete_document(db: Session, user: User, document_id: int) -> None:
    """Withdraw a document. It stops being readable at once.

    The row and its text are kept, so the audit trail can still explain an
    answer given before the deletion. This is not erasure.
    """
    ensure_permission(user, Permission.MANAGE_DOCUMENTS)
    document = find_current_document(db, document_id)

    document.status = DocumentStatus.DELETED
    db.commit()
    logger.info("document deleted", extra={"document_id": document.id, "user_id": user.id})


def rechunk_documents(db: Session, user: User, settings: Settings) -> int:
    """Bring the chunks of every active document in line with the current settings.

    Needed after the chunk size or overlap is changed, and for documents stored
    before chunking existed. A document whose chunks are already right is not
    touched, so its chunk IDs stay the same. Returns how many were re-chunked.
    """
    ensure_permission(user, Permission.MANAGE_DOCUMENTS)

    changed = 0
    documents = db.scalars(select(Document).where(Document.status == DocumentStatus.ACTIVE))
    for document in documents:
        wanted = _chunks_of(document.content, settings)
        if [_shape(chunk) for chunk in document.chunks] == [_shape(chunk) for chunk in wanted]:
            continue
        # Flushed in between: the old rows must be gone before new ones take
        # the same positions.
        document.chunks.clear()
        db.flush()
        document.chunks = wanted
        changed += 1
    db.commit()

    logger.info("documents re-chunked", extra={"documents": changed, "user_id": user.id})
    return changed


def _chunks_of(content: str, settings: Settings) -> list[Chunk]:
    return [
        Chunk(
            position=piece.position, start_offset=piece.start, end_offset=piece.end, text=piece.text
        )
        for piece in chunk_text(
            content, settings.chunk_max_characters, settings.chunk_overlap_characters
        )
    ]


def _shape(chunk: Chunk) -> tuple[int, int, int, str]:
    return chunk.position, chunk.start_offset, chunk.end_offset, chunk.text


def _read_file(db: Session, filename: str, data: bytes, settings: Settings) -> tuple[str, str]:
    """Check a file and return its cleaned text and its checksum."""
    if extension_of(filename) not in EXTRACTORS:
        supported = ", ".join(sorted(EXTRACTORS))
        raise InvalidInputError(f"unsupported file type; supported types: {supported}")
    if not data:
        raise InvalidInputError("the file is empty")
    if len(data) > settings.max_upload_bytes:
        raise InvalidInputError(
            f"the file is larger than the limit of {settings.max_upload_bytes} bytes"
        )

    # Checked before parsing: a file that is already in use needs no more work.
    checksum = hashlib.sha256(data).hexdigest()
    existing = db.scalars(
        select(Document.id).where(
            Document.sha256 == checksum, Document.status == DocumentStatus.ACTIVE
        )
    ).first()
    if existing is not None:
        raise InvalidInputError(f"this file is already stored as document {existing}")

    extracted = extract_text(filename, data)
    if len(extracted) > MAX_EXTRACTED_CHARACTERS:
        raise InvalidInputError(
            f"the file contains more than {MAX_EXTRACTED_CHARACTERS} characters of text"
        )
    # Checked after cleaning, so a file of nothing but invisible characters
    # counts as empty.
    content = clean_text(extracted)
    if not content:
        raise InvalidInputError("no text could be extracted from the file")
    return content, checksum


def _stored_filename(filename: str) -> str:
    """The name to keep: no directory part, no invisible characters, bounded length."""
    name = clean_text(base_name(filename)).replace("\n", " ")
    return name[-MAX_FILENAME_LENGTH:]


def _valid_title(title: str) -> str:
    title = title.strip()
    if not title:
        raise InvalidInputError("title is required")
    if len(title) > MAX_TITLE_LENGTH:
        raise InvalidInputError(f"title must be at most {MAX_TITLE_LENGTH} characters")
    return title

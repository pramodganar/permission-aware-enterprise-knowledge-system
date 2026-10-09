"""The ingestion pipeline: from an uploaded file to a stored document.

    check permission -> validate -> extract text -> clean -> store with its permissions

Everything is checked before anything is written. If any step fails, the
database is left exactly as it was.
"""

import hashlib
import logging
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.authorization.checks import ensure_permission
from app.authorization.document_access import (
    GrantRequest,
    find_department,
    parse_classification,
    resolve_grants,
)
from app.authorization.permissions import Permission
from app.core.config import Settings
from app.core.exceptions import InvalidInputError
from app.ingestion.cleaning import clean_text
from app.ingestion.extractors import EXTRACTORS, base_name, extension_of, extract_text
from app.models import Classification, Document, DocumentGrant, User

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
    """Validate an upload, extract its text and store it as a document.

    Raises PermissionDeniedError if the user may not manage documents, and
    InvalidInputError if the file or its labels are not acceptable.
    """
    ensure_permission(user, Permission.MANAGE_DOCUMENTS)

    title = _valid_title(upload.title)
    classification = parse_classification(upload.classification)
    _check_file(upload, settings.max_upload_bytes)
    department = find_department(db, upload.department_code)
    readers = resolve_grants(db, user, upload.grants)
    if classification is Classification.RESTRICTED and not readers:
        # It would be stored but readable by nobody, which looks like a lost document.
        raise InvalidInputError("a restricted document needs at least one named reader")

    extracted = extract_text(upload.filename, upload.data)
    if len(extracted) > MAX_EXTRACTED_CHARACTERS:
        raise InvalidInputError(
            f"the file contains more than {MAX_EXTRACTED_CHARACTERS} characters of text"
        )
    # Checked after cleaning, so a file of nothing but invisible characters
    # counts as empty.
    content = clean_text(extracted)
    if not content:
        raise InvalidInputError("no text could be extracted from the file")

    document = Document(
        title=title,
        department=department,
        classification=classification,
        content=content,
        source_filename=_stored_filename(upload.filename),
        file_type=extension_of(upload.filename),
        size_bytes=len(upload.data),
        sha256=hashlib.sha256(upload.data).hexdigest(),
        uploaded_by=user,
        grants=[
            DocumentGrant(user_id=reader.id, granted_by_id=user.id, expires_at=expires_at)
            for reader, expires_at in readers
        ],
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
            "grants": len(readers),
        },
    )
    return document


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


def _check_file(upload: Upload, max_bytes: int) -> None:
    if extension_of(upload.filename) not in EXTRACTORS:
        supported = ", ".join(sorted(EXTRACTORS))
        raise InvalidInputError(f"unsupported file type; supported types: {supported}")
    if not upload.data:
        raise InvalidInputError("the file is empty")
    if len(upload.data) > max_bytes:
        raise InvalidInputError(f"the file is larger than the limit of {max_bytes} bytes")

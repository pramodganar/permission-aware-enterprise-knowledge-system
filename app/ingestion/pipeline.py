"""The ingestion pipeline: from an uploaded file to a stored document.

    check permission -> validate -> extract text -> store

Everything is checked before anything is written. If any step fails, the
database is left exactly as it was.
"""

import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.authorization.checks import ensure_permission
from app.authorization.permissions import Permission
from app.core.config import Settings
from app.core.exceptions import InvalidInputError
from app.ingestion.extractors import EXTRACTORS, extension_of, extract_text
from app.models import Classification, Department, Document, User

logger = logging.getLogger(__name__)

MAX_TITLE_LENGTH = 255


@dataclass(frozen=True)
class Upload:
    """A file and the labels its uploader gave it."""

    filename: str
    data: bytes
    title: str
    department_code: str
    classification: str


def ingest(db: Session, user: User, upload: Upload, settings: Settings) -> Document:
    """Validate an upload, extract its text and store it as a document.

    Raises PermissionDeniedError if the user may not manage documents, and
    InvalidInputError if the file or its labels are not acceptable.
    """
    ensure_permission(user, Permission.MANAGE_DOCUMENTS)

    title = _valid_title(upload.title)
    classification = _valid_classification(upload.classification)
    _check_file(upload, settings.max_upload_bytes)
    department = _find_department(db, upload.department_code)

    content = extract_text(upload.filename, upload.data)
    if not content.strip():
        raise InvalidInputError("no text could be extracted from the file")

    document = Document(
        title=title,
        department=department,
        classification=classification,
        content=content,
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
        },
    )
    return document


def _valid_title(title: str) -> str:
    title = title.strip()
    if not title:
        raise InvalidInputError("title is required")
    if len(title) > MAX_TITLE_LENGTH:
        raise InvalidInputError(f"title must be at most {MAX_TITLE_LENGTH} characters")
    return title


def _valid_classification(value: str) -> Classification:
    # No fallback: a missing or unknown classification stops the ingestion.
    try:
        return Classification(value.strip().lower())
    except ValueError:
        allowed = ", ".join(level.value for level in Classification)
        raise InvalidInputError(f"classification must be one of: {allowed}") from None


def _check_file(upload: Upload, max_bytes: int) -> None:
    if extension_of(upload.filename) not in EXTRACTORS:
        supported = ", ".join(sorted(EXTRACTORS))
        raise InvalidInputError(f"unsupported file type; supported types: {supported}")
    if not upload.data:
        raise InvalidInputError("the file is empty")
    if len(upload.data) > max_bytes:
        raise InvalidInputError(f"the file is larger than the limit of {max_bytes} bytes")


def _find_department(db: Session, code: str) -> Department:
    department = db.scalars(
        select(Department).where(Department.code == code.strip().lower())
    ).one_or_none()
    if department is None:
        raise InvalidInputError("unknown department")
    return department

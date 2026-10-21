"""The managing page: the parts that aren't drawing.

Managing a document is not reading it. The service never tells an admin a
document's title, file name or text, so this page works with documents by
ID and shows only how each is managed: its labels, its version, who holds
a grant and whether search can find it yet.

The service checks every rule: who may manage, what labels exist, who can
be granted. The lists here are only there to offer sensible choices, and a
test keeps them the same as the service's.
"""

import re
from datetime import UTC, date, datetime, time, timedelta

from ui import asking
from ui.client import ApiError, InvalidInput, ManagedDocument, NotAllowed, NotFound

# The same as app.models.Classification and the extractors the pipeline has.
CLASSIFICATIONS = ("general", "departmental", "restricted")
FILE_TYPES = ("txt", "pdf", "docx")
# The service's limit on readers named in one upload.
MAX_READERS = 50

NO_FILE = "Choose a file to upload."
NO_TITLE = "Give the document a title."
NO_DEPARTMENT = "Give the department that owns the document, such as hr."
TOO_MANY_READERS = f"Name at most {MAX_READERS} readers in one upload; grant the rest after."
NOT_FOUND = "No document has that ID."

_SEPARATORS = re.compile(r"[\s,;]+")


def readers_from(text: str) -> tuple[str, ...]:
    """The emails typed, one per line or separated by commas, each once."""
    found = [part.strip() for part in _SEPARATORS.split(text) if part.strip()]
    return tuple(dict.fromkeys(found))


def upload_problem(
    filename: str | None, title: str, department: str, readers: tuple[str, ...]
) -> str | None:
    if not filename:
        return NO_FILE
    if not title.strip():
        return NO_TITLE
    if not department.strip():
        return NO_DEPARTMENT
    if len(readers) > MAX_READERS:
        return TOO_MANY_READERS
    return None


def until_end_of(day: date | None) -> datetime | None:
    """A grant "until" a day lasts through that whole day, in UTC."""
    if day is None:
        return None
    return datetime.combine(day + timedelta(days=1), time.min, tzinfo=UTC)


def label_changes(
    document: ManagedDocument, classification: str, department: str
) -> dict[str, str]:
    """Only the labels that differ, so an unchanged one isn't sent."""
    changes = {}
    if classification != document.classification:
        changes["classification"] = classification
    if department.strip() and department.strip().lower() != document.department:
        changes["department"] = department.strip().lower()
    return changes


def facts(document: ManagedDocument) -> dict[str, str]:
    """What the service says about how a document is managed, in words."""
    return {
        "Status": document.status,
        "Version": str(document.version),
        "Department": document.department,
        "Classification": document.classification,
        "File type": document.file_type,
        "Size": f"{document.size_bytes} bytes",
        "Chunks": str(document.chunks),
        "Replaces": "nothing" if document.supersedes_id is None else str(document.supersedes_id),
        "Search": "finds it" if document.in_index else "can't find it until the index is rebuilt",
    }


def grant_lines(document: ManagedDocument) -> list[str]:
    return [
        f"{grant.email}, "
        + (
            "until revoked"
            if grant.expires_at is None
            else f"until {grant.expires_at.astimezone(UTC):%d %b %Y %H:%M} UTC"
        )
        for grant in document.grants
    ]


def explain(error: ApiError) -> str:
    """What to tell an admin when a call fails."""
    if isinstance(error, NotFound):
        return NOT_FOUND
    if isinstance(error, NotAllowed):
        # The service's words: an account that may not manage, or an admin
        # trying to grant themselves access, which nobody may do.
        return error.message
    if error.status == 413:
        return "The file is larger than the service accepts."
    if isinstance(error, InvalidInput):
        # The service's reason: an unknown department, a grant to the admin
        # themselves, a file it can't read.
        return error.message
    return asking.explain(error)

"""The documents page: the parts that aren't drawing.

The list is the service's ``GET /documents``, with its department and
classification filters, a page at a time. The service decides what is in
it; the UI only asks for a page and shows what came back.

Opening a document fetches it again on every interaction, so a document
whose access was taken away stops showing on the next click. Only its ID is
remembered between interactions, in the widget that chose it, never its text.
"""

from dataclasses import dataclass
from datetime import datetime

from ui.client import Api, DocumentDetail, DocumentSummary

PAGE_SIZE = 20
UNAVAILABLE = (
    "This document isn't available. It may have been removed, or you may no longer have access."
)


@dataclass(frozen=True)
class Listing:
    documents: list[DocumentSummary]
    total: int
    page: int  # from 1
    pages: int


def pages_for(total: int) -> int:
    return max(1, -(-total // PAGE_SIZE))


def listing(
    api: Api, token: str, *, department: str | None, classification: str | None, page: int
) -> Listing:
    """One page of the documents the person may read, with the filters given.

    A page past the end, which happens when a filter leaves fewer documents
    than before, gives the last page instead of an empty one.
    """
    page = max(1, page)

    def fetch(number: int) -> tuple[list[DocumentSummary], int]:
        found = api.documents(
            token,
            department=department,
            classification=classification,
            limit=PAGE_SIZE,
            offset=(number - 1) * PAGE_SIZE,
        )
        return found.documents, found.total

    documents, total = fetch(page)
    if page > pages_for(total):
        page = pages_for(total)
        if total:
            documents, total = fetch(page)
    return Listing(documents=documents, total=total, page=page, pages=pages_for(total))


def when(moment: datetime) -> str:
    return moment.strftime("%d %b %Y")


def rows(documents: list[DocumentSummary]) -> list[dict[str, object]]:
    """The table of a page of documents."""
    return [
        {
            "Title": d.title,
            "Department": d.department.name,
            "Classification": d.classification,
            "Version": d.version,
            "Updated": when(d.updated_at),
        }
        for d in documents
    ]


def size(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes} bytes"
    return f"{size_bytes / 1024:.1f} KB"


def about(document: DocumentDetail) -> str:
    """One line on where the document is from and how it is labelled."""
    return " · ".join(
        [
            document.department.name,
            document.classification,
            f"version {document.version}",
            f"{document.source_filename} ({size(document.size_bytes)})",
            f"updated {when(document.updated_at)}",
        ]
    )

"""The documents page's logic: paging, the table and the line about a document."""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import httpx
import pytest

from ui.client import Api, DocumentDetail
from ui.documents import PAGE_SIZE, about, listing, pages_for, rows, size

UPDATED = datetime(2026, 3, 9, 14, 30, tzinfo=UTC)


def summary(n: int) -> dict[str, Any]:
    return {
        "id": str(UUID(int=n)),
        "title": f"Doc {n:03}",
        "department": {"code": "sales", "name": "Sales"},
        "classification": "general",
        "file_type": "md",
        "version": 2,
        "updated_at": UPDATED.isoformat(),
    }


def serving(count: int) -> tuple[Api, list[httpx.Request]]:
    everything = [summary(n) for n in range(1, count + 1)]
    seen: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        limit, offset = int(request.url.params["limit"]), int(request.url.params["offset"])
        page = everything[offset : offset + limit]
        return httpx.Response(200, json={"total": count, "documents": page})

    http = httpx.Client(base_url="http://api.test", transport=httpx.MockTransport(answer))
    return Api(http=http), seen


@pytest.mark.parametrize(
    ("total", "pages"),
    [(0, 1), (1, 1), (PAGE_SIZE, 1), (PAGE_SIZE + 1, 2), (3 * PAGE_SIZE, 3)],
)
def test_the_number_of_pages(total: int, pages: int) -> None:
    assert pages_for(total) == pages


def test_a_page_is_asked_for_by_offset_with_the_filters() -> None:
    api, seen = serving(PAGE_SIZE * 2 + 3)

    page = listing(api, "tok", department="sales", classification="general", page=3)

    assert [d.id for d in page.documents] == [UUID(int=n) for n in (41, 42, 43)]
    assert (page.total, page.page, page.pages) == (43, 3, 3)
    assert seen[0].url.params["offset"] == str(2 * PAGE_SIZE)
    assert seen[0].url.params["limit"] == str(PAGE_SIZE)
    assert seen[0].url.params["department"] == "sales"
    assert seen[0].url.params["classification"] == "general"


def test_a_page_past_the_end_gives_the_last_page() -> None:
    api, seen = serving(PAGE_SIZE + 5)

    page = listing(api, "tok", department=None, classification=None, page=9)

    assert (page.page, len(page.documents)) == (2, 5)
    assert len(seen) == 2


@pytest.mark.parametrize("asked", [0, -3])
def test_a_page_below_one_is_the_first(asked: int) -> None:
    api, _ = serving(3)

    page = listing(api, "tok", department=None, classification=None, page=asked)

    assert (page.page, len(page.documents)) == (1, 3)


def test_nothing_to_list_is_one_empty_page() -> None:
    api, seen = serving(0)

    page = listing(api, "tok", department=None, classification=None, page=4)

    assert (page.documents, page.total, page.page, page.pages) == ([], 0, 1, 1)
    assert len(seen) == 1


def test_the_table_has_what_a_reader_needs_and_no_ids() -> None:
    api, _ = serving(1)

    table = rows(listing(api, "tok", department=None, classification=None, page=1).documents)

    assert table == [
        {
            "Title": "Doc 001",
            "Department": "Sales",
            "Classification": "general",
            "Version": 2,
            "Updated": "09 Mar 2026",
        }
    ]


@pytest.mark.parametrize(
    ("size_bytes", "said"), [(0, "0 bytes"), (1023, "1023 bytes"), (1536, "1.5 KB")]
)
def test_sizes(size_bytes: int, said: str) -> None:
    assert size(size_bytes) == said


def test_the_line_about_a_document() -> None:
    document = DocumentDetail.model_validate(
        {
            **summary(1),
            "source_filename": "leave-policy.pdf",
            "size_bytes": 2048,
            "created_at": UPDATED.isoformat(),
            "content": "text",
        }
    )

    assert about(document) == (
        "Sales · general · version 2 · leave-policy.pdf (2.0 KB) · updated 09 Mar 2026"
    )

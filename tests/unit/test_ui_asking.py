"""The ask page's logic: what may be chosen, what is checked before sending, what is said."""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import httpx
import pytest

from ui.asking import (
    EMPTY_QUESTION,
    MAX_DEPARTMENTS,
    MAX_DOCUMENTS,
    PAGE_SIZE,
    TOO_MANY_DEPARTMENTS,
    TOO_MANY_DOCUMENTS,
    choices,
    chosen,
    explain,
    problem_with,
    readable_documents,
)
from ui.client import (
    Api,
    ApiError,
    DocumentSummary,
    Filters,
    InvalidInput,
    NotAllowed,
    TooManyAttempts,
    Unavailable,
)
from ui.session import UNREACHABLE


def uuid_of(n: int) -> UUID:
    return UUID(int=n)


def summary(id_: int, title: str, department: str, classification: str) -> dict[str, Any]:
    return {
        "id": str(UUID(int=id_)),
        "title": title,
        "department": {"code": department, "name": department.title()},
        "classification": classification,
        "file_type": "md",
        "version": 1,
        "updated_at": datetime(2026, 1, 1, tzinfo=UTC).isoformat(),
    }


def listing(count: int) -> list[dict[str, Any]]:
    return [summary(n, f"Doc {n:04}", "sales", "internal") for n in range(1, count + 1)]


def serving(documents: list[dict[str, Any]]) -> tuple[Api, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        limit = int(request.url.params["limit"])
        offset = int(request.url.params["offset"])
        page = documents[offset : offset + limit]
        return httpx.Response(200, json={"total": len(documents), "documents": page})

    http = httpx.Client(base_url="http://api.test", transport=httpx.MockTransport(answer))
    return Api(http=http), seen


@pytest.mark.parametrize("count", [0, 1, PAGE_SIZE - 1, PAGE_SIZE, PAGE_SIZE + 1, 3 * PAGE_SIZE])
def test_every_readable_document_is_fetched_page_by_page(count: int) -> None:
    api, seen = serving(listing(count))

    found = readable_documents(api, "tok")

    assert [d.id for d in found] == [uuid_of(n) for n in range(1, count + 1)]
    assert len(seen) == max(1, -(-count // PAGE_SIZE))


def test_fetching_stops_at_a_limit_whatever_the_service_says() -> None:
    api, seen = serving(listing(20 * PAGE_SIZE))

    found = readable_documents(api, "tok")

    assert len(found) == 10 * PAGE_SIZE
    assert len(seen) == 10


def test_the_choices_come_only_from_the_documents_given() -> None:
    documents = [
        DocumentSummary.model_validate(summary(7, "Sales Playbook", "sales", "internal")),
        DocumentSummary.model_validate(summary(3, "Employee Handbook", "hr", "public")),
        DocumentSummary.model_validate(summary(9, "Sales Targets", "sales", "confidential")),
    ]

    offered = choices(documents)

    assert offered.departments == {"hr": "Hr", "sales": "Sales"}
    assert offered.classifications == ("confidential", "internal", "public")
    assert offered.documents == {
        UUID(int=3): "Employee Handbook (v1)",
        UUID(int=7): "Sales Playbook (v1)",
        UUID(int=9): "Sales Targets (v1)",
    }
    assert list(offered.documents) == [UUID(int=3), UUID(int=7), UUID(int=9)]
    assert not offered.empty
    assert choices([]).empty


def test_nothing_chosen_is_no_filter() -> None:
    assert chosen([], [], []).body() is None
    assert chosen(["sales", "hr"], [], [UUID(int=9), UUID(int=3)]) == Filters(
        departments=("hr", "sales"), document_ids=(UUID(int=3), UUID(int=9))
    )


@pytest.mark.parametrize(
    ("question", "filters", "problem"),
    [
        ("", Filters(), EMPTY_QUESTION),
        ("   \n", Filters(), EMPTY_QUESTION),
        (
            "Leave?",
            Filters(document_ids=tuple(map(uuid_of, range(1, MAX_DOCUMENTS + 2)))),
            TOO_MANY_DOCUMENTS,
        ),
        (
            "Leave?",
            Filters(departments=tuple(f"d{n}" for n in range(MAX_DEPARTMENTS + 1))),
            TOO_MANY_DEPARTMENTS,
        ),
        ("Leave?", Filters(document_ids=tuple(map(uuid_of, range(1, MAX_DOCUMENTS + 1)))), None),
        ("Leave?", Filters(), None),
    ],
)
def test_a_request_the_service_would_refuse_is_caught_first(
    question: str, filters: Filters, problem: str | None
) -> None:
    assert problem_with(question, filters) == problem


@pytest.mark.parametrize(
    ("error", "said"),
    [
        (
            InvalidInput(422, "invalid_input", "The question is too long."),
            "The question is too long.",
        ),
        (
            TooManyAttempts(429, "too_many_requests", "x", retry_after=12),
            "Try again in 12 seconds.",
        ),
        (NotAllowed(403, "permission_denied", "x"), "Your account may not ask questions."),
        (Unavailable(0, "unreachable", "x"), UNREACHABLE),
        (
            Unavailable(0, "timeout", "The service took too long to answer."),
            "The service took too long to answer.",
        ),
        (
            Unavailable(503, "service_unavailable", "Answers are switched off on this service."),
            "Answers are switched off on this service.",
        ),
        (ApiError(500, "internal_error", "boom", request_id="req-42"), "(reference req-42)"),
        (ApiError(502, "bad_response", "x"), "Something went wrong. Try again."),
    ],
)
def test_each_failure_is_explained_in_the_services_words_or_plainly(
    error: ApiError, said: str
) -> None:
    assert said in explain(error)

"""The managing and audit pages' logic, and that their lists match the service's."""

from datetime import UTC, date, datetime
from typing import Any

import httpx
import pytest

from app.api import admin
from app.api import audit as audit_api
from app.ingestion.extractors import EXTRACTORS
from app.models import AuditAction, Classification
from ui import managing, reviewing
from ui.client import (
    Api,
    ApiError,
    AuditEntry,
    InvalidInput,
    ManagedDocument,
    NotAllowed,
    NotFound,
    Unavailable,
)
from ui.session import UNREACHABLE

# --- in step with the service -----------------------------------------------------------


def test_the_classifications_offered_are_the_services() -> None:
    assert set(managing.CLASSIFICATIONS) == {c.value for c in Classification}


def test_the_file_types_offered_are_the_ones_the_pipeline_reads() -> None:
    assert {f".{kind}" for kind in managing.FILE_TYPES} == set(EXTRACTORS)


def test_the_reader_limit_is_the_services() -> None:
    assert managing.MAX_READERS == admin.MAX_READERS


def test_the_actions_offered_are_the_services() -> None:
    assert tuple(a.value for a in AuditAction) == reviewing.ACTIONS


def test_an_audit_page_is_one_the_service_will_give() -> None:
    assert reviewing.PAGE_SIZE <= audit_api.MAX_PAGE


# --- managing ---------------------------------------------------------------------------


def managed(**changes: Any) -> ManagedDocument:
    return ManagedDocument.model_validate(
        {
            "id": 7,
            "version": 2,
            "status": "active",
            "department": "hr",
            "classification": "restricted",
            "file_type": "pdf",
            "size_bytes": 2048,
            "chunks": 3,
            "supersedes_id": 4,
            "grants": [
                {"email": "carol@acmecorp.example", "expires_at": None},
                {"email": "emma@acmecorp.example", "expires_at": "2026-12-01T00:00:00+00:00"},
            ],
            "in_index": False,
            **changes,
        }
    )


@pytest.mark.parametrize(
    ("typed", "readers"),
    [
        ("", ()),
        ("  \n ", ()),
        ("a@x.example", ("a@x.example",)),
        ("a@x.example\nb@x.example", ("a@x.example", "b@x.example")),
        ("a@x.example, b@x.example; a@x.example", ("a@x.example", "b@x.example")),
        ("  a@x.example  \n\n", ("a@x.example",)),
    ],
)
def test_readers_are_read_one_per_line_or_comma_each_once(
    typed: str, readers: tuple[str, ...]
) -> None:
    assert managing.readers_from(typed) == readers


@pytest.mark.parametrize(
    ("filename", "title", "department", "readers", "problem"),
    [
        (None, "T", "hr", (), managing.NO_FILE),
        ("", "T", "hr", (), managing.NO_FILE),
        ("a.txt", " ", "hr", (), managing.NO_TITLE),
        ("a.txt", "T", "  ", (), managing.NO_DEPARTMENT),
        (
            "a.txt",
            "T",
            "hr",
            tuple(f"p{n}@x.example" for n in range(51)),
            managing.TOO_MANY_READERS,
        ),
        ("a.txt", "T", "hr", tuple(f"p{n}@x.example" for n in range(50)), None),
        ("a.txt", "T", "hr", (), None),
    ],
)
def test_an_upload_the_service_would_refuse_is_caught_first(
    filename: str | None, title: str, department: str, readers: tuple[str, ...], problem: str | None
) -> None:
    assert managing.upload_problem(filename, title, department, readers) == problem


def test_a_grant_until_a_day_lasts_the_whole_day_in_utc() -> None:
    assert managing.until_end_of(date(2026, 11, 30)) == datetime(2026, 12, 1, tzinfo=UTC)
    assert managing.until_end_of(None) is None


@pytest.mark.parametrize(
    ("classification", "department", "sent"),
    [
        ("restricted", "hr", {}),
        ("restricted", " HR ", {}),
        ("restricted", "", {}),
        ("general", "hr", {"classification": "general"}),
        ("restricted", "Legal", {"department": "legal"}),
        ("departmental", "legal", {"classification": "departmental", "department": "legal"}),
    ],
)
def test_only_the_labels_that_changed_are_sent(
    classification: str, department: str, sent: dict[str, str]
) -> None:
    assert managing.label_changes(managed(), classification, department) == sent


def test_what_is_shown_of_a_managed_document_holds_no_title_or_text() -> None:
    facts = managing.facts(managed())

    assert facts == {
        "Status": "active",
        "Version": "2",
        "Department": "hr",
        "Classification": "restricted",
        "File type": "pdf",
        "Size": "2048 bytes",
        "Chunks": "3",
        "Replaces": "4",
        "Search": "can't find it until the index is rebuilt",
    }
    assert managing.facts(managed(in_index=True, supersedes_id=None))["Search"] == "finds it"
    assert managing.facts(managed(supersedes_id=None))["Replaces"] == "nothing"


def test_grants_say_who_and_until_when() -> None:
    assert managing.grant_lines(managed()) == [
        "carol@acmecorp.example, until revoked",
        "emma@acmecorp.example, until 01 Dec 2026 00:00 UTC",
    ]


@pytest.mark.parametrize(
    ("error", "said"),
    [
        (NotFound(404, "not_found", "Not found."), managing.NOT_FOUND),
        (
            NotAllowed(403, "permission_denied", "You are not allowed to perform this action."),
            "You are not allowed to perform this action.",
        ),
        (ApiError(413, "request_too_large", "x"), "The file is larger than the service accepts."),
        (InvalidInput(422, "invalid_input", "unknown department: zz"), "unknown department: zz"),
        (Unavailable(0, "unreachable", "x"), UNREACHABLE),
    ],
)
def test_each_managing_failure_is_explained(error: ApiError, said: str) -> None:
    assert managing.explain(error) == said


# --- reviewing --------------------------------------------------------------------------


def test_a_window_runs_from_the_start_of_the_first_day_to_the_end_of_the_last() -> None:
    since, until = reviewing.window(date(2026, 10, 1), date(2026, 10, 3))

    assert since == datetime(2026, 10, 1, tzinfo=UTC)
    assert until == datetime(2026, 10, 4, tzinfo=UTC)
    assert reviewing.window(None, None) == (None, None)


def test_a_window_backwards_is_caught() -> None:
    assert reviewing.window_problem(date(2026, 10, 3), date(2026, 10, 1)) == reviewing.BACKWARDS
    assert reviewing.window_problem(date(2026, 10, 1), date(2026, 10, 1)) is None
    assert reviewing.window_problem(None, date(2026, 10, 1)) is None


def entry(n: int, **changes: Any) -> dict[str, Any]:
    return {
        "id": n,
        "created_at": "2026-10-21T09:15:02.123+00:00",
        "request_id": f"req-{n}",
        "actor": {"id": 4, "email": "david@acmecorp.example"},
        "action": "question.asked",
        "outcome": "answered",
        "document_id": None,
        "question": "How many days of **annual leave** do I get?",
        "chunk_ids": [12, 3],
        "details": {"model": "fake", "chunks": 2},
        **changes,
    }


def test_a_record_is_shown_as_the_service_gave_it() -> None:
    table = reviewing.rows(
        [
            AuditEntry.model_validate(entry(1)),
            AuditEntry.model_validate(
                entry(2, actor=None, question=None, chunk_ids=[], details={}, request_id=None)
            ),
        ]
    )

    assert table[0] == {
        "When (UTC)": "2026-10-21 09:15:02",
        "Who": "david@acmecorp.example",
        "Action": "question.asked",
        "Outcome": "answered",
        "Document": None,
        "Question": "How many days of **annual leave** do I get?",
        "Chunks": "12, 3",
        "Details": '{"chunks": 2, "model": "fake"}',
        "Request": "req-1",
    }
    assert (table[1]["Who"], table[1]["Question"], table[1]["Chunks"], table[1]["Details"]) == (
        "(no such account)",
        "",
        "",
        "",
    )


def trail(count: int) -> tuple[Api, list[httpx.Request]]:
    records = [entry(n) for n in range(count, 0, -1)]
    seen: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        limit, offset = int(request.url.params["limit"]), int(request.url.params["offset"])
        return httpx.Response(
            200, json={"total": count, "records": records[offset : offset + limit]}
        )

    http = httpx.Client(base_url="http://api.test", transport=httpx.MockTransport(answer))
    return Api(http=http), seen


def fetch(api: Api, page: int, **filters: Any) -> tuple[Any, int]:
    query: dict[str, Any] = {
        "actor": None,
        "action": None,
        "document_id": None,
        "since": None,
        "until": None,
        **filters,
    }
    return reviewing.fetch(api, "tok", page=page, **query)


def test_a_page_of_the_trail_is_asked_for_by_offset_with_the_filters() -> None:
    api, seen = trail(reviewing.PAGE_SIZE + 3)

    found, page = fetch(
        api,
        2,
        actor="david@acmecorp.example",
        action="search",
        document_id=9,
        since=datetime(2026, 10, 1, tzinfo=UTC),
    )

    assert (page, len(found.records)) == (2, 3)
    params = seen[0].url.params
    assert params["offset"] == str(reviewing.PAGE_SIZE)
    assert (params["actor"], params["action"], params["document_id"]) == (
        "david@acmecorp.example",
        "search",
        "9",
    )
    assert params["since"] == "2026-10-01T00:00:00+00:00"
    assert "until" not in params


def test_a_page_of_the_trail_past_the_end_gives_the_last() -> None:
    api, seen = trail(reviewing.PAGE_SIZE + 3)

    found, page = fetch(api, 7)

    assert (page, len(found.records), len(seen)) == (2, 3, 2)


def test_an_empty_trail_is_page_one() -> None:
    api, seen = trail(0)

    found, page = fetch(api, 3)

    assert (page, found.records, len(seen)) == (1, [], 1)


@pytest.mark.parametrize(
    ("error", "said"),
    [
        (NotAllowed(403, "permission_denied", "x"), "Your account may not read the audit trail."),
        (
            InvalidInput(422, "invalid_input", "until must have a timezone"),
            "until must have a timezone",
        ),
        (Unavailable(0, "unreachable", "x"), UNREACHABLE),
    ],
)
def test_each_audit_failure_is_explained(error: ApiError, said: str) -> None:
    assert reviewing.explain(error) == said

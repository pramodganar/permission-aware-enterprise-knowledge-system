"""The UI's client on its own: its contract with the API, its boundary, and its failures."""

import ast
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from pydantic import BaseModel

from app.api import admin, audit, auth, documents, questions
from ui import client
from ui.client import (
    Api,
    ApiError,
    Filters,
    InvalidInput,
    NotAllowed,
    NotFound,
    NotSignedIn,
    TooManyAttempts,
    Unavailable,
)

UI = Path(__file__).resolve().parents[2] / "ui"

# Each model the UI reads, and the API model it mirrors.
CONTRACT: list[tuple[type[BaseModel], type[BaseModel]]] = [
    (client.Department, auth.Department),
    (client.Me, auth.Me),
    (client.DocumentSummary, documents.DocumentSummary),
    (client.DocumentList, documents.DocumentList),
    (client.DocumentDetail, documents.DocumentDetail),
    (client.DocumentRef, questions.DocumentRef),
    (client.Passage, questions.Passage),
    (client.SearchResult, questions.SearchResponse),
    (client.Citation, questions.Citation),
    (client.Answer, questions.AskResponse),
    (client.Grant, admin.GrantOut),
    (client.ManagedDocument, admin.ManagedDocument),
    (client.Actor, audit.Actor),
    (client.AuditEntry, audit.AuditEntry),
    (client.AuditPage, audit.AuditList),
]


@pytest.mark.parametrize(("ui_model", "api_model"), CONTRACT, ids=lambda m: m.__name__)
def test_the_ui_reads_every_field_the_api_sends_and_no_other(
    ui_model: type[BaseModel], api_model: type[BaseModel]
) -> None:
    assert set(ui_model.model_fields) == set(api_model.model_fields)
    for name, field in ui_model.model_fields.items():
        # A field the API always sends must not be one the UI could do without.
        assert field.is_required() == api_model.model_fields[name].is_required(), name


def test_the_ui_ignores_a_field_a_newer_service_adds() -> None:
    me = client.Me.model_validate(
        {
            "id": 4,
            "email": "david@acmecorp.example",
            "full_name": "David Okafor",
            "department": {"code": "sales", "name": "Sales"},
            "roles": ["employee"],
            "permissions": ["documents:read:general"],
            "added_in_a_later_version": True,
        }
    )

    assert me.can("documents:read:general")


# --- the boundary -----------------------------------------------------------------------

# The demo page calls the application code directly, by design (docs/demo.md).
DEMO = {"demo.py", "streamlit_app.py"}


def imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module.split(".")[0])
    return found


def test_the_ui_never_imports_the_service() -> None:
    checked = sorted(p for p in UI.glob("*.py") if p.name not in DEMO | {"__init__.py"})

    assert checked, "nothing to check"
    for path in checked:
        assert not imported_modules(path) & {"app", "scripts"}, path.name


def test_the_demo_is_the_only_exception() -> None:
    assert {"app"} <= imported_modules(UI / "demo.py")
    assert {p.name for p in UI.glob("*.py")} >= DEMO | {"client.py"}


# --- failures, against a fake server ----------------------------------------------------

Handler = Callable[[httpx.Request], httpx.Response]


def api_answering(handler: Handler) -> tuple[Api, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    http = httpx.Client(base_url="http://api.test", transport=httpx.MockTransport(record))
    return Api(http=http), seen


def error(status: int, code: str, message: str, **extra: object) -> Handler:
    body = {"error": {"code": code, "message": message, "request_id": "req-1", **extra}}
    return lambda _: httpx.Response(status, json=body)


@pytest.mark.parametrize(
    ("status", "kind"),
    [
        (401, NotSignedIn),
        (403, NotAllowed),
        (404, NotFound),
        (422, InvalidInput),
        (429, TooManyAttempts),
        (503, Unavailable),
        (500, ApiError),
        (418, ApiError),
    ],
)
def test_each_status_becomes_its_own_error_with_the_services_words(
    status: int, kind: type[ApiError]
) -> None:
    api, _ = api_answering(error(status, "some_code", "Some message.", fields=["body.question"]))

    with pytest.raises(kind) as raised:
        api.me("token")

    assert type(raised.value) is kind
    assert (raised.value.status, raised.value.code, raised.value.message) == (
        status,
        "some_code",
        "Some message.",
    )
    assert raised.value.request_id == "req-1"
    assert raised.value.fields == ("body.question",)


def test_retry_after_is_read_from_the_header() -> None:
    def limited(_: httpx.Request) -> httpx.Response:
        body = {"error": {"code": "too_many_requests", "message": "Wait."}}
        return httpx.Response(429, json=body, headers={"Retry-After": "900"})

    api, _ = api_answering(limited)

    with pytest.raises(TooManyAttempts) as raised:
        api.login("frank@acmecorp.example", "x")

    assert raised.value.retry_after == 900


@pytest.mark.parametrize(
    "reply",
    [
        httpx.Response(200, text="<html>a proxy page</html>"),
        httpx.Response(200, json=["not", "an", "object"]),
        httpx.Response(200, json={"id": "not a number"}),
        httpx.Response(502, text="Bad Gateway"),
        httpx.Response(200, json={"access_token": 7, "expires_in": "soon"}),
    ],
    ids=["html", "a list", "wrong types", "an error that isn't ours", "a bad token reply"],
)
def test_a_reply_the_ui_does_not_understand_is_a_bad_response(reply: httpx.Response) -> None:
    api, _ = api_answering(lambda _: reply)

    with pytest.raises(ApiError) as raised:
        if "access_token" in reply.text:
            api.login("david@acmecorp.example", "x")
        else:
            api.me("token")

    assert raised.value.code == "bad_response"


def test_a_timeout_is_unavailable() -> None:
    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    api, _ = api_answering(slow)

    with pytest.raises(Unavailable) as raised:
        api.ask("token", "leave?")

    assert raised.value.code == "timeout"


def test_an_empty_token_is_not_sent_at_all() -> None:
    api, seen = api_answering(lambda _: httpx.Response(200, json={}))

    with pytest.raises(NotSignedIn):
        api.documents("")

    assert seen == []


# --- what is sent -----------------------------------------------------------------------


def test_an_email_can_not_change_which_route_a_revoke_reaches() -> None:
    api, seen = api_answering(lambda _: httpx.Response(204))

    api.revoke("token", 9, "a/b?c#d@acmecorp.example")

    assert seen[0].url.raw_path == b"/admin/documents/9/grants/a%2Fb%3Fc%23d@acmecorp.example"
    assert seen[0].method == "DELETE"


def test_a_filter_that_is_not_set_is_not_sent() -> None:
    api, seen = api_answering(
        lambda _: httpx.Response(200, json={"results": [], "message": "No information."})
    )

    api.search("token", "leave?", Filters(departments=("hr",)))
    api.search("token", "leave?", Filters())

    assert seen[0].content == b'{"question":"leave?","filters":{"departments":["hr"]}}'
    assert seen[1].content == b'{"question":"leave?"}'


def test_a_document_id_is_always_a_number_in_the_path() -> None:
    api, seen = api_answering(lambda _: httpx.Response(200, json={}))

    with pytest.raises(ValueError):
        api.document("token", "16/../../audit")  # type: ignore[arg-type]

    assert seen == []
